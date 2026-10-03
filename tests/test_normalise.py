"""Each normalising rule, on responses shaped as SnapTrade documents them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import meridian
import pytest

from snaptrade.normalise import (
    Identifier,
    Lot,
    Serving,
    Side,
    SyncState,
    Withheld,
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
    wire_decimal,
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


def test_what_the_wire_carries_exactly_is_kept_and_what_it_does_not_is_refused() -> None:
    assert format(wire_decimal("0.000000000000000001", "x"), "f") == "0.000000000000000001"
    assert wire_decimal("9" * 38, "x") == Decimal("9" * 38)
    with pytest.raises(ValueError, match="19 decimal places"):
        wire_decimal("0.0000000000000000001", "x")
    with pytest.raises(ValueError, match="38 digits"):
        wire_decimal("1" + "0" * 38, "x")
    with pytest.raises(ValueError, match="38 digits"):
        wire_decimal(Decimal("1E+38"), "x")


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


def test_a_currency_neither_the_position_nor_its_listing_states_is_marked_assumed() -> None:
    # The listing's currency is SnapTrade's own statement of it (contract v11).
    listed_only = position_holding(stock("SAP.DE", "30", instrument={"currency": "EUR"}), "USD")
    assert (listed_only.currency, listed_only.currency_assumed) == ("EUR", False)
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


def made(
    positions: list[dict[str, Any]],
    balances: list[dict[str, Any]],
    total: Any = None,
    activities: list[dict[str, Any]] | None = None,
) -> Any:
    """A statement, its activities read and none pending unless given."""
    acct = external_account(account(), connection())
    fresh = freshness(account(), connection(), NOW, STALE_AFTER)
    return statement(
        acct,
        {"results": positions, "data_freshness": {"as_of": "2026-09-28T14:55:00Z"}},
        balances,
        NOW,
        fresh,
        total,
        [] if activities is None else activities,
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


def test_a_money_market_fund_counted_in_cash_is_kept_and_the_cash_sent_net_of_it() -> None:
    fund = stock(
        "SYNXX", "500.00", price="1.00", cash_equivalent=True, instrument={"kind": "mutualfund"}
    )
    result, _ = made([fund], [balance("USD", Decimal("1523.45"))])
    held, cash = result.holdings
    assert held.cash_equivalent and held.quantity == Decimal("500.00")
    assert cash.quantity == Decimal("1023.45")
    # Written to the cash's own places, as SnapTrade wrote the cash: exact.
    assert str(cash.quantity) == "1023.45"
    assert cash.market_value == meridian.Money(Decimal("1023.45"), "USD")
    assert {(c.field, c.kind) for c in cash.closed} >= {
        ("quantity", "derived"),
        ("market_value", "derived"),
    }
    assert result.netted[0].gross == Decimal("1523.45")


def test_a_fund_counted_in_cash_with_no_price_withholds_the_statement() -> None:
    fund = stock("SYNXX", "500.00", cash_equivalent=True, instrument={"kind": "mutualfund"})
    with pytest.raises(Withheld, match="no price for SYNXX"):
        made([fund], [balance("USD", Decimal("1523.45"))])


def test_a_fund_worth_more_than_its_cash_withholds_the_statement() -> None:
    fund = stock(
        "SYNXX", "500.00", price="1.00", cash_equivalent=True, instrument={"kind": "mutualfund"}
    )
    with pytest.raises(Withheld, match="worth more than that cash"):
        made([fund], [balance("USD", Decimal("100.00"))])


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


# ── Cost and lots ────────────────────────────────────────────────────────────


def lot(quantity: Any = "10", **changes: Any) -> dict[str, Any]:
    """A `tax_lots` entry as SnapTrade's TaxLot model has it."""
    base: dict[str, Any] = {
        "original_purchase_date": "2024-03-11T14:30:00.000Z",
        "quantity": quantity,
        "purchased_price": "189.00",
        "cost_basis": "1890.00",
        "current_value": "2314.00",
        "position_type": "LONG",
        "lot_id": "L-1",
    }
    return {**base, **changes}


def held(position: dict[str, Any], fallback: str = "USD") -> tuple[Any, list[str]]:
    problems: list[str] = []
    return position_holding(position, fallback, problems), problems


def test_the_average_cost_is_snaptrades_per_unit_figure_as_reported() -> None:
    row, problems = held(stock("AAPL", "12.5", cost_basis="198.10"))
    assert row.average_cost == meridian.Money(Decimal("198.10"), "USD")
    assert str(row.average_cost.amount) == "198.10" and problems == []


def test_an_options_average_cost_is_per_share_as_reported_never_multiplied() -> None:
    option = stock(
        "AAPL  261218C00250000",
        "2",
        cost_basis="8.75",
        instrument={"kind": "option", "multiplier": "100"},
    )
    row, _ = held(option)
    assert row.average_cost == meridian.Money(Decimal("8.75"), "USD")


