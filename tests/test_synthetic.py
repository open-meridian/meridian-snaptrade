"""Synthetic mode's responses are SnapTrade's shapes: each is held to
SnapTrade's own models, from its official SDK, and exercises the rules."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from snaptrade_client.model.account import Account
from snaptrade_client.model.all_account_positions_response import (
    AllAccountPositionsResponse,
)
from snaptrade_client.model.balance import Balance
from snaptrade_client.model.brokerage_authorization import BrokerageAuthorization

from snaptrade.normalise import Serving, Side, SyncState, views
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
    for account_id in ACCOUNTS:
        AllAccountPositionsResponse.from_openapi_data_oapg(
            await venue.positions(account_id), _configuration=None
        )
        for balance in await venue.balances(account_id):
            Balance.from_openapi_data_oapg(balance, _configuration=None)


async def test_each_rule_has_something_to_act_on() -> None:
    snapshot = await read(SyntheticVenue(clock()), clock())
    connections = views(snapshot, timedelta(hours=36))
    states = {c.institution: c.state for c in connections}
    assert states == {
        "Alpaca": SyncState.CURRENT,
        "Interactive Brokers": SyncState.DELAYED_BY_DESIGN,
        "Schwab": SyncState.DISABLED,
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

    ibkr = by_name["IBKR Individual"].statement
    assert ibkr is not None
    unstated = next(row for row in ibkr.holdings if row.identifiers[-1].value == "SYNX")
    assert unstated.currency_assumed
    usd = next(row for row in ibkr.holdings if row.kind == "cash" and row.currency == "USD")
    assert usd.side is Side.SHORT

    schwab = by_name["Schwab Brokerage"]
    assert not schwab.account.stable
    assert schwab.freshness.holdings_as_of is not None
    assert NOW - schwab.freshness.holdings_as_of == timedelta(days=5)
