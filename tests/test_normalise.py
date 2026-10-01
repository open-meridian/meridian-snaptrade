"""Each normalising rule, on responses shaped as SnapTrade documents them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import meridian
import pytest

from snaptrade.normalise import (
    Identifier,
    Serving,
    Side,
    SyncState,
    asset_class,
    cash_holding,
    external_account,
    freshness,
    ns,
    position_holding,
    serving,
    statement,
    statement_id,
    to_decimal,
    views,
)
from snaptrade.venue import Snapshot, parse_exact

from conftest import NOW

STALE_AFTER = timedelta(hours=36)


def connection(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "00000000-0000-4000-8000-0000000000c1",
        "brokerage": {"slug": "ALPACA", "name": "Alpaca", "display_name": "Alpaca"},
        "name": "Connection 1",
        "type": "read",
        "disabled": False,
        "disabled_date": None,
        "data_freshness_mode": {"institution": "realtime", "snaptrade": "realtime"},
    }
    return {**base, **changes}


def account(**changes: Any) -> dict[str, Any]:
    synced = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    base: dict[str, Any] = {
        "id": "00000000-0000-4000-8000-0000000000a1",
        "brokerage_authorization": "00000000-0000-4000-8000-0000000000c1",
        "name": "Margin",
        "number": "SYN001",
        "institution_name": "Alpaca",
        "institution_account_id": "INST-1",
        "raw_type": "margin",
        "sync_status": {
            "holdings": {"initial_sync_completed": True, "last_successful_sync": synced},
            "transactions": {
                "initial_sync_completed": True,
                "last_successful_sync": "2026-09-27",
            },
        },
    }
    return {**base, **changes}


def stock(symbol: str, units: Any, **changes: Any) -> dict[str, Any]:
    instrument = {
        "kind": "stock",
        "id": "00000000-0000-4000-8000-0000000000d1",
        "symbol": symbol,
        "raw_symbol": symbol,
        "currency": "USD",
        "exchange": "XNAS",
    }
    return {
        "instrument": {**instrument, **changes.pop("instrument", {})},
        "units": units,
        **changes,
    }


def balance(code: str | None, cash: Any, buying_power: Any = None) -> dict[str, Any]:
    currency = {"id": "00000000-0000-4000-8000-0000000000e1", "code": code, "name": code}
    return {"currency": currency if code else {}, "cash": cash, "buying_power": buying_power}


# ── Numbers ──────────────────────────────────────────────────────────────────


def test_a_float_becomes_its_shortest_round_trip_form_never_its_binary_expansion() -> None:
    assert to_decimal(0.1, "x") == Decimal("0.1")
    assert str(to_decimal(0.1, "x")) == "0.1"
    assert to_decimal(1523.45, "x") == Decimal("1523.45")


def test_a_decimal_string_keeps_the_scale_it_was_written_at() -> None:
    assert str(to_decimal("12.50", "x")) == "12.50"
    assert str(to_decimal("0.012345678", "x")) == "0.012345678"


def test_json_numbers_are_read_exactly_from_their_text() -> None:
    body = parse_exact('{"cash": 0.1, "big": 12345678901234567890.123456789, "n": 3}')
    assert body["cash"] == Decimal("0.1") and isinstance(body["cash"], Decimal)
    assert body["big"] == Decimal("12345678901234567890.123456789")
    assert body["n"] == 3


@pytest.mark.parametrize("value", [None, True, "abc", float("nan"), "Infinity", [1]])
def test_what_is_not_a_finite_number_is_refused(value: Any) -> None:
    with pytest.raises(ValueError):
        to_decimal(value, "x")


# ── Accounts ─────────────────────────────────────────────────────────────────


def test_the_account_id_is_the_brokerage_and_its_own_stable_account_id() -> None:
    made = external_account(account(), connection())
    assert made.external_account_id == "ALPACA:INST-1"
    assert made.stable


def test_one_real_account_through_two_connections_is_one_account() -> None:
    first = external_account(account(), connection())
    again = external_account(
        account(id="00000000-0000-4000-8000-0000000000a9", brokerage_authorization="other"),
        connection(id="other"),
    )
    assert first.external_account_id == again.external_account_id
    assert first.snaptrade_account_id != again.snaptrade_account_id


def test_without_an_institution_account_id_the_id_is_snaptrades_and_said_to_be_unstable() -> (
    None
):
    made = external_account(account(institution_account_id=None), connection())
    assert made.external_account_id == "snaptrade:00000000-0000-4000-8000-0000000000a1"
    assert not made.stable


def test_the_venues_account_type_is_carried_verbatim_and_the_name_falls_back() -> None:
    made = external_account(account(raw_type="INDIVIDUAL", name=None), connection())
    assert made.account_type == "INDIVIDUAL"
    assert made.name == "Alpaca"


# ── Holdings ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("U1234567", "U1234567"),
        ("  SYN-0001  ", "SYN-0001"),
        # Masked: part of the number is hidden, so it matches nothing.
        ("****3003", ""),
        ("•••• 3003", ""),
        (None, ""),
        (12345, ""),
    ],
)
def test_the_account_number_is_kept_whole_or_not_at_all(given: Any, kept: str) -> None:
    # What the Account links tab matches an account to one of the
    # deployment's by, besides its name.
    assert external_account(account(number=given), connection()).number == kept


def test_a_long_position_is_a_long_row_with_no_market_value() -> None:
    row = position_holding(
        stock(
            "AAPL",
            "12.5",
            price="231.40",
            instrument={"figi_instrument": {"figi_code": "BBG000B9XRY4"}},
        ),
        "USD",
    )
    assert row.side is Side.LONG
    assert row.quantity == Decimal("12.5")
    assert row.identifiers == (
        Identifier("figi", "BBG000B9XRY4"),
        Identifier("symbol", "AAPL", "snaptrade"),
    )
    # SnapTrade reports none, and price times units is never made into one.
    assert row.market_value is None
    assert row.settle_date_quantity is None
    assert row.exchange_mic == "XNAS"


def test_a_short_position_is_a_short_row_with_a_negative_quantity() -> None:
    row = position_holding(stock("ZZTOP", "-40"), "USD")
    assert row.side is Side.SHORT
    assert row.quantity == Decimal("-40")


def test_an_option_is_named_by_its_occ_symbol_as_snaptrades_own() -> None:
    option = {
        "instrument": {
            "kind": "option",
            "id": "00000000-0000-4000-8000-0000000000d3",
            "symbol": "AAPL  261218C00250000",
            "option_type": "CALL",
            "strike_price": "250",
            "expiration_date": "2026-12-18",
            "multiplier": "100",
            "underlying": {"kind": "stock", "symbol": "AAPL"},
        },
        "units": "-2",
        "currency": "USD",
    }
    row = position_holding(option, "USD")
    assert row.identifiers == (Identifier("symbol", "AAPL  261218C00250000", "snaptrade"),)
    assert row.side is Side.SHORT and row.kind == "option"


# Every kind SnapTrade documents for `instrument.kind` in `positions/all`
# (its API spec and SDK 13.0.27's instrument models), and what each is sent as.
ASSET_CLASSES = [
    ("stock", "equity"),
    ("etf", "fund"),
    ("mutualfund", "fund"),
    ("bond", "debt"),
    ("option", "derivative"),
    ("crypto", "crypto_asset"),
    ("adr", "equity"),
    ("cef", "fund"),
    ("future", "derivative"),
    ("future_option", "derivative"),
    ("cfd", "derivative"),
    # Left for a person: a token's class is what it stands for.
    ("tokenized_asset", ""),
    ("other", ""),
    # A kind SnapTrade does not document, a spelling it does not use, none.
    ("warrant", ""),
    ("STOCK", ""),
    ("", ""),
]


@pytest.mark.parametrize(("kind", "said"), ASSET_CLASSES)
def test_each_snaptrade_kind_is_the_ruled_asset_class_or_none(kind: str, said: str) -> None:
    assert asset_class(kind) == said
    row = position_holding(stock("X", "1", instrument={"kind": kind}), "USD")
    assert (row.kind, row.asset_class) == (kind, said)


def test_each_asset_class_sent_is_one_the_sdk_defines() -> None:
    for _, said in ASSET_CLASSES:
        if said:
            assert meridian.AssetClass.Value(f"ASSET_CLASS_{said.upper()}")


def test_cash_is_a_holding_of_its_cash_instrument_in_the_cash_class() -> None:
    # The product owner, 2026-10-01: cash rows carry the cash class; no
    # position SnapTrade reports has the kind "cash", so the kinds' map keeps none.
    row = cash_holding(balance("USD", Decimal("1523.45")))
    assert row is not None and row.kind == "cash" and row.asset_class == "cash"
    assert asset_class("cash") == ""


def test_a_float_from_an_older_response_is_converted_by_repr() -> None:
    row = position_holding(stock("AAPL", 0.1), "USD")
    assert row.quantity == Decimal("0.1")


def test_the_positions_own_currency_is_the_venues_and_not_assumed() -> None:
    row = position_holding(stock("SAP.DE", "30", currency="EUR"), "USD")
    assert (row.currency, row.currency_assumed) == ("EUR", False)


def test_a_currency_the_venue_did_not_state_for_the_position_is_marked_assumed() -> None:
    listed_only = position_holding(stock("SAP.DE", "30", instrument={"currency": "EUR"}), "USD")
    assert (listed_only.currency, listed_only.currency_assumed) == ("EUR", True)
    unstated = position_holding(stock("SYNX", "7", instrument={"currency": None}), "CAD")
    assert (unstated.currency, unstated.currency_assumed) == ("CAD", True)


def test_an_exchange_code_that_is_not_a_mic_is_not_passed_as_one() -> None:
    assert (
        position_holding(
            stock("VAB.TO", "1", instrument={"exchange": "TSX"}), "CAD"
        ).exchange_mic
        == ""
    )


def test_cash_is_a_holding_of_its_currencys_cash_instrument() -> None:
    row = cash_holding(balance("USD", Decimal("1523.45")))
    assert row is not None
    assert row.identifiers == (Identifier("iso4217", "USD"),)
    assert (row.kind, row.side, row.quantity) == ("cash", Side.LONG, Decimal("1523.45"))
    # The cash itself is what SnapTrade reported, in its own currency.
    assert row.market_value == meridian.Money(Decimal("1523.45"), "USD")
    assert not row.currency_assumed


def test_negative_cash_is_a_short_row_of_the_currency() -> None:
    row = cash_holding(balance("USD", Decimal("-120.55")))
    assert row is not None and row.side is Side.SHORT and row.quantity == Decimal("-120.55")


def test_a_balance_with_no_currency_is_never_recorded_against_a_guess() -> None:
    with pytest.raises(ValueError, match="ISO 4217"):
        cash_holding(balance(None, Decimal("5")))
    with pytest.raises(ValueError):
        cash_holding({"currency": {"code": "BASE"}, "cash": Decimal("5")})


def test_a_balance_with_no_cash_figure_is_no_row() -> None:
    assert cash_holding(balance("USD", None)) is None


# ── Statements ───────────────────────────────────────────────────────────────


def made(positions: list[dict[str, Any]], balances: list[dict[str, Any]]) -> Any:
    acct = external_account(account(), connection())
    fresh = freshness(account(), connection(), NOW, STALE_AFTER)
    return statement(
        acct,
        {"results": positions, "data_freshness": {"as_of": "2026-09-28T14:55:00Z"}},
        balances,
        NOW,
        fresh,
    )


def test_a_statement_carries_positions_and_cash_in_several_currencies() -> None:
    result, problems = made(
        [stock("AAPL", "12.5"), stock("ZZTOP", "-40")],
        [
            balance("USD", Decimal("1523.45"), Decimal("3046.90")),
            balance("CAD", Decimal("200.00"), Decimal("200.00")),
        ],
    )
    assert problems == ()
    kinds = [(row.kind, row.currency, row.side) for row in result.holdings]
    assert kinds == [
        ("stock", "USD", Side.LONG),
        ("stock", "USD", Side.SHORT),
        ("cash", "USD", Side.LONG),
        ("cash", "CAD", Side.LONG),
    ]
    assert result.buying_power == (
        meridian.Money(Decimal("3046.90"), "USD"),
        meridian.Money(Decimal("200.00"), "CAD"),
    )
    assert result.as_of_date == "2026-09-28"


def test_the_statement_id_is_made_from_the_account_and_the_read_time() -> None:
    result, _ = made([], [])
    assert result.external_statement_id == statement_id("ALPACA:INST-1", NOW)
    assert result.external_statement_id == f"snaptrade:ALPACA:INST-1:{ns(NOW)}"
    assert result.read_at_ns == ns(NOW)
    later, _ = statement(
        external_account(account(), connection()),
        {"results": []},
        [],
        NOW + timedelta(seconds=1),
        freshness(account(), connection(), NOW, STALE_AFTER),
    )
    assert later.external_statement_id != result.external_statement_id


def test_one_row_per_instrument_and_side() -> None:
    result, _ = made([stock("AAPL", "10"), stock("AAPL", "2.5"), stock("AAPL", "-3")], [])
    rows = [(row.side, row.quantity) for row in result.holdings]
    assert rows == [(Side.LONG, Decimal("12.5")), (Side.SHORT, Decimal("-3"))]


def test_a_money_market_fund_counted_in_cash_is_kept_and_marked() -> None:
    fund = stock("SYNXX", "500.00", cash_equivalent=True, instrument={"kind": "mutualfund"})
    result, _ = made([fund], [balance("USD", Decimal("1523.45"))])
    assert result.holdings[0].cash_equivalent
    assert len(result.holdings) == 2


def test_a_row_that_cannot_be_read_is_said_and_the_rest_are_kept() -> None:
    result, problems = made(
        [stock("AAPL", "abc"), stock("MSFT", "1")], [balance(None, Decimal("1"))]
    )
    assert [row.identifiers[-1].value for row in result.holdings] == ["MSFT"]
    assert len(problems) == 2


def test_the_position_currency_falls_back_to_the_accounts_only_cash_currency() -> None:
    unstated = stock("SYNX", "7", instrument={"currency": None})
    result, _ = made([unstated], [balance("CAD", Decimal("1"))])
    assert (result.holdings[0].currency, result.holdings[0].currency_assumed) == ("CAD", True)


# ── Sync state and freshness ─────────────────────────────────────────────────


def synced(hours_ago: float, **holdings: Any) -> dict[str, Any]:
    moment = (NOW - timedelta(hours=hours_ago)).isoformat()
    return account(
        sync_status={
            "holdings": {
                "initial_sync_completed": True,
                "last_successful_sync": moment,
                **holdings,
            },
            "transactions": {"last_successful_sync": "2026-09-26"},
        }
    )


def test_recent_is_current_with_holdings_and_history_freshness_apart() -> None:
    fresh = freshness(synced(1), connection(), NOW, STALE_AFTER)
    assert fresh.state is SyncState.CURRENT and fresh.healthy
    assert fresh.holdings_as_of == NOW - timedelta(hours=1)
    assert fresh.history_as_of == date(2026, 9, 26)


def test_older_than_the_setting_is_stale_since_then() -> None:
    fresh = freshness(synced(40), connection(), NOW, STALE_AFTER)
    assert fresh.state is SyncState.STALE and not fresh.healthy
    assert fresh.holdings_as_of == NOW - timedelta(hours=40)


def test_a_disabled_connection_is_disabled_though_it_still_serves() -> None:
    fresh = freshness(
        synced(1),
        connection(disabled=True, disabled_date="2026-09-23T10:00:00Z"),
        NOW,
        STALE_AFTER,
    )
    assert fresh.state is SyncState.DISABLED
    assert "2026-09-23" in fresh.detail
    assert fresh.holdings_as_of == NOW - timedelta(hours=1)


def test_interactive_brokers_through_snaptrade_is_delayed_by_design() -> None:
    ibkr = connection(
        brokerage={"slug": "INTERACTIVE-BROKERS-FLEX", "name": "Interactive Brokers"},
        data_freshness_mode={"institution": "delayed", "snaptrade": "delayed"},
    )
    fresh = freshness(synced(40), ibkr, NOW, STALE_AFTER)
    assert fresh.state is SyncState.DELAYED_BY_DESIGN and fresh.healthy


def test_without_a_freshness_mode_the_brokerage_says_whether_it_is_late_by_design() -> None:
    ibkr = connection(brokerage={"slug": "INTERACTIVE-BROKERS-FLEX"})
    del ibkr["data_freshness_mode"]
    assert freshness(synced(30), ibkr, NOW, STALE_AFTER).state is SyncState.DELAYED_BY_DESIGN


def test_late_by_design_and_later_than_that_is_stale() -> None:
    ibkr = connection(data_freshness_mode={"institution": "delayed", "snaptrade": "delayed"})
    assert freshness(synced(24 * 5), ibkr, NOW, STALE_AFTER).state is SyncState.STALE


def test_no_successful_sync_is_stale() -> None:
    raw = account(
        sync_status={"holdings": {"initial_sync_completed": True, "last_successful_sync": None}}
    )
    fresh = freshness(raw, connection(), NOW, STALE_AFTER)
    assert fresh.state is SyncState.STALE and fresh.holdings_as_of is None


# ── A whole read ─────────────────────────────────────────────────────────────


def snapshot(accounts: list[dict[str, Any]], connections: list[dict[str, Any]]) -> Snapshot:
    positions = {a["id"]: {"results": [stock("AAPL", "1")]} for a in accounts}
    balances = {a["id"]: [balance("USD", Decimal("1"))] for a in accounts}
    return Snapshot(NOW, connections, accounts, positions, balances)


def test_a_first_sync_not_finished_records_nothing_and_says_why() -> None:
    raw = synced(1, initial_sync_completed=False)
    (view,) = views(snapshot([raw], [connection()]), STALE_AFTER)[0].accounts
    assert view.statement is None and "first sync" in view.withheld
    assert view.freshness.state is SyncState.STALE


def test_holdings_the_brokerage_does_not_expose_are_not_recorded_as_empty() -> None:
    raw = synced(1, holdings_unavailable=True)
    (view,) = views(snapshot([raw], [connection()]), STALE_AFTER)[0].accounts
    assert view.statement is None and "does not show" in view.withheld


def test_an_account_that_could_not_be_read_is_withheld_with_the_reason() -> None:
    raw = account()
    read = Snapshot(
        NOW, [connection()], [raw], failures={raw["id"]: "reading positions failed"}
    )
    (view,) = views(read, STALE_AFTER)[0].accounts
    assert view.statement is None and view.withheld == "reading positions failed"


def test_a_connection_shows_the_worst_of_its_accounts() -> None:
    healthy = account()
    old = account(
        id="00000000-0000-4000-8000-0000000000a2",
        institution_account_id="INST-2",
        sync_status=synced(48)["sync_status"],
    )
    (seen,) = views(snapshot([healthy, old], [connection()]), STALE_AFTER)
    assert seen.state is SyncState.STALE and len(seen.accounts) == 2


def test_an_account_whose_connection_was_not_listed_is_still_shown() -> None:
    (seen,) = views(snapshot([account()], []), STALE_AFTER)
    assert seen.connection_id == "00000000-0000-4000-8000-0000000000c1"
    assert len(seen.accounts) == 1
    # Nothing says how SnapTrade serves it.
    assert seen.serving is Serving.UNKNOWN


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ({"institution": "realtime", "snaptrade": "realtime"}, Serving.REAL_TIME),
        ({"institution": "delayed", "snaptrade": "delayed"}, Serving.DELAYED),
        # The two are independent: SnapTrade may cache a real-time brokerage.
        ({"institution": "realtime", "snaptrade": "delayed"}, Serving.DELAYED),
        ({"institution": "delayed", "snaptrade": "realtime"}, Serving.REAL_TIME),
        ({"institution": "realtime"}, Serving.UNKNOWN),
        ({"institution": "realtime", "snaptrade": "REALTIME"}, Serving.UNKNOWN),
        ({"institution": "realtime", "snaptrade": None}, Serving.UNKNOWN),
        (None, Serving.UNKNOWN),
        ("realtime", Serving.UNKNOWN),
    ],
)
def test_how_snaptrade_serves_a_connection_is_its_data_freshness_mode(
    mode: Any, expected: Serving
) -> None:
    assert serving(connection(data_freshness_mode=mode)) is expected


def test_a_connection_carries_how_snaptrade_serves_it() -> None:
    delayed = connection(data_freshness_mode={"institution": "delayed", "snaptrade": "delayed"})
    (seen,) = views(snapshot([account()], [delayed]), STALE_AFTER)
    assert seen.serving is Serving.DELAYED
    absent = connection()
    del absent["data_freshness_mode"]
    (seen,) = views(snapshot([account()], [absent]), STALE_AFTER)
    assert seen.serving is Serving.UNKNOWN


def test_read_time_is_in_utc_nanoseconds_exactly() -> None:
    assert ns(datetime(1970, 1, 1, 0, 0, 1, 5, tzinfo=UTC)) == 1_000_005_000
