"""What reaches the sidecar, through the pinned SDK's operations."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import meridian
from meridian.plugin.v1 import operations_pb2 as ops

from snaptrade.contract import _SYNC_STATE, Recorder
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

from conftest import NOW, Sidecar, ambiguous, found

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
    asset_class="equity",
)
SHORT = Holding(
    identifiers=(Identifier("symbol", "ZZTOP", "snaptrade"),),
    description="ZZTOP",
    kind="stock",
    side=Side.SHORT,
    quantity=Decimal("-40"),
    currency="USD",
    currency_assumed=True,
    asset_class="equity",
)
CASH = Holding(
    identifiers=(Identifier("iso4217", "CAD"),),
    description="CAD cash",
    kind="cash",
    asset_class="cash",
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


# ── A statement and its rows ─────────────────────────────────────────────────


async def test_a_statement_is_opened_with_its_row_count_and_every_row_recorded() -> None:
    sidecar = Sidecar()
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
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
    await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    first = sidecar.sent("ResolveIdentifier")[0]
    assert [(i.scheme, i.value, i.source) for i in first.identifiers] == [
        ("figi", "BBG000B9XRY4", ""),
        ("symbol", "AAPL", "snaptrade"),
    ]
    assert first.as_of_ns == ns(NOW.replace(hour=0))
    assert (first.exchange_mic, first.currency) == ("XNAS", "USD")
    cash = sidecar.sent("ResolveIdentifier")[2]
    assert [(i.scheme, i.value) for i in cash.identifiers] == [("iso4217", "CAD")]


async def test_a_row_says_its_side_and_what_was_and_was_not_reported() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    rows = sidecar.sent("RecordHolding")
    assert [ops.HoldingSide.Name(row.side) for row in rows] == [
        "HOLDING_SIDE_LONG",
        "HOLDING_SIDE_SHORT",
        "HOLDING_SIDE_LONG",
    ]
    # A value SnapTrade did not report is left unset, never zero; cash's is its amount.
    assert [row.HasField("market_value") for row in rows] == [False, False, True]
    assert meridian.as_money(rows[2].market_value) == meridian.Money(Decimal("200.00"), "CAD")
    assert [row.currency_assumed for row in rows] == [False, True, False]
    # SnapTrade reports no settle-date quantity, so none is sent.
    assert not any(row.HasField("settle_date_quantity") for row in rows)
    assert not any(row.also_counted_in_cash for row in rows)


async def test_a_fund_snaptrade_counts_in_cash_is_marked_so() -> None:
    sidecar = Sidecar()
    fund = Holding(
        identifiers=(Identifier("symbol", "SWVXX", "snaptrade"),),
        description="Money market",
        kind="mutualfund",
        side=Side.LONG,
        quantity=Decimal("10"),
        currency="USD",
        cash_equivalent=True,
    )
    statement = Statement(STATEMENT.external_statement_id, "2026-09-28", ns(NOW), (fund,))
    await Recorder(sidecar.plugin()).record(ACCOUNT, statement, ns(NOW))
    (row,) = sidecar.sent("RecordHolding")
    assert row.also_counted_in_cash


async def test_buying_power_in_one_currency_is_on_the_statement() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    (opened,) = sidecar.sent("RecordHoldingsStatement")
    assert meridian.as_money(opened.buying_power) == meridian.Money(Decimal("3046.90"), "USD")


async def test_buying_power_in_several_currencies_is_not_summed() -> None:
    sidecar = Sidecar()
    several = Statement(
        STATEMENT.external_statement_id,
        "2026-09-28",
        ns(NOW),
        (),
        (meridian.Money(Decimal("1"), "USD"), meridian.Money(Decimal("2"), "CAD")),
    )
    await Recorder(sidecar.plugin()).record(ACCOUNT, several, ns(NOW))
    (opened,) = sidecar.sent("RecordHoldingsStatement")
    assert not opened.HasField("buying_power")


async def test_a_placeholder_is_recorded_against_and_counted() -> None:
    sidecar = Sidecar(resolve=lambda p: found("LCL-1", placeholder=True))
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert {row.instrument_id for row in sidecar.sent("RecordHolding")} == {"LCL-1"}
    assert outcome.placeholders == 3
    # The instrument store reports a not-found; the connector does not.
    assert sidecar.sent("ReportMissingInstrument") == []


async def test_an_ambiguous_row_is_still_recorded_and_its_miss_published_once() -> None:
    sidecar = Sidecar(
        resolve=lambda p: ambiguous() if p.identifiers[0].value == "ZZTOP" else found()
    )
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    rows = sidecar.sent("RecordHolding")
    assert len(rows) == 3
    assert rows[1].instrument_id == ""
    assert [(i.scheme, i.value) for i in rows[1].unresolved_identifiers] == [
        ("symbol", "ZZTOP")
    ]
    (miss,) = sidecar.sent("ReportMissingInstrument")
    assert miss.reason == ops.MISS_REASON_AMBIGUOUS and miss.source == "snaptrade"
    assert miss.asset_class == ops.ASSET_CLASS_EQUITY
    assert outcome.ambiguous == 1


async def test_a_miss_carries_the_rows_asset_class_and_none_where_it_has_none() -> None:
    sidecar = Sidecar(resolve=lambda p: ambiguous())
    await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    # The two stocks are equity; the cash is a holding of its cash instrument.
    misses = sidecar.sent("ReportMissingInstrument")
    assert [ops.AssetClass.Name(miss.asset_class) for miss in misses] == [
        "ASSET_CLASS_EQUITY",
        "ASSET_CLASS_EQUITY",
        "ASSET_CLASS_CASH",
    ]


async def test_a_redelivered_statement_records_no_rows() -> None:
    sidecar = Sidecar(already_recorded=True)
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.already_recorded and sidecar.sent("RecordHolding") == []


async def test_a_refused_row_stops_the_statement_and_is_not_retried() -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "RecordHolding":
            return meridian.CallFailed("RecordHolding", "refused", "no link for ALPACA:INST-1")
        return None

    sidecar = Sidecar(refuse=refuse)
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.recorded == 0 and "no link" in outcome.stopped
    assert len(sidecar.sent("RecordHoldingsStatement")) == 1
    # Refused for something other than a missing link.
    assert not outcome.unlinked


async def test_a_row_refused_for_want_of_a_link_says_so() -> None:
    # The SDK raises NotLinked by the refusal's code; its words are not read.
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "RecordHolding":
            return meridian.NotLinked("RecordHolding", "reworded at some release")
        return None

    sidecar = Sidecar(refuse=refuse)
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.unlinked and outcome.recorded == 0
    assert "reworded at some release" in outcome.stopped


async def test_a_refusal_saying_not_linked_without_the_code_is_not_taken_for_one() -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "RecordHolding":
            return meridian.CallFailed(
                "RecordHolding", "refused", "external account ALPACA:INST-1 is not linked"
            )
        return None

    sidecar = Sidecar(refuse=refuse)
    outcome = await Recorder(sidecar.plugin()).record(ACCOUNT, STATEMENT, ns(NOW))
    assert outcome.stopped and not outcome.unlinked


async def test_sync_status_carries_a_state_and_both_freshnesses() -> None:
    sidecar = Sidecar()
    await Recorder(sidecar.plugin()).report_sync(ACCOUNT, DISABLED, ns(NOW))
    (sent,) = sidecar.sent("ReportSyncStatus")
    assert sent.state == ops.SYNC_STATE_DISABLED
    assert not sent.connection_healthy
    assert sent.holdings_as_of_ns == sent.last_synced_at_ns == ns(NOW - timedelta(days=5))
    assert sent.history_as_of_ns == ns(NOW.replace(hour=0))
    assert sent.status_detail == "SnapTrade disabled it."
    assert sent.external_account_id == "ALPACA:INST-1"


async def test_delayed_by_design_is_healthy() -> None:
    sidecar = Sidecar()
    late = Freshness(SyncState.DELAYED_BY_DESIGN, NOW, NOW.date(), "")
    await Recorder(sidecar.plugin()).report_sync(ACCOUNT, late, ns(NOW))
    (sent,) = sidecar.sent("ReportSyncStatus")
    assert sent.connection_healthy and sent.state == ops.SYNC_STATE_DELAYED_BY_DESIGN


async def test_holdings_unavailable_is_sent_as_the_contracts_own_state() -> None:
    """Not stale: SYNC_STATE_HOLDINGS_UNAVAILABLE, ruled for exactly this
    (the product owner, 2026-09-28)."""
    sidecar = Sidecar()
    hidden = Freshness(
        SyncState.HOLDINGS_UNAVAILABLE, NOW, NOW.date(), "Not shown to SnapTrade."
    )
    await Recorder(sidecar.plugin()).report_sync(ACCOUNT, hidden, ns(NOW))
    (sent,) = sidecar.sent("ReportSyncStatus")
    assert sent.state == ops.SYNC_STATE_HOLDINGS_UNAVAILABLE
    assert not sent.connection_healthy
    assert sent.status_detail == "Not shown to SnapTrade."


def test_every_sync_state_has_the_contracts_value() -> None:
    """Each of the plugin's states is sent as the contract's value of the same
    name, so none is sent as another."""
    for state in SyncState:
        assert _SYNC_STATE[state] == ops.SyncState.Value(f"SYNC_STATE_{state.name}")


async def test_the_accounts_a_connection_reaches_are_reported(sidecar: Sidecar) -> None:
    await Recorder(sidecar.plugin()).report_accounts([ACCOUNT])
    (sent,) = sidecar.sent("ReportExternalAccounts")
    assert list(sent.accounts) == [
        meridian.ExternalAccount(
            external_account_id="ALPACA:INST-1", name="Margin", venue_account_type="margin"
        )
    ]


# ── The tripwire for the next SDK ────────────────────────────────────────────

# Every parameter of the pinned SDK's operations this plugin knows. Moving the
# pin to an SDK with an operation or a parameter outside this set fails here,
# naming it, so what the new contract adds is decided rather than missed.
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
        "also_counted_in_cash",
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
    "report_external_accounts": {"accounts"},
    "link_external_account": {
        "external_account_id",
        "account_id",
        "new_account_name",
        # Pre-filled from the venue on the Accounts tab (W6.4).
        "new_account_custodian",
        "new_account_type",
        # Known, and not sent: the admin gives an owner and a note on the
        # dashboard (W6.3), not here.
        "new_account_owner",
        "new_account_note",
        "acting_for",
    },
    "read_accounts_for_linking": {"acting_for"},
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
    assert not unknown_operations, f"decide what to do with: {unknown_operations}"
    for name in operations:
        parameters = set(inspect.signature(getattr(Operations, name)).parameters) - {"self"}
        unknown = parameters - KNOWN_PARAMETERS[name]
        assert not unknown, f"{name} has parameters this plugin does not know: {unknown}"
