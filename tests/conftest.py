"""Stand-ins for the sidecar, for SnapTrade's SDK, and for the clock.

`Sidecar` is the SDK's own generated `Operations` with the transport replaced:
every call goes through the SDK's real conversion (a Decimal to the wire's
integer and scale, a float refused) and is kept as the protobuf message the
sidecar would have received. `FutureSidecar` has the account-side contract's
parameters, as the SDK is expected to gain them, so the adapters can be shown
to switch on.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
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
    """Today's SDK operations, answered here."""

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
        return ops.Published(message_id=f"M-{len(self.calls)}")

    async def report(self, *, healthy: bool, detail: str = "") -> None:
        self.reports.append((healthy, detail))

    def sent(self, name: str) -> list[Any]:
        return [params for called, params in self.calls if called == name]

    def plugin(self) -> meridian.Plugin:
        return cast(meridian.Plugin, self)


@dataclass
class FutureSidecar:
    """The account-side contract's operations, as the SDK is expected to gain
    them (names from the in-progress protos). Keeps the keyword arguments."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reports: list[tuple[bool, str]] = field(default_factory=list)

    async def report_sync_status(
        self,
        *,
        source: str = "",
        last_synced_at_ns: int = 0,
        connection_healthy: bool = False,
        status_detail: str = "",
        observed_at_ns: int = 0,
        external_account_id: str = "",
        state: str | None = None,
        holdings_as_of_ns: int = 0,
        history_as_of_ns: int = 0,
    ) -> ops.Published:
        self.calls.append(("report_sync_status", dict(locals_without_self(locals()))))
        return ops.Published(message_id="M")

    async def record_holdings_statement(
        self,
        *,
        source: str = "",
        external_statement_id: str = "",
        as_of_date: str = "",
        read_at_ns: int = 0,
        expected_rows: int = 0,
        acting_for: str | None = None,
        buying_power: meridian.Money | None = None,
        margin_requirement: meridian.Money | None = None,
        maintenance_excess: meridian.Money | None = None,
        currency_assumed: bool = False,
    ) -> ops.RecordHoldingsStatementResult:
        self.calls.append(("record_holdings_statement", dict(locals_without_self(locals()))))
        return ops.RecordHoldingsStatementResult(statement_id="STMT-F")

    async def record_holding(
        self,
        *,
        statement_id: str = "",
        instrument_id: str = "",
        unresolved_identifiers: Sequence[ops.Identifier] = (),
        quantity: Decimal | int,
        market_value: meridian.Money | None = None,
        external_account_id: str = "",
        acting_for: str | None = None,
        side: str | None = None,
        settle_date_quantity: Decimal | None = None,
        currency_assumed: bool = False,
    ) -> ops.RecordHoldingResult:
        self.calls.append(("record_holding", dict(locals_without_self(locals()))))
        return ops.RecordHoldingResult(holding_id="H", resolved=bool(instrument_id))

    async def resolve_identifier(
        self,
        *,
        identifiers: Sequence[ops.Identifier] = (),
        as_of_ns: int = 0,
        exchange_mic: str = "",
        currency: str = "",
    ) -> ops.ResolveIdentifierResult:
        self.calls.append(("resolve_identifier", dict(locals_without_self(locals()))))
        return found()

    async def report_missing_instrument(self, **kwargs: Any) -> ops.Published:
        self.calls.append(("report_missing_instrument", kwargs))
        return ops.Published(message_id="M")

    async def report_external_accounts(
        self, *, source: str = "", accounts: Sequence[Any] = (), observed_at_ns: int = 0
    ) -> ops.Published:
        self.calls.append(("report_external_accounts", dict(locals_without_self(locals()))))
        return ops.Published(message_id="M")

    async def report(self, *, healthy: bool, detail: str = "") -> None:
        self.reports.append((healthy, detail))

    def sent(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, kwargs in self.calls if called == name]

    def plugin(self) -> meridian.Plugin:
        return cast(meridian.Plugin, self)


def locals_without_self(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key != "self"}


def caller_header(subject: str = "person-1", name: str = "A Person") -> str:
    """A Meridian-Caller header as a sidecar forwards one."""
    claims = sidecar_pb2.CallerClaims(subject=subject, display_name=name)
    assertion = sidecar_pb2.CallerAssertion(claims=claims.SerializeToString())
    return base64.urlsafe_b64encode(assertion.SerializeToString()).decode().rstrip("=")


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


def clock(moment: datetime = NOW) -> Callable[[], datetime]:
    return lambda: moment
