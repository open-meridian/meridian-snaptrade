"""Synthetic SnapTrade: responses shaped like SnapTrade's documented API,
served when the `synthetic` setting is on, so the plugin can be developed live
on a deployment before any SnapTrade key exists.

Invented, every value of it: the IDs, account numbers and amounts belong to no
real account. Instruments are named as SnapTrade would name them, with the
FIGI the design fixtures already use for Apple, and one ticker nothing
resolves (ZZTOP, the fixtures' unresolvable), so a placeholder shows too.

Three connections, chosen so each normalising rule has something to act on:

- Alpaca, current and served in real time: long and short stock, an option,
  a money-market fund also counted in cash, crypto to nine decimals, and cash
  in two currencies.
- Interactive Brokers, a business day late by design and the one SnapTrade
  serves on a delay, so the one a refresh applies to: a euro listing, a
  position whose currency SnapTrade does not state, and negative dollar cash.
- Schwab, served in real time but disabled five days ago and serving what it
  last read, with an account SnapTrade gives no institution_account_id for.

The tests hold every response here to SnapTrade's own models.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from string import Template
from typing import Any

from .venue import Json, parse_exact, utc_now

USER_ID = "synthetic-user"

ALPACA = "00000000-0000-4000-8000-00000000a001"
IBKR = "00000000-0000-4000-8000-00000000a002"
SCHWAB = "00000000-0000-4000-8000-00000000a003"

ALPACA_MARGIN = "00000000-0000-4000-8000-00000000b001"
IBKR_INDIVIDUAL = "00000000-0000-4000-8000-00000000b002"
SCHWAB_BROKERAGE = "00000000-0000-4000-8000-00000000b003"

_CONNECTIONS = Template("""[
  {
    "id": "00000000-0000-4000-8000-00000000a001",
    "created_date": "2026-01-15T10:00:00.000Z",
    "updated_date": "$recent",
    "brokerage": {"id": "00000000-0000-4000-8000-00000000c001", "slug": "ALPACA",
                  "name": "Alpaca", "display_name": "Alpaca", "enabled": true,
                  "maintenance_mode": false, "is_degraded": false,
                  "is_real_time_connection": true},
    "name": "Connection 1",
    "type": "read",
    "disabled": false,
    "disabled_date": null,
    "meta": {},
    "data_freshness_mode": {"institution": "realtime", "snaptrade": "realtime"}
  },
  {
    "id": "00000000-0000-4000-8000-00000000a002",
    "created_date": "2026-02-02T10:00:00.000Z",
    "updated_date": "$yesterday",
    "brokerage": {"id": "00000000-0000-4000-8000-00000000c002",
                  "slug": "INTERACTIVE-BROKERS-FLEX", "name": "Interactive Brokers",
                  "display_name": "Interactive Brokers", "enabled": true,
                  "maintenance_mode": false, "is_degraded": false,
                  "is_real_time_connection": false},
    "name": "Connection 2",
    "type": "read",
    "disabled": false,
    "disabled_date": null,
    "meta": {},
    "data_freshness_mode": {"institution": "delayed", "snaptrade": "delayed"}
  },
  {
    "id": "00000000-0000-4000-8000-00000000a003",
    "created_date": "2026-03-03T10:00:00.000Z",
    "updated_date": "$five_days_ago",
    "brokerage": {"id": "00000000-0000-4000-8000-00000000c003", "slug": "SCHWAB",
                  "name": "Charles Schwab", "display_name": "Schwab", "enabled": true,
                  "maintenance_mode": false, "is_degraded": false,
                  "is_real_time_connection": true},
    "name": "Connection 3",
    "type": "read",
    "disabled": true,
    "disabled_date": "$five_days_ago",
    "meta": {},
    "data_freshness_mode": {"institution": "realtime", "snaptrade": "realtime"}
  }
]""")

_ACCOUNTS = Template("""[
  {
    "id": "00000000-0000-4000-8000-00000000b001",
    "brokerage_authorization": "00000000-0000-4000-8000-00000000a001",
    "name": "Alpaca Margin",
    "number": "SYN0001001",
    "institution_name": "Alpaca",
    "created_date": "2026-01-15T10:00:00.000Z",
    "sync_status": {
      "transactions": {"initial_sync_completed": true, "last_successful_sync": "$today",
                       "first_transaction_date": "2025-06-02"},
      "holdings": {"initial_sync_completed": true, "last_successful_sync": "$recent"}
    },
    "balance": {"total": {"amount": 9876.54, "currency": "USD"}},
    "is_paper": true,
    "institution_account_id": "SYN-ALP-1001",
    "status": "open",
    "raw_type": "margin",
    "account_category": "INVESTMENT"
  },
  {
    "id": "00000000-0000-4000-8000-00000000b002",
    "brokerage_authorization": "00000000-0000-4000-8000-00000000a002",
    "name": "IBKR Individual",
    "number": "SYNU2002",
    "institution_name": "Interactive Brokers",
    "created_date": "2026-02-02T10:00:00.000Z",
    "sync_status": {
      "transactions": {"initial_sync_completed": true,
                       "last_successful_sync": "$yesterday_date",
                       "first_transaction_date": "2025-01-06"},
      "holdings": {"initial_sync_completed": true, "last_successful_sync": "$yesterday"}
    },
    "balance": {"total": {"amount": 5432.10, "currency": "USD"}},
    "is_paper": true,
    "institution_account_id": "SYN-IB-2002",
    "status": "open",
    "raw_type": "INDIVIDUAL",
    "account_category": "INVESTMENT"
  },
  {
    "id": "00000000-0000-4000-8000-00000000b003",
    "brokerage_authorization": "00000000-0000-4000-8000-00000000a003",
    "name": "Schwab Brokerage",
    "number": "****3003",
    "institution_name": "Schwab",
    "created_date": "2026-03-03T10:00:00.000Z",
    "sync_status": {
      "transactions": {"initial_sync_completed": true,
                       "last_successful_sync": "$five_days_ago_date",
                       "first_transaction_date": "2026-03-03"},
      "holdings": {"initial_sync_completed": true, "last_successful_sync": "$five_days_ago"}
    },
    "balance": {"total": {"amount": 1230.00, "currency": "USD"}},
    "is_paper": true,
    "institution_account_id": null,
    "status": "open",
    "raw_type": "Brokerage",
    "account_category": "INVESTMENT"
  }
]""")

_POSITIONS: dict[str, Template] = {
    ALPACA_MARGIN: Template("""{
  "results": [
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d001",
                    "symbol": "AAPL", "raw_symbol": "AAPL", "description": "Apple Inc",
                    "currency": "USD", "exchange": "XNAS",
                    "figi_instrument": {"figi_code": "BBG000B9XRY4",
                                        "figi_share_class": "BBG001S5N8V8"}},
     "units": "12.5", "price": "231.40", "cost_basis": "198.10", "currency": "USD"},
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d002",
                    "symbol": "ZZTOP", "raw_symbol": "ZZTOP",
                    "description": "Synthetic short, nothing resolves it",
                    "currency": "USD", "exchange": "XNYS", "figi_instrument": null},
     "units": "-40", "price": "3.15", "cost_basis": "4.02", "currency": "USD"},
    {"instrument": {"kind": "option", "id": "00000000-0000-4000-8000-00000000d003",
                    "symbol": "AAPL  261218C00250000", "option_type": "CALL",
                    "strike_price": "250", "expiration_date": "2026-12-18",
                    "multiplier": "100",
                    "underlying": {"kind": "stock",
                                   "id": "00000000-0000-4000-8000-00000000d001",
                                   "symbol": "AAPL", "raw_symbol": "AAPL",
                                   "description": "Apple Inc"},
                    "description": "AAPL Dec 18 2026 250 Call"},
     "units": "2", "price": "11.20", "cost_basis": "8.75", "currency": "USD"},
    {"instrument": {"kind": "mutualfund", "id": "00000000-0000-4000-8000-00000000d004",
                    "symbol": "SYNXX", "raw_symbol": "SYNXX",
                    "description": "Synthetic Treasury Money Fund", "currency": "USD"},
     "units": "500.00", "price": "1.00", "cost_basis": "1.00", "currency": "USD",
     "cash_equivalent": true},
    {"instrument": {"kind": "crypto", "id": "00000000-0000-4000-8000-00000000d005",
                    "symbol": "BTC", "raw_symbol": "BTC", "description": "Bitcoin",
                    "currency": "USD"},
     "units": "0.012345678", "price": "64210.5", "cost_basis": "58000", "currency": "USD"}
  ],
  "data_freshness": {"as_of": "$recent"}
}"""),
    IBKR_INDIVIDUAL: Template("""{
  "results": [
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d006",
                    "symbol": "SAP.DE", "raw_symbol": "SAP", "description": "SAP SE",
                    "currency": "EUR", "exchange": "XETR"},
     "units": "30", "price": "212.35", "cost_basis": "180.00", "currency": "EUR"},
    {"instrument": {"kind": "other", "id": "00000000-0000-4000-8000-00000000d007",
                    "symbol": "SYNX", "raw_symbol": "SYNX",
                    "description": "A position SnapTrade names no currency for"},
     "units": "7", "price": null, "cost_basis": null, "currency": null}
  ],
  "data_freshness": {"as_of": "$yesterday"}
}"""),
    SCHWAB_BROKERAGE: Template("""{
  "results": [
    {"instrument": {"kind": "etf", "id": "00000000-0000-4000-8000-00000000d008",
                    "symbol": "VTI", "raw_symbol": "VTI",
                    "description": "Vanguard Total Stock Market ETF",
                    "currency": "USD", "exchange": "ARCX"},
     "units": "15", "price": "281.02", "cost_basis": "240.00", "currency": "USD"}
  ],
  "data_freshness": {"as_of": "$five_days_ago"}
}"""),
}

_BALANCES: dict[str, str] = {
    ALPACA_MARGIN: """[
  {"currency": {"id": "00000000-0000-4000-8000-00000000e840", "code": "USD",
                "name": "US Dollar"}, "cash": 1523.45, "buying_power": 3046.90},
  {"currency": {"id": "00000000-0000-4000-8000-00000000e124", "code": "CAD",
                "name": "Canadian Dollar"}, "cash": 200.00, "buying_power": 200.00}
]""",
    IBKR_INDIVIDUAL: """[
  {"currency": {"id": "00000000-0000-4000-8000-00000000e978", "code": "EUR",
                "name": "Euro"}, "cash": 850.10, "buying_power": null},
  {"currency": {"id": "00000000-0000-4000-8000-00000000e840", "code": "USD",
                "name": "US Dollar"}, "cash": -120.55, "buying_power": null}
]""",
    SCHWAB_BROKERAGE: """[
  {"currency": {"id": "00000000-0000-4000-8000-00000000e840", "code": "USD",
                "name": "US Dollar"}, "cash": 42.00, "buying_power": 42.00}
]""",
}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SyntheticVenue:
    """The `Venue` synthetic mode serves. Times are relative to now, so the
    Alpaca account is current, the IBKR one is a day old, and Schwab's is five
    days old, whenever it runs."""

    def __init__(self, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now

    def __repr__(self) -> str:
        return "SyntheticVenue()"

    def _times(self) -> dict[str, str]:
        now = self._now()
        yesterday = now - timedelta(hours=20)
        five_days_ago = now - timedelta(days=5)
        return {
            "recent": _stamp(now - timedelta(minutes=10)),
            "today": now.date().isoformat(),
            "yesterday": _stamp(yesterday),
            "yesterday_date": yesterday.date().isoformat(),
            "five_days_ago": _stamp(five_days_ago),
            "five_days_ago_date": five_days_ago.date().isoformat(),
        }

    def _json(self, template: Template) -> Any:
        return parse_exact(template.substitute(self._times()))

    async def users(self) -> list[str]:
        return [USER_ID]

    async def connections(self) -> list[Json]:
        body: list[Json] = self._json(_CONNECTIONS)
        return body

    async def accounts(self) -> list[Json]:
        body: list[Json] = self._json(_ACCOUNTS)
        return body

    async def positions(self, account_id: str) -> Json:
        template = _POSITIONS.get(account_id)
        body: Json = (
            self._json(template)
            if template
            else {"results": [], "data_freshness": {"as_of": self._times()["recent"]}}
        )
        return body

    async def balances(self, account_id: str) -> list[Json]:
        body: list[Json] = parse_exact(_BALANCES.get(account_id, "[]"))
        return body

    async def connection_portal(self, reconnect: str | None = None) -> str:
        # There is no portal without a key; the page says so.
        return ""

    async def refresh(self, connection_id: str) -> str:
        return "Synthetic mode: nothing was asked of SnapTrade."