def test_the_average_cost_is_in_the_rows_currency_assumed_or_not() -> None:
    row, _ = held(stock("SYNX", "7", cost_basis="3.10", instrument={"currency": None}), "CAD")
    assert row.average_cost == meridian.Money(Decimal("3.10"), "CAD")
    assert row.currency_assumed


def test_no_average_cost_reported_is_none() -> None:
    row, problems = held(stock("SYNX", "7", cost_basis=None))
    assert row.average_cost is None and problems == []
    assert held(stock("SYNX", "7"))[0].average_cost is None


@pytest.mark.parametrize("given", ["abc", "0.0000000000000000001", True])
def test_an_average_cost_not_read_exactly_is_not_sent_and_said(given: Any) -> None:
    row, problems = held(stock("AAPL", "12.5", cost_basis=given))
    assert row.average_cost is None and row.quantity == Decimal("12.5")
    (problem,) = problems
    assert "the average cost of AAPL" in problem


def test_lots_are_read_exactly_in_snaptrades_order() -> None:
    row, problems = held(
        stock(
            "AAPL",
            "12.5",
            tax_lots=[
                lot("10"),
                lot(
                    "2.500",
                    cost_basis="586.25",
                    original_purchase_date="2025-06-02T15:00:00.000Z",
                ),
                lot("0.012345678", cost_basis=Decimal("1.5")),
            ],
        )
    )
    assert problems == []
    assert row.lots == (
        Lot(Decimal("10"), meridian.Money(Decimal("1890.00"), "USD"), "2024-03-11"),
        Lot(Decimal("2.500"), meridian.Money(Decimal("586.25"), "USD"), "2025-06-02"),
        Lot(Decimal("0.012345678"), meridian.Money(Decimal("1.5"), "USD"), "2024-03-11"),
    )
    assert [str(each.quantity) for each in row.lots] == ["10", "2.500", "0.012345678"]


def test_lots_are_recorded_as_reported_even_when_they_do_not_add_up() -> None:
    row, problems = held(stock("SAP.DE", "30", tax_lots=[lot("20"), lot("5")]))
    assert sum(each.quantity for each in row.lots) == Decimal("25") and problems == []


def test_a_lots_purchase_date_is_its_date_part_as_written() -> None:
    # No time-zone conversion, which could move the day.
    late = lot(original_purchase_date="2024-03-11T23:30:00-05:00")
    plain = lot(original_purchase_date="2024-03-11")
    row, _ = held(stock("AAPL", "20", tax_lots=[late, plain]))
    assert [each.acquired_date for each in row.lots] == ["2024-03-11", "2024-03-11"]


def test_a_lots_cost_and_date_not_reported_are_unset_never_made_up() -> None:
    row, problems = held(
        stock("BTC", "0.5", tax_lots=[lot("0.5", cost_basis=None, original_purchase_date=None)])
    )
    assert row.lots == (Lot(Decimal("0.5"), None, ""),) and problems == []


def test_a_lots_cost_keeps_its_sign_as_reported() -> None:
    row, _ = held(stock("AAPL", "10", tax_lots=[lot("10", cost_basis="-18.25")]))
    assert row.lots[0].cost == meridian.Money(Decimal("-18.25"), "USD")


@pytest.mark.parametrize("quantity", ["40", "-40"])
def test_a_short_holdings_lots_are_short(quantity: str) -> None:
    short = lot(quantity, position_type="SHORT", cost_basis="160.80")
    row, problems = held(stock("ZZTOP", "-40", tax_lots=[short]))
    assert row.lots == (
        Lot(Decimal("-40"), meridian.Money(Decimal("160.80"), "USD"), "2024-03-11"),
    )
    assert problems == []


@pytest.mark.parametrize("given", [None, []])
def test_no_lots_listed_is_no_lots(given: Any) -> None:
    row, problems = held(stock("AAPL", "12.5", tax_lots=given))
    assert row.lots == () and problems == []
    assert held(stock("AAPL", "12.5"))[0].lots == ()


@pytest.mark.parametrize(
    ("bad", "said"),
    [
        (lot(None), "lot 2 of AAPL has no quantity"),
        (lot("abc"), "the quantity of lot 2 of AAPL is not a number"),
        (lot("0.0000000000000000001"), "19 decimal places"),
        (lot("1", cost_basis="abc"), "the cost of lot 2 of AAPL is not a number"),
        (lot("1", position_type="SHORT"), "lot 2 of AAPL is short on a long holding"),
        (lot("-1"), "lot 2 of AAPL has a negative quantity on a long holding"),
        (lot("1", original_purchase_date="last spring"), "a purchase date that is not one"),
        (lot("1", original_purchase_date="2024-02-30T00:00:00Z"), "not one"),
        ("a lot", "lot 2 of AAPL is not an object"),
    ],
)
def test_a_lot_not_read_exactly_sends_none_of_the_holdings_lots(bad: Any, said: str) -> None:
    row, problems = held(stock("AAPL", "12.5", tax_lots=[lot("10"), bad]))
    # Never part of the list: that would show a difference that is not there.
    assert row.lots == ()
    # The row itself stands.
    assert row.quantity == Decimal("12.5") and row.side is Side.LONG
    (problem,) = problems
    assert said in problem and "none of AAPL's lots is sent" in problem


