"""Stand-ins for the sidecar, for SnapTrade's SDK, and for the clock.

`Sidecar` is the pinned SDK's own generated `Operations` with the transport
replaced: every call goes through the SDK's real conversion (a Decimal to the
wire's integer and scale, a float refused, a caller's header to the assertion
it carries) and the checks the SDK makes before sending (a statement's
figures), and is kept as the protobuf message the sidecar would have
received. It keeps each activity once, as the street does, answering a
redelivery already recorded, and each re-resolution beside it (contract v15),
answering one naming what the activity's latest resolution names already
recorded and refusing one naming no activity it holds. It holds the plugin's
links as the deployment would, one external account per account of the
deployment's, refusing a second as the conductor does (contract v7), and
delivers them as the SDK's `account_scope()` does: the first at once, and
another on every change. A report is kept as the
heartbeat the sidecar receives, built by `meridian.testing.heartbeat`, with
the figures standing as the SDK's do.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import meridian
import pytest
from meridian import edge
from meridian.operations import Operations
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.statements import checked
from meridian.testing import heartbeat
from meridian.v1 import sidecar_pb2

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
# The instance the sidecar registered this plugin as.
INSTANCE = "snaptrade"

_OPERATIONS = SimpleNamespace(
    ReportSyncStatus="ReportSyncStatus",
    RecordHoldingsStatement="RecordHoldingsStatement",
    RecordHolding="RecordHolding",
    ResolveIdentifier="ResolveIdentifier",
    ReportMissingInstrument="ReportMissingInstrument",
    ReportExternalAccounts="ReportExternalAccounts",
    ReadAccountsForLinking="ReadAccountsForLinking",
    LinkExternalAccount="LinkExternalAccount",
    RecordActivity="RecordActivity",
    ReResolveActivity="ReResolveActivity",
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


def found(instrument_id: str = "INS-1", minted: bool = False) -> ops.ResolveIdentifierResult:
    return ops.ResolveIdentifierResult(found=True, instrument_id=instrument_id, minted=minted)


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
        self.heartbeats: list[sidecar_pb2.HeartbeatRequest] = []
        self._figures: Sequence[meridian.Figure] = ()
        self._resolve = resolve or (lambda params: found())
        self._refuse = refuse or (lambda name, params: None)
        self._already = already_recorded
        self._statements = 0
        # The street's activity, once per source, external account and the
        # custodian's identifier, as it keeps them (contract v14).
        self.activities: dict[tuple[str, str, str], str] = {}
        # Each activity's resolution as first recorded, then each
        # re-resolution kept, the latest last: (instrument, provenance).
        self.resolutions: dict[tuple[str, str, str], list[tuple[str, bytes]]] = {}
        self.not_carried: dict[tuple[str, str], int] = {}

    def _operations(self) -> Any:
        return _OPERATIONS

    async def _operate(self, method: Any, params: Any) -> Any:
        name = cast(str, method)
        checked(params)
        refusal = self._refuse(name, params) or self._held(name, params)
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
        if name == "RecordActivity":
            key = (
                params.source,
                params.external_account_id,
                params.activity.external_activity_id,
            )
            already = key in self.activities
            if not already:
                self.activities[key] = f"ACT-{len(self.activities) + 1}"
                own = [p for p in params.activity.provenance if p.field == "instrument_id"]
                self.resolutions[key] = [
                    (
                        params.activity.instrument_id,
                        own[0].SerializeToString() if own else b"",
                    )
                ]
            return ops.RecordActivityResult(
                activity_id=self.activities[key], already_recorded=already
            )
        if name == "ReResolveActivity":
            key = (params.source, params.external_account_id, params.external_activity_id)
            if key not in self.activities:
                raise meridian.CallFailed(
                    "ReResolveActivity",
                    "aborted",
                    f"no activity {params.external_activity_id} from {params.source} "
                    f"on {params.external_account_id}",
                )
            resolution = (params.instrument_id, params.provenance.SerializeToString())
            already = self.resolutions[key][-1] == resolution
            if not already:
                self.resolutions[key].append(resolution)
            return ops.ReResolveActivityResult(
                activity_id=self.activities[key], already_recorded=already
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

    def _held(self, name: str, params: Any) -> Exception | None:
        """The conductor's refusal of a second external account for an account
        (v7), sent as the sidecar sends a handler's error: in its words."""
        if name != "LinkExternalAccount" or not params.account_id:
            return None
        held = next(
            (
                link.external_account_id
                for link in self.links.values()
                if link.account_id == params.account_id
                and link.external_account_id != params.external_account_id
            ),
            None,
        )
        if held is None:
            return None
        return meridian.CallFailed(
            "LinkExternalAccount",
            "handler error",
            f"{params.account_id} already has external account {held} linked "
            f"(snaptrade); an account has one external account: link "
            f"{params.external_account_id} to another account, or a new one",
        )

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

    async def report(
        self,
        *,
        healthy: bool,
        detail: str = "",
        figures: Sequence[meridian.Figure] | None = None,
    ) -> None:
        """As the SDK's: `figures` replaces those standing, which go on it."""
        standing = self._figures if figures is None else tuple(figures)
        sent = heartbeat(healthy=healthy, detail=detail, figures=standing)
        self._figures = standing
        self.reports.append((healthy, detail))
        self.heartbeats.append(sent)

    def raw_record(self, key: str) -> ops.RawRecordRef:
        """As the SDK's: the raw record `key` names, kept by this instance."""
        return edge.raw_record(key, INSTANCE)

    def note_not_carried(self, scheme: str, name: str) -> None:
        """As the SDK's: counted, by name only, for the heartbeat."""
        self.not_carried[(scheme, name)] = self.not_carried.get((scheme, name), 0) + 1

    def sent(self, name: str) -> list[Any]:
        return [params for called, params in self.calls if called == name]

    def plugin(self) -> meridian.Plugin:
        return cast(meridian.Plugin, self)


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


def clock(moment: datetime = NOW) -> Callable[[], datetime]:
    return lambda: moment
