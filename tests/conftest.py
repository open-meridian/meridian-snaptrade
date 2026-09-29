"""Stand-ins for the sidecar, for SnapTrade's SDK, and for the clock.

`Sidecar` is the pinned SDK's own generated `Operations` with the transport
replaced: every call goes through the SDK's real conversion (a Decimal to the
wire's integer and scale, a float refused, a caller's header to the assertion
it carries) and is kept as the protobuf message the sidecar would have
received.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import meridian
import pytest
from meridian.operations import Operations
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.v1 import sidecar_pb2

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
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
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
            # The conductor creates a named account and links it in one step.
            made = f"ACC-NEW-{len(self.calls)}" if params.new_account_name else ""
            return ops.LinkExternalAccountResult(
                external_account_id=params.external_account_id,
                account_id=params.account_id or made,
            )
        return ops.Published(message_id=f"M-{len(self.calls)}")

    async def report(self, *, healthy: bool, detail: str = "") -> None:
        self.reports.append((healthy, detail))

    def sent(self, name: str) -> list[Any]:
        return [params for called, params in self.calls if called == name]

    def plugin(self) -> meridian.Plugin:
        return cast(meridian.Plugin, self)


def caller_header(
    subject: str = "person-1",
    name: str = "A Person",
    deployment_admin: bool = False,
    read: Iterable[str] = (),
) -> str:
    """A Meridian-Caller header as a sidecar forwards one: `read`, the
    deployment's accounts the person may read through this plugin."""
    claims = sidecar_pb2.CallerClaims(
        subject=subject,
        display_name=name,
        deployment_admin=deployment_admin,
        read_account_ids=list(read),
    ).SerializeToString()
    assertion = sidecar_pb2.CallerAssertion(claims=claims)
    return base64.urlsafe_b64encode(assertion.SerializeToString()).decode().rstrip("=")


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


def clock(moment: datetime = NOW) -> Callable[[], datetime]:
    return lambda: moment