def test_lots_that_are_not_a_list_are_none_and_said() -> None:
    row, problems = held(stock("AAPL", "12.5", tax_lots={"quantity": "1"}))
    assert row.lots == () and len(problems) == 1


def test_a_statement_says_why_a_holding_has_no_lots() -> None:
    result, problems = made([stock("AAPL", "1", tax_lots=[lot(None)]), stock("MSFT", "1")], [])
    assert [row.identifiers[-1].value for row in result.holdings] == ["AAPL", "MSFT"]
    (problem,) = problems
    assert "AAPL" in problem


def test_two_rows_for_one_holding_keep_both_rows_lots_and_no_average_cost() -> None:
    result, problems = made(
        [
            stock("AAPL", "10", cost_basis="190", tax_lots=[lot("10")]),
            stock("AAPL", "2.5", cost_basis="200", tax_lots=[lot("2.5")]),
        ],
        [],
    )
    (row,) = result.holdings
    assert row.quantity == Decimal("12.5")
    assert [each.quantity for each in row.lots] == [Decimal("10"), Decimal("2.5")]
    # Two averages are never combined into one.
    assert row.average_cost is None
    (problem,) = problems
    assert "average costs are not combined" in problem


def test_two_rows_for_one_holding_with_lots_only_once_send_none() -> None:
    result, problems = made(
        [stock("AAPL", "10", tax_lots=[lot("10")]), stock("AAPL", "2.5")], []
    )
    assert result.holdings[0].lots == ()
    assert any("lots only once" in problem for problem in problems)


# ── Figures ──────────────────────────────────────────────────────────────────


def test_the_accounts_total_value_is_its_net_liquidation_exactly() -> None:
    total = parse_exact('{"amount": 9876.54, "currency": "USD"}')
    result, problems = made([], [], total)
    assert result.net_liquidation == meridian.Money(Decimal("9876.54"), "USD")
    assert str(result.net_liquidation.amount) == "9876.54" and problems == ()


def test_a_total_value_in_whole_units_is_kept_whole() -> None:
    result, _ = made([], [], {"amount": 1230, "currency": "usd"})
    assert result.net_liquidation == meridian.Money(Decimal("1230"), "USD")


@pytest.mark.parametrize("total", [None, {}, {"amount": None, "currency": "USD"}, "9876.54"])
def test_no_total_value_is_no_net_liquidation(total: Any) -> None:
    result, problems = made([], [], total)
    assert result.net_liquidation is None and problems == ()


def test_a_total_value_with_no_currency_is_no_net_liquidation_and_said() -> None:
    result, problems = made([], [], {"amount": Decimal("10"), "currency": None})
    assert result.net_liquidation is None
    (problem,) = problems
    assert "no ISO 4217 currency" in problem


def test_a_read_takes_the_net_liquidation_from_the_accounts_balance() -> None:
    raw = account(balance={"total": {"amount": Decimal("9876.54"), "currency": "USD"}})
    snapshot = Snapshot(
        NOW, [connection()], [raw], {raw["id"]: {"results": []}}, {raw["id"]: []}
    )
    (only,) = views(snapshot, STALE_AFTER)
    (view,) = only.accounts
    assert view.statement is not None
    assert view.statement.net_liquidation == meridian.Money(Decimal("9876.54"), "USD")


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


def test_holdings_the_brokerage_does_not_expose_are_holdings_unavailable() -> None:
    """The product owner, 2026-09-28 (the account side, Q4): SnapTrade's
    `holdings_unavailable` is its own state, not stale, because waiting
    changes nothing; the remedy is to connect the account another way."""
    fresh = freshness(synced(1, holdings_unavailable=True), connection(), NOW, STALE_AFTER)
    assert fresh.state is SyncState.HOLDINGS_UNAVAILABLE and not fresh.healthy
    assert "does not show" in fresh.detail
    assert fresh.remedy.startswith("Connect the account another way")
    # Freshness as SnapTrade reports it, whatever the state.
    assert fresh.holdings_as_of == NOW - timedelta(hours=1)


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


def test_holdings_unavailable_comes_before_stale_on_its_connection() -> None:
    """A person's remedy before one that is waiting: a connection with a
    stale account and one whose holdings are unavailable shows the latter."""
    old = account(sync_status=synced(48)["sync_status"])
    hidden = account(
        id="00000000-0000-4000-8000-0000000000a2",
        institution_account_id="INST-2",
        sync_status=synced(1, holdings_unavailable=True)["sync_status"],
    )
    (seen,) = views(snapshot([old, hidden], [connection()]), STALE_AFTER)
    assert seen.state is SyncState.HOLDINGS_UNAVAILABLE


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
