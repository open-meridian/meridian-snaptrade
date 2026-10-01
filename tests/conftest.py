"""Stand-ins for the sidecar, for SnapTrade's SDK, and for the clock.

`Sidecar` is the pinned SDK's own generated `Operations` with the transport
replaced: every call goes through the SDK's real conversion (a Decimal to the
wire's integer and scale, a float refused, a caller's header to the assertion
it carries) and is kept as the protobuf message the sidecar would have
received. It holds the plugin's links as the deployment would, and delivers
them as the SDK's `account_scope()` does: the first at once, and another on
every change.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import meridian
import pytest
from meridian.operations import Operations
from meridian.plugin.v1 import operations_pb2 as ops

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)

_OPERATIONS = SimpleNamespace(
    ReportSyncStatus="ReportSyncStatus",
    RecordHoldingsStatement="RecordHoldingsStatement",
    RecordHolding="RecordHolding",
    ResolveIdentifier="ResolveIdentifier",
    ReportMissingInstrument="ReportMissingInstrument",
    ReportExternalAccounts="ReportExternalAccounts",
    ReadAccountsForLinking="ReadAccountsForLinking",
    LinkExternalAccount="LinkExternalAccount",
)

# The deployment's accounts, as ReadAccountsForLinking answers by default.
DEPLOYMENT_ACCOUNTS = (
    ops.AccountRecord(
        account_id="ACC-1",
        name="Household",
        state=ops.ACCOUNT_STATE_OPEN,
        custodian="Schwab",
        account_type="Brokerage",
        owner="Fund I",
        note="The family's.",
    ),
    ops.AccountRecord(account_id="ACC-3", name="Spare", state=ops.ACCOUNT_STATE_OPEN),
    ops.AccountRecord(account_id="ACC-2", name="Retired", state=ops.ACCOUNT_STATE_CLOSED),
)


def found(
    instrument_id: str = "INS-1", placeholder: bool = False
) -> ops.ResolveIdentifierResult:
    return ops.ResolveIdentifierResult(
        found=not placeholder, instrument_id=instrument_id, placeholder=placeholder
    )


def ambiguous() -> ops.ResolveIdentifierResult:
    return ops.ResolveIdentifierResult(found=False, miss_reason=ops.MISS_REASON_AMBIGUOUS)


class Sidecar(Operations):
    """The pinned SDK's operations, answered here."""

    def __init__(
        self,
        resolve: Callable[[ops.ResolveIdentifierParams], ops.ResolveIdentifierResult]
        | None = None,
        refuse: Callable[[str, Any], Exception | None] | None = None,
        already_recorded: bool = False,
        links: Iterable[meridian.LinkedExternalAccount] = (),
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        # By external account ID, as the deployment holds them.
        self.links = {link.external_account_id: link for link in links}
        self._watching: list[asyncio.Queue[meridian.AccountScope]] = []
        self.reports: list[tuple[bool, str]] = []
        self._resolve = resolve or (lambda params: found())
        self._refuse = refuse or (lambda name, params: None)
        self._already = already_recorded
        self._statements = 0

    def _operations(self) -> Any:
        return _OPERATIONS

    async def _operate(self, method: Any, params: Any) -> Any:
        name = cast(str, method)
        refusal = self._refuse(name, params)
        if refusal is not None:
            raise refusal
        self.calls.append((name, params))
        if name == "ResolveIdentifier":
            return self._resolve(params)
        if name == "RecordHoldingsStatement":
            self._statements += 1
            return ops.RecordHoldingsStatementResult(
                statement_id=f"STMT-{self._statements}", already_recorded=self._already
            )
        if name == "RecordHolding":
            return ops.RecordHoldingResult(
                holding_id=f"H-{len(self.calls)}", resolved=bool(params.instrument_id)
            )
        if name == "ReadAccountsForLinking":
            return ops.ReadAccountsForLinkingResult(accounts=DEPLOYMENT_ACCOUNTS)
        if name == "LinkExternalAccount":
            # The conductor creates a named account and links it in one step,
            # and a link replaces the one standing.
            made = f"ACC-NEW-{len(self.calls)}" if params.new_account_name else ""
            account_id = params.account_id or made
            named = params.new_account_name or {
                account.account_id: account.name for account in DEPLOYMENT_ACCOUNTS
            }.get(account_id, "")
            self.set_link(params.external_account_id, account_id, named)
            return ops.LinkExternalAccountResult(
                external_account_id=params.external_account_id, account_id=account_id
            )
        return ops.Published(message_id=f"M-{len(self.calls)}")

    def scope(self) -> meridian.AccountScope:
        return meridian.AccountScope(links=tuple(self.links.values()))

    def set_link(self, external_account_id: str, account_id: str, name: str) -> None:
        """A link as the deployment now holds it, made here or elsewhere."""
        self.links.pop(external_account_id, None)
        if account_id:
            self.links[external_account_id] = meridian.LinkedExternalAccount(
                external_account_id, account_id, name
            )
        for watching in self._watching:
            watching.put_nowait(self.scope())

    async def account_scope(self) -> AsyncIterator[meridian.AccountScope]:
        """As the SDK's: the links now, then again on every change."""
        watching: asyncio.Queue[meridian.AccountScope] = asyncio.Queue()
        watching.put_nowait(self.scope())
        self._watching.append(watching)
        while True:
            yield await watching.get()

    async def report(self, *, healthy: bool, detail: str = "") -> None:
        self.reports.append((healthy, detail))

    def sent(self, name: str) -> list[Any]:
        return [params for called, params in self.calls if called == name]

    def plugin(self) -> meridian.Plugin:
        return cast(meridian.Plugin, self)


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


def clock(moment: datetime = NOW) -> Callable[[], datetime]:
    return lambda: moment
