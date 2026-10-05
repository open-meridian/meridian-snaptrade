"""Synthetic mode's responses are SnapTrade's shapes: each is held to
SnapTrade's own models, from its official SDK, and exercises the rules."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import meridian
from snaptrade_client.model.account import Account
from snaptrade_client.model.all_account_positions_response import (
    AllAccountPositionsResponse,
)
from snaptrade_client.model.balance import Balance
from snaptrade_client.model.brokerage_authorization import BrokerageAuthorization
from snaptrade_client.model.paginated_universal_activity import PaginatedUniversalActivity
from snaptrade_client.model.tax_lot import TaxLot

from snaptrade.normalise import Lot, Serving, Side, SyncState, views
from snaptrade.synthetic import ALPACA_MARGIN, IBKR_INDIVIDUAL, SCHWAB_BROKERAGE, SyntheticVenue
from snaptrade.venue import read

from conftest import NOW, clock

ACCOUNTS = (ALPACA_MARGIN, IBKR_INDIVIDUAL, SCHWAB_BROKERAGE)


async def test_every_response_is_valid_by_snaptrades_own_models() -> None:
    venue = SyntheticVenue(clock())
    for connection in await venue.connections():
        BrokerageAuthorization.from_openapi_data_oapg(connection, _configuration=None)
    for account in await venue.accounts():
        Account.from_openapi_data_oapg(account, _configuration=None)
    lots = 0
    for account_id in ACCOUNTS:
        positions = await venue.positions(account_id)
        AllAccountPositionsResponse.from_openapi_data_oapg(positions, _configuration=None)
        for position in positions["results"]:
            for lot in position.get("tax_lots") or []:
                TaxLot.from_openapi_data_oapg(lot, _configuration=None)
                lots += 1
        for balance in await venue.balances(account_id):
            Balance.from_openapi_data_oapg(balance, _configuration=None)
    assert lots == 6
    # Every activity each account holds, a page as SnapTrade answers one.
    held = 0
    for account_id in (ALPACA_MARGIN, IBKR_INDIVIDUAL):
        page = await venue.activity_page(account_id, None, NOW.date(), 0, 1000)
        PaginatedUniversalActivity.from_openapi_data_oapg(page.body, _configuration=None)
        held += len(page.activities)
    # IBKR's eight, the last a reinvestment named by a plan's own code, and
    # Alpaca's sixteen: one of each type converted to a kind, and an option's
    # expiry, which converts to none (contract v14).
    assert held == 24


async def test_a_read_of_activities_gets_those_traded_in_its_range() -> None:
    venue = SyntheticVenue(clock())
    recent = await venue.activities(
        IBKR_INDIVIDUAL, NOW.date() - timedelta(days=10), NOW.date()
    )
    assert [a["symbol"]["symbol"] for a in recent] == ["SAP.DE"]
    page = await venue.activity_page(IBKR_INDIVIDUAL, date(2025, 1, 6), NOW.date(), 1, 2)
    assert page.total == 8 and [a["id"][-4:] for a in page.activities] == ["f004", "f005"]


async def test_each_rule_has_something_to_act_on() -> None:
    snapshot = await read(SyntheticVenue(clock()), clock())
    connections = views(snapshot, timedelta(hours=36))
    states = {c.institution: c.state for c in connections}
    assert states == {
        "Alpaca": SyncState.CURRENT,
        "Interactive Brokers": SyncState.DELAYED_BY_DESIGN,
        "Schwab": SyncState.NEEDS_SIGN_IN,
    }
    # Real time for most; the IBKR one on a delay, the one a refresh applies to.
    assert {c.institution: c.serving for c in connections} == {
        "Alpaca": Serving.REAL_TIME,
        "Interactive Brokers": Serving.DELAYED,
        "Schwab": Serving.REAL_TIME,
    }
    by_name = {view.account.name: view for c in connections for view in c.accounts}

    alpaca = by_name["Alpaca Margin"].statement
    assert alpaca is not None
    sides = {row.identifiers[-1].value: row.side for row in alpaca.holdings}
    assert sides["ZZTOP"] is Side.SHORT and sides["AAPL"] is Side.LONG
    assert {row.currency for row in alpaca.holdings if row.kind == "cash"} == {"USD", "CAD"}
    assert any(row.cash_equivalent for row in alpaca.holdings)
    btc = next(row for row in alpaca.holdings if row.identifiers[-1].value == "BTC")
    assert btc.quantity == Decimal("0.012345678")
    assert all(row.market_value is None for row in alpaca.holdings if row.kind != "cash")
    assert by_name["Alpaca Margin"].problems == ()
    by_symbol = {row.identifiers[-1].value: row for row in alpaca.holdings}
    assert by_symbol["AAPL"].average_cost == meridian.Money(Decimal("198.10"), "USD")
    assert by_symbol["AAPL"].lots == (
        Lot(Decimal("10"), meridian.Money(Decimal("1890.00"), "USD"), "2024-03-11"),
        Lot(Decimal("2.5"), meridian.Money(Decimal("586.25"), "USD"), "2025-06-02"),
    )
    # A short holding's lot is short, though SnapTrade writes it positive.
    assert by_symbol["ZZTOP"].lots == (
        Lot(Decimal("-40"), meridian.Money(Decimal("160.80"), "USD"), "2026-08-14"),
    )
    # A lot with no cost or date has none, never one made up.
    assert by_symbol["BTC"].lots == (Lot(Decimal("0.012345678")),)
    assert by_symbol["SYNXX"].lots == by_symbol["AAPL  261218C00250000"].lots == ()
    assert alpaca.net_liquidation == meridian.Money(Decimal("9876.54"), "USD")

    ibkr = by_name["IBKR Individual"].statement
    assert ibkr is not None
    unstated = next(row for row in ibkr.holdings if row.identifiers[-1].value == "SYNX")
    assert unstated.currency_assumed
    assert unstated.average_cost is None and unstated.lots == ()
    sap = next(row for row in ibkr.holdings if row.identifiers[-1].value == "SAP.DE")
    # Lots adding up to less than the holding are recorded as reported.
    assert sum(lot.quantity for lot in sap.lots) == Decimal("25") < sap.quantity
    assert sap.average_cost == meridian.Money(Decimal("180.00"), "EUR")
    usd = next(row for row in ibkr.holdings if row.kind == "cash" and row.currency == "USD")
    assert usd.side is Side.SHORT

    schwab = by_name["Schwab Brokerage"]
    assert not schwab.account.stable
    assert schwab.statement is not None
    assert all(row.lots == () for row in schwab.statement.holdings)
    assert schwab.freshness.holdings_as_of is not None
    assert NOW - schwab.freshness.holdings_as_of == timedelta(days=5)
