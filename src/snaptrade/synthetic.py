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
  in two currencies. Apple has two tax lots; ZZTOP, short, one, which
  SnapTrade writes with a positive quantity; Bitcoin one with no cost or
  date; the option and the fund none.
- Interactive Brokers, a business day late by design and the one SnapTrade
  serves on a delay, so the one a refresh applies to: a euro listing whose
  lots add up to less than its quantity (recorded as reported), a position
  whose currency SnapTrade does not state, and negative dollar cash. Three
  positions with no tax lots and a history behind them, for the lots
  proposed from it (history.py): SYNQ, bought twice and held since, so its
  buys are its lots; SYNB, transferred in from another brokerage, so its
  history states no cost and only its average purchase price proposes one;
  and SYNV, bought and partly sold, which no rule SnapTrade states splits
  into lots (no FIFO), so its average purchase price proposes one too.
- Schwab, served in real time but disabled five days ago and serving what it
  last read, so needing sign-in, with an account SnapTrade gives no
  institution_account_id for, and no tax lots.

Alpaca's history, on fixed dates from its first transaction, holds one
activity of each type the plugin converts to a kind and one it converts to
none (contract v14, activities.py), for its backfill to report.

The tests hold every response here to SnapTrade's own models.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from string import Template
from typing import Any

from .venue import ActivityPage, Json, VenueError, activity_page_of, parse_exact, utc_now

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
     "units": "12.5", "price": "231.40", "cost_basis": "198.10", "currency": "USD",
     "tax_lots": [
       {"original_purchase_date": "2024-03-11T14:30:00.000Z", "quantity": "10",
        "purchased_price": "189.00", "cost_basis": "1890.00", "current_value": "2314.00",
        "position_type": "LONG", "lot_id": "SYN-LOT-1"},
       {"original_purchase_date": "2025-06-02T15:00:00.000Z", "quantity": "2.5",
        "purchased_price": "234.50", "cost_basis": "586.25", "current_value": "578.50",
        "position_type": "LONG", "lot_id": "SYN-LOT-2"}
     ]},
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d002",
                    "symbol": "ZZTOP", "raw_symbol": "ZZTOP",
                    "description": "Synthetic short, nothing resolves it",
                    "currency": "USD", "exchange": "XNYS", "figi_instrument": null},
     "units": "-40", "price": "3.15", "cost_basis": "4.02", "currency": "USD",
     "tax_lots": [
       {"original_purchase_date": "2026-08-14T13:45:00.000Z", "quantity": "40",
        "purchased_price": "4.02", "cost_basis": "160.80", "current_value": "126.00",
        "position_type": "SHORT", "lot_id": "SYN-LOT-3"}
     ]},
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
     "units": "0.012345678", "price": "64210.5", "cost_basis": "58000", "currency": "USD",
     "tax_lots": [
       {"original_purchase_date": null, "quantity": "0.012345678",
        "purchased_price": null, "cost_basis": null, "current_value": null,
        "position_type": "LONG", "lot_id": null}
     ]}
  ],
  "data_freshness": {"as_of": "$recent"}
}"""),
    IBKR_INDIVIDUAL: Template("""{
  "results": [
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d006",
                    "symbol": "SAP.DE", "raw_symbol": "SAP", "description": "SAP SE",
                    "currency": "EUR", "exchange": "XETR"},
     "units": "30", "price": "212.35", "cost_basis": "180.00", "currency": "EUR",
     "tax_lots": [
       {"original_purchase_date": "2023-05-15T09:00:00.000Z", "quantity": "20",
        "purchased_price": "175.00", "cost_basis": "3500.00", "current_value": "4247.00",
        "position_type": "LONG", "lot_id": "SYN-LOT-4"},
       {"original_purchase_date": "2024-11-04T10:30:00.000Z", "quantity": "5",
        "purchased_price": "190.00", "cost_basis": "950.00", "current_value": "1061.75",
        "position_type": "LONG", "lot_id": "SYN-LOT-5"}
     ]},
    {"instrument": {"kind": "other", "id": "00000000-0000-4000-8000-00000000d007",
                    "symbol": "SYNX", "raw_symbol": "SYNX",
                    "description": "A position SnapTrade names no currency for"},
     "units": "7", "price": null, "cost_basis": null, "currency": null},
    {"instrument": {"kind": "etf", "id": "00000000-0000-4000-8000-00000000d009",
                    "symbol": "SYNQ", "raw_symbol": "SYNQ",
                    "description": "Synthetic leveraged index ETF, bought twice",
                    "currency": "USD", "exchange": "XNAS"},
     "units": "30", "price": "52.10", "cost_basis": "41.25", "currency": "USD"},
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d010",
                    "symbol": "SYNB", "raw_symbol": "SYNB",
                    "description": "Synthetic bank shares, transferred in",
                    "currency": "USD", "exchange": "XNYS"},
     "units": "100", "price": "21.05", "cost_basis": "18.40", "currency": "USD"},
    {"instrument": {"kind": "stock", "id": "00000000-0000-4000-8000-00000000d011",
                    "symbol": "SYNV", "raw_symbol": "SYNV",
                    "description": "Synthetic shares, bought and partly sold",
                    "currency": "USD", "exchange": "XNYS"},
     "units": "30", "price": "12.40", "cost_basis": "10.00", "currency": "USD"}
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


