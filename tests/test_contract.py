"""What reaches the sidecar: through today's SDK, and through the account-side
contract's operations once the SDK has them."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import meridian
from meridian.plugin.v1 import operations_pb2 as ops

from snaptrade.contract import Contract, Recorder, is_administrator
from snaptrade.normalise import (
    ExternalAccount,
    Freshness,
    Holding,
    Identifier,
    Side,
    Statement,
    SyncState,
    ns,
)

from conftest import NOW, FutureSidecar, Sidecar, ambiguous, found

ACCOUNT = ExternalAccount(
    external_account_id="ALPACA:INST-1",
    stable=True,
    name="Margin",
    account_type="margin",
    institution="Alpaca",
    connection_id="c1",
    snaptrade_account_id="a1",
)

LONG = Holding(
    identifiers=(Identifier("figi", "BBG000B9XRY4"), Identifier("symbol", "AAPL", "snaptrade")),
    description="Apple",
    kind="stock",
    side=Side.LONG,
    quantity=Decimal("12.50"),
    currency="USD",
    exchange_mic="XNAS",
)
SHORT = Holding(
    identifiers=(Identifier("symbol", "ZZTOP", "snaptrade"),),
    description="ZZTOP",
    kind="stock",
    side=Side.SHORT,
    quantity=Decimal("-40"),
    currency="USD",
    currency_assumed=True,
)
CASH = Holding(
    identifiers=(Identifier("iso4217", "CAD"),),
    description="CAD cash",
    kind="cash",
    side=Side.LONG,
    quantity=Decimal("200.00"),
    currency="CAD",
    market_value=meridian.Money(Decimal("200.00"), "CAD"),
)
STATEMENT = Statement(
    external_statement_id=f"snaptrade:ALPACA:INST-1:{ns(NOW)}",
    as_of_date="2026-09-28",
    read_at_ns=ns(NOW),
    holdings=(LONG, SHORT, CASH),
    buying_power=(meridian.Money(Decimal("3046.90"), "USD"),),
)
DISABLED = Freshness(
    SyncState.DISABLED, NOW - timedelta(days=5), NOW.date(), "SnapTrade disabled it."
)


# ── Which parts the SDK carries ──────────────────────────────────────────────


def test_todays_sdk_carries_none_of_the_account_side_contract(sidecar: Sidecar) -> None:
    contract = Contract.of(sidecar)
    assert contract == Contract()
    assert len(contract.waiting()) == 7


def test_each_part_switches_on_when_the_sdk_has_it() -> None:
    contract = Contract.of(FutureSidecar())
    assert contract == Contract(True, True, True, True, True, True, True)
    assert contract.waiting() == ()


def test_the_admin_page_waits_for_the_caller_to_say_who_administers() -> None:
    caller = meridian.Caller(subject="s", display_name="d", access=(), header="h")
    assert not is_administrator(caller)
    assert is_administrator(SimpleNamespace(administrator=True))  # type: ignore[arg-type]
    assert not is_administrator(SimpleNamespace(administrator="yes"))  # type: ignore[arg-type]


# ── Through today's SDK ──────────────────────────────────────────────────────


async def test_a_statement_is_opened_with_its_row_count_and_every_row_recorded() -> None:
    sidecar = Sidecar()
    outcome = await Recorder(sidecar.plugin(), Contract.of(sidecar)).record(
        ACCOUNT, STATEMENT, ns(NOW)
    )
    (opened,) = sidecar.sent("RecordHoldingsStatement")
    assert opened.source == "snaptrade"
    assert opened.external_statement_id == STATEMENT.external_statement_id
    assert opened.expected_rows == 3
    assert opened.as_of_date == "2026-09-28"
    rows = sidecar.sent("RecordHolding")
    assert [meridian.as_decimal(row.quantity) for row in rows] == [
        Decimal("12.50"),
        Decimal("-40"),
        Decimal("200.00"),
    ]
    # The scale it was stated with crosses the wire: 12.50 is 1250 at two places.
    assert (rows[0].quantity.low, rows[0].quantity.scale) == (1250, 2)
    assert {row.external_account_id for row in rows} == {"ALPACA:INST-1"}
    assert {row.statement_id for row in rows} == {"STMT-1"}
    assert outcome.recorded == 3 and not outcome.stopped


async def test_resolution_is_dated_to_the_statement_and_qualified() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    first = sidecar.sent("ResolveIdentifier")[0]
    assert [(i.scheme, i.value, i.source) for i in first.identifiers] == [
        ("figi", "BBG000B9XRY4", ""),
        ("symbol", "AAPL", "snaptrade"),
    ]
    assert first.as_of_ns == ns(NOW.replace(hour=0))
    assert (first.exchange_mic, first.currency) == ("XNAS", "USD")
    cash = sidecar.sent("ResolveIdentifier")[2]
    assert [(i.scheme, i.value) for i in cash.identifiers] == [("iso4217", "CAD")]


async def test_until_it_can_be_unset_a_market_value_not_reported_is_sent_as_zero() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    values = [meridian.as_money(row.market_value) for row in sidecar.sent("RecordHolding")]
    assert values == [
        meridian.Money(Decimal("0"), "USD"),
        meridian.Money(Decimal("0"), "USD"),
        meridian.Money(Decimal("200.00"), "CAD"),
    ]


async def test_a_placeholder_is_recorded_against_and_counted() -> None:
    sidecar = Sidecar(resolve=lambda p: found("LCL-1", placeholder=True))
    outcome = await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert {row.instrument_id for row in sidecar.sent("RecordHolding")} == {"LCL-1"}
    assert outcome.placeholders == 3
    # The instrument store reports a not-found; the connector does not.
    assert sidecar.sent("ReportMissingInstrument") == []


async def test_an_ambiguous_row_is_still_recorded_and_its_miss_published_once() -> None:
    sidecar = Sidecar(
        resolve=lambda p: ambiguous() if p.identifiers[0].value == "ZZTOP" else found()
    )
    outcome = await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    rows = sidecar.sent("RecordHolding")
    assert len(rows) == 3
    assert rows[1].instrument_id == ""
    assert [(i.scheme, i.value) for i in rows[1].unresolved_identifiers] == [
        ("symbol", "ZZTOP")
    ]
    (miss,) = sidecar.sent("ReportMissingInstrument")
    assert miss.reason == ops.MISS_REASON_AMBIGUOUS and miss.source == "snaptrade"
    assert outcome.ambiguous == 1


async def test_a_redelivered_statement_records_no_rows() -> None:
    sidecar = Sidecar(already_recorded=True)
    outcome = await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.already_recorded and sidecar.sent("RecordHolding") == []


async def test_a_refused_row_stops_the_statement_and_is_not_retried() -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "RecordHolding":
            return meridian.CallFailed("RecordHolding", "refused", "no link for ALPACA:INST-1")
        return None

    sidecar = Sidecar(refuse=refuse)
    outcome = await Recorder(sidecar.plugin(), Contract()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.recorded == 0 and "no link" in outcome.stopped
    assert len(sidecar.sent("RecordHoldingsStatement")) == 1


async def test_sync_status_says_its_state_in_the_only_words_today_has() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin(), Contract()).report_sync(ACCOUNT, DISABLED, ns(NOW))
    (sent,) = sidecar.sent("ReportSyncStatus")
    assert not sent.connection_healthy
    assert sent.status_detail == "disabled: SnapTrade disabled it."
    assert sent.last_synced_at_ns == ns(NOW - timedelta(days=5))
    assert sent.external_account_id == "ALPACA:INST-1"


async def test_delayed_by_design_is_healthy() -> None:
    sidecar = Sidecar()
    late = Freshness(SyncState.DELAYED_BY_DESIGN, NOW, NOW.date(), "")
    await Recorder(sidecar.plugin(), Contract()).report_sync(ACCOUNT, late, ns(NOW))
    (sent,) = sidecar.sent("ReportSyncStatus")
    assert sent.connection_healthy and sent.status_detail == "delayed_by_design"


async def test_the_accounts_are_not_reported_until_the_sdk_can(sidecar: Sidecar) -> None:
    assert not await Recorder(sidecar.plugin(), Contract()).report_accounts([ACCOUNT], 1)
    assert sidecar.calls == []


# ── Through the account-side contract ────────────────────────────────────────


async def test_with_the_contract_a_row_says_its_side_and_leaves_value_unset() -> None:
    future = FutureSidecar()
    await Recorder(future.plugin(), Contract.of(future)).record(ACCOUNT, STATEMENT, ns(NOW))
    rows = future.sent("record_holding")
    assert [row["side"] for row in rows] == [
        "HOLDING_SIDE_LONG",
        "HOLDING_SIDE_SHORT",
        "HOLDING_SIDE_LONG",
    ]
    assert [row["market_value"] for row in rows] == [
        None,
        None,
        meridian.Money(Decimal("200.00"), "CAD"),
    ]
    assert [row["currency_assumed"] for row in rows] == [False, True, False]
    # SnapTrade reports no settle-date quantity, so none is sent.
    assert {row["settle_date_quantity"] for row in rows} == {None}
    (opened,) = future.sent("record_holdings_statement")
    assert opened["buying_power"] == meridian.Money(Decimal("3046.90"), "USD")


async def test_with_the_contract_buying_power_in_several_currencies_is_not_summed() -> None:
    future = FutureSidecar()
    several = Statement(
        STATEMENT.external_statement_id,
        "2026-09-28",
        ns(NOW),
        (),
        (meridian.Money(Decimal("1"), "USD"), meridian.Money(Decimal("2"), "CAD")),
    )
    await Recorder(future.plugin(), Contract.of(future)).record(ACCOUNT, several, ns(NOW))
    (opened,) = future.sent("record_holdings_statement")
    assert opened["buying_power"] is None


async def test_with_the_contract_sync_status_carries_a_state_and_both_freshnesses() -> None:
    future = FutureSidecar()
    await Recorder(future.plugin(), Contract.of(future)).report_sync(ACCOUNT, DISABLED, ns(NOW))
    (sent,) = future.sent("report_sync_status")
    assert sent["state"] == "SYNC_STATE_DISABLED"
    assert sent["holdings_as_of_ns"] == ns(NOW - timedelta(days=5))
    assert sent["history_as_of_ns"] == ns(NOW.replace(hour=0))
    assert sent["status_detail"] == "SnapTrade disabled it."


async def test_with_the_contract_the_accounts_a_connection_reaches_are_reported() -> None:
    future = FutureSidecar()
    assert await Recorder(future.plugin(), Contract.of(future)).report_accounts([ACCOUNT], 7)
    (sent,) = future.sent("report_external_accounts")
    assert sent["accounts"] == [
        {"external_account_id": "ALPACA:INST-1", "name": "Margin", "account_type": "margin"}
    ]


# ── The tripwire for when the SDK lands ──────────────────────────────────────

# Every parameter of the pinned SDK's operations this plugin knows: today's,
# and the account-side contract's as contract.py names them. A parameter
# outside this set is one the contract added under a name the adapters do not
# use, which would leave its part silently off.
KNOWN_PARAMETERS = {
    "report_sync_status": {
        "source",
        "last_synced_at_ns",
        "connection_healthy",
        "status_detail",
        "observed_at_ns",
        "external_account_id",
        "state",
        "holdings_as_of_ns",
        "history_as_of_ns",
    },
    "record_holdings_statement": {
        "source",
        "external_statement_id",
        "as_of_date",
        "read_at_ns",
        "expected_rows",
        "acting_for",
        "buying_power",
        "margin_requirement",
        "maintenance_excess",
        "currency_assumed",
    },
    "record_holding": {
        "statement_id",
        "instrument_id",
        "unresolved_identifiers",
        "quantity",
        "market_value",
        "external_account_id",
        "acting_for",
        "side",
        "settle_date_quantity",
        "currency_assumed",
    },
    "resolve_identifier": {"identifiers", "as_of_ns", "exchange_mic", "currency"},
    "report_missing_instrument": {
        "source",
        "asset_class",
        "identifiers",
        "as_of_ns",
        "reason",
        "observed_at_ns",
    },
    "report_external_accounts": {"source", "accounts", "observed_at_ns"},
}


def test_the_pinned_sdk_has_no_operation_or_parameter_this_plugin_does_not_know() -> None:
    import inspect

    from meridian.operations import Operations

    operations = {
        name
        for name, member in vars(Operations).items()
        if inspect.iscoroutinefunction(member) and not name.startswith("_")
    }
    unknown_operations = operations - set(KNOWN_PARAMETERS)
    assert not unknown_operations, f"map these in contract.py: {unknown_operations}"
    for name in operations:
        parameters = set(inspect.signature(getattr(Operations, name)).parameters) - {"self"}
        unknown = parameters - KNOWN_PARAMETERS[name]
        assert not unknown, f"{name} has parameters contract.py does not map: {unknown}"