# Each account's activities, as SnapTrade's account activities answer them,
# every one SnapTrade holds: a read asks for a range, and gets those traded
# in it. The last ten days are what the settled and pending quantities come
# from (contract v11): Alpaca's last trade settled long ago, so all it holds
# is settled; IBKR bought 5 SAP.DE yesterday, settling tomorrow, so 5 of its
# 30 and the euros paying for them are pending; Schwab's activities cannot be
# read, so its settled quantities say so. Before them, IBKR's history: SYNQ
# bought twice, with a dividend since; SYNB transferred in, with no cost
# stated; SYNV bought, then partly sold. Alpaca's history, on fixed dates
# from its first transaction (2025-06-02), holds one of each type the plugin
# converts to a kind (contract v14, activities.py) and one it does not, an
# option's expiry: a deposit, AAPL bought (its second tax lot), dividends
# with tax withheld, SYNXX's dividend reinvested, interest and a fee, SYNT
# transferred in, split 2 for 1, given a stock dividend and transferred out,
# a withdrawal, a journal, and ZZTOP sold short.
_ACTIVITIES: dict[str, list[str]] = {
    ALPACA_MARGIN: [
        """{"id": "00000000-0000-4000-8000-00000000f101", "type": "CONTRIBUTION",
   "symbol": null, "units": 0, "price": 0, "amount": 5000.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-06-02T00:00:00.000Z",
   "settlement_date": "2025-06-02T00:00:00.000Z", "description": "Deposit"}""",
        """{"id": "00000000-0000-4000-8000-00000000f001", "type": "BUY",
   "symbol": {"symbol": "AAPL", "description": "Apple Inc"},
   "units": 2.5, "price": 234.50, "amount": -586.25, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-06-02T00:00:00.000Z",
   "settlement_date": "2025-06-03T00:00:00.000Z", "description": "Bought 2.5 AAPL"}""",
        """{"id": "00000000-0000-4000-8000-00000000f102", "type": "OPTIONEXPIRATION",
   "symbol": null, "option_symbol": {"id": "00000000-0000-4000-8000-00000000d012",
                     "ticker": "AAPL  250620C00300000", "option_type": "CALL",
                     "strike_price": 300, "expiration_date": "2025-06-20",
                     "is_mini_option": false,
                     "underlying_symbol": {"id": "00000000-0000-4000-8000-00000000d001",
                                           "symbol": "AAPL", "raw_symbol": "AAPL",
                                           "description": "Apple Inc"}},
   "units": -1, "price": 0, "amount": 0.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-06-20T00:00:00.000Z",
   "settlement_date": "2025-06-20T00:00:00.000Z",
   "description": "AAPL Jun 20 2025 300 Call expired"}""",
        """{"id": "00000000-0000-4000-8000-00000000f103", "type": "EXTERNAL_ASSET_TRANSFER_IN",
   "symbol": {"symbol": "SYNT", "description": "Synthetic shares, split and moved on"},
   "units": 40, "price": 0, "amount": null, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-07-01T00:00:00.000Z",
   "settlement_date": "2025-07-01T00:00:00.000Z",
   "description": "40 SYNT received from another brokerage"}""",
        """{"id": "00000000-0000-4000-8000-00000000f104", "type": "DIVIDEND",
   "symbol": {"symbol": "AAPL", "description": "Apple Inc"},
   "units": 0, "price": 0, "amount": 3.25, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-08-14T00:00:00.000Z",
   "settlement_date": "2025-08-14T00:00:00.000Z", "description": "AAPL dividend"}""",
        """{"id": "00000000-0000-4000-8000-00000000f105", "type": "TAX",
   "symbol": {"symbol": "AAPL", "description": "Apple Inc"},
   "units": 0, "price": 0, "amount": -0.49, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-08-14T00:00:00.000Z",
   "settlement_date": "2025-08-14T00:00:00.000Z",
   "description": "AAPL dividend, tax withheld"}""",
        """{"id": "00000000-0000-4000-8000-00000000f106", "type": "DIVIDEND",
   "symbol": {"symbol": "SYNXX", "description": "Synthetic Treasury Money Fund"},
   "units": 0, "price": 0, "amount": 3.27, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-09-30T00:00:00.000Z",
   "settlement_date": "2025-09-30T00:00:00.000Z",
   "description": "DIVIDEND RECEIVED SYNTHETIC TREASURY MONEY FUND (SYNXX)"}""",
        """{"id": "00000000-0000-4000-8000-00000000f107", "type": "REI",
   "symbol": {"symbol": "SYNXX", "description": "Synthetic Treasury Money Fund"},
   "units": 3.27, "price": 1.00, "amount": -3.27, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-09-30T00:00:00.000Z",
   "settlement_date": "2025-09-30T00:00:00.000Z",
   "description": "REINVESTMENT SYNTHETIC TREASURY MONEY FUND (SYNXX)"}""",
        """{"id": "00000000-0000-4000-8000-00000000f108", "type": "INTEREST",
   "symbol": null, "units": 0, "price": 0, "amount": 0.42, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-10-01T00:00:00.000Z",
   "settlement_date": "2025-10-01T00:00:00.000Z", "description": "Interest on cash"}""",
        """{"id": "00000000-0000-4000-8000-00000000f109", "type": "FEE",
   "symbol": null, "units": 0, "price": 0, "amount": -1.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-11-03T00:00:00.000Z",
   "settlement_date": "2025-11-03T00:00:00.000Z", "description": "Account fee"}""",
        """{"id": "00000000-0000-4000-8000-00000000f110", "type": "SPLIT",
   "symbol": {"symbol": "SYNT", "description": "Synthetic shares, split and moved on"},
   "units": 40, "price": 0, "amount": 0.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-11-20T00:00:00.000Z",
   "settlement_date": "2025-11-20T00:00:00.000Z", "description": "STOCK SPLIT 2 FOR 1"}""",
        """{"id": "00000000-0000-4000-8000-00000000f111", "type": "STOCK_DIVIDEND",
   "symbol": {"symbol": "SYNT", "description": "Synthetic shares, split and moved on"},
   "units": 1, "price": 0, "amount": 0.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2025-12-01T00:00:00.000Z",
   "settlement_date": "2025-12-01T00:00:00.000Z", "description": "Stock dividend, 1 SYNT"}""",
        """{"id": "00000000-0000-4000-8000-00000000f112", "type": "EXTERNAL_ASSET_TRANSFER_OUT",
   "symbol": {"symbol": "SYNT", "description": "Synthetic shares, split and moved on"},
   "units": 81, "price": 0, "amount": null, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2026-01-15T00:00:00.000Z",
   "settlement_date": "2026-01-15T00:00:00.000Z",
   "description": "81 SYNT delivered to another brokerage"}""",
        """{"id": "00000000-0000-4000-8000-00000000f113", "type": "WITHDRAWAL",
   "symbol": null, "units": 0, "price": 0, "amount": -500.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2026-02-02T00:00:00.000Z",
   "settlement_date": "2026-02-02T00:00:00.000Z", "description": "Withdrawal"}""",
        """{"id": "00000000-0000-4000-8000-00000000f114", "type": "JOURNALED",
   "symbol": null, "units": 0, "price": 0, "amount": -200.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2026-03-02T00:00:00.000Z",
   "settlement_date": "2026-03-02T00:00:00.000Z",
   "description": "Journal from the margin to the cash side"}""",
        """{"id": "00000000-0000-4000-8000-00000000f115", "type": "SELL",
   "symbol": {"symbol": "ZZTOP", "description": "Synthetic short, nothing resolves it"},
   "units": -40, "price": 4.02, "amount": 160.80, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "2026-08-14T00:00:00.000Z",
   "settlement_date": "2026-08-15T00:00:00.000Z", "description": "Sold short 40 ZZTOP"}""",
    ],
    IBKR_INDIVIDUAL: [
        """{"id": "00000000-0000-4000-8000-00000000f003", "type": "BUY",
   "symbol": {"symbol": "SYNQ", "description": "Synthetic leveraged index ETF"},
   "units": 20, "price": 38.00, "amount": -760.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_300",
   "settlement_date": "$days_ago_299", "description": "Bought 20 SYNQ"}""",
        """{"id": "00000000-0000-4000-8000-00000000f004", "type": "BUY",
   "symbol": {"symbol": "SYNV", "description": "Synthetic shares"},
   "units": 50, "price": 10.00, "amount": -500.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_250",
   "settlement_date": "$days_ago_249", "description": "Bought 50 SYNV"}""",
        """{"id": "00000000-0000-4000-8000-00000000f005",
   "type": "EXTERNAL_ASSET_TRANSFER_IN",
   "symbol": {"symbol": "SYNB", "description": "Synthetic bank shares"},
   "units": 100, "price": 0, "amount": null, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_200",
   "settlement_date": "$days_ago_200",
   "description": "100 SYNB received from another brokerage"}""",
        """{"id": "00000000-0000-4000-8000-00000000f006", "type": "BUY",
   "symbol": {"symbol": "SYNQ", "description": "Synthetic leveraged index ETF"},
   "units": 10, "price": 47.75, "amount": -477.50, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_120",
   "settlement_date": "$days_ago_119", "description": "Bought 10 SYNQ"}""",
        """{"id": "00000000-0000-4000-8000-00000000f007", "type": "SELL",
   "symbol": {"symbol": "SYNV", "description": "Synthetic shares"},
   "units": -20, "price": 12.00, "amount": 240.00, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_90",
   "settlement_date": "$days_ago_89", "description": "Sold 20 SYNV"}""",
        """{"id": "00000000-0000-4000-8000-00000000f008", "type": "DIVIDEND",
   "symbol": {"symbol": "SYNQ", "description": "Synthetic leveraged index ETF"},
   "units": 0, "price": 0, "amount": 3.20, "fee": 0,
   "currency": {"code": "USD"}, "trade_date": "$days_ago_60",
   "settlement_date": "$days_ago_60", "description": "SYNQ dividend"}""",
        """{"id": "00000000-0000-4000-8000-00000000f002", "type": "BUY",
   "symbol": {"symbol": "SAP.DE", "description": "SAP SE"},
   "units": 5, "price": 212.00, "amount": -1060.00, "fee": 0,
   "currency": {"code": "EUR"}, "trade_date": "$yesterday_midnight",
   "settlement_date": "$tomorrow_date", "description": "Bought 5 SAP.DE"}""",
    ],
}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _midnight(moment: datetime) -> str:
    """A day as SnapTrade writes an activity's date: midnight UTC."""
    return moment.strftime("%Y-%m-%dT00:00:00.000Z")


def _traded(activity: Json) -> date | None:
    said = activity.get("trade_date")
    try:
        return date.fromisoformat(said[:10]) if isinstance(said, str) else None
    except ValueError:
        return None


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
            "tomorrow_date": _midnight(now + timedelta(days=1)),
            "yesterday_midnight": _midnight(yesterday),
            **{
                f"days_ago_{days}": _midnight(now - timedelta(days=days))
                for days in (300, 299, 250, 249, 200, 120, 119, 90, 89, 60)
            },
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

    def _activities(self, account_id: str, start: date | None, end: date) -> list[Json]:
        """The account's activities traded from `start` to `end`, in the
        order SnapTrade lists them."""
        if account_id == SCHWAB_BROKERAGE:
            # The one account whose activities cannot be read: its settled
            # quantities say so, and its history cannot be read either.
            raise VenueError("reading activities")
        listed = [self._json(Template(each)) for each in _ACTIVITIES.get(account_id, [])]
        return [
            activity
            for activity in listed
            if (traded := _traded(activity)) is not None
            and (start is None or start <= traded)
            and traded <= end
        ]

    async def activities(self, account_id: str, start: date, end: date) -> list[Json]:
        return self._activities(account_id, start, end)

    async def activity_page(
        self, account_id: str, start: date | None, end: date, offset: int, limit: int
    ) -> ActivityPage:
        found = self._activities(account_id, start, end)
        page = found[offset : offset + limit]
        return activity_page_of(
            {
                "data": page,
                "pagination": {"offset": offset, "limit": limit, "total": len(found)},
            }
        )

    async def connection_portal(self, reconnect: str | None = None) -> str:
        # There is no portal without a key; the page says so.
        return ""

    async def refresh(self, connection_id: str) -> str:
        return "Synthetic mode: nothing was asked of SnapTrade."
