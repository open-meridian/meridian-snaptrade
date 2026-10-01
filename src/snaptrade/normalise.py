"""SnapTrade's shapes, turned into the platform's convention.

The one place SnapTrade's vocabulary is read; nothing after this module knows
it (decisions/023: the venue's convention stops at the plugin that speaks to
the venue). Pure functions of a `venue.Snapshot` and the time, so each rule is
tested on its own. What the account-side contract calls each thing
(spec/the-account-side-fits-every-venue) is what it is called here;
contract.py sends it to the sidecar.

The rules, each from the spec or the broker survey (reference/broker-apis.md):

- **A stable account ID.** SnapTrade's account `id` changes when a connection
  is deleted and made again; `institution_account_id` does not, and is the
  same when one real account is reached through two connections. So the ID is
  the brokerage's slug and `institution_account_id`. Where SnapTrade gives no
  `institution_account_id`, it is SnapTrade's `id`, said to be unstable: a
  reconnect then shows the account as new, to be linked again.
- **Cash is a holding** of the currency's cash instrument, identified
  `{scheme: iso4217, value: <code>}`, one row per account and currency.
- **Long and short are separate rows,** each with its side and a quantity
  signed to match. SnapTrade reports one signed `units` per instrument.
- **Trade-date quantity always; settle-date where the venue gives one.**
  SnapTrade gives none, so it is never set here.
- **Market value only as reported.** SnapTrade reports none for a position,
  and it is never computed from `price`: unset means "not reported". A cash
  row's value is its amount, which is what SnapTrade reported.
- **Cost as reported, never multiplied out** (the product owner,
  2026-10-01). SnapTrade's `cost_basis` on a position is an average per unit
  (per share for an option), so it is the holding's average cost, as
  written; the holding's total cost basis is left unset, since SnapTrade
  reports none. A lot is a `tax_lots` entry: its quantity signed as its
  holding, its cost as reported (the whole lot's, sign included) and the date
  part of its purchase date as written. No `tax_lots` is no lots, never a
  made-up one; and where any lot of a holding cannot be read exactly, none of
  its lots is sent and the account says why, since a partial list would show
  a difference that is not there.
- **The account's total value is its net liquidation** (the product owner,
  2026-10-01): `balance.total` on the account, which SnapTrade has from the
  brokerage, is the statement's net liquidation; buying power is SnapTrade's
  per currency, sent where exactly one currency reports it.
- **An asset class from SnapTrade's instrument kind** (the product owner,
  2026-10-01): stock is equity, etf and mutualfund are fund, bond is debt,
  option is derivative, crypto is crypto_asset; any other kind is left unset
  for a person to set on the platform. Cash is no instrument SnapTrade
  reports, and has none.
- **A stated currency where the venue gives none,** marked as assumed.
- **Numbers are Decimal.** A float reaching here becomes Decimal(repr(value)).
- **The statement ID is made from the account and the read time.**
- **Sync state and freshness** from the connection and the account's sync
  status: disabled, holdings unavailable (the brokerage does not show
  SnapTrade the account's holdings), delayed by design (Interactive Brokers
  through SnapTrade), stale, or current, with holdings and history freshness
  apart.
- **How SnapTrade serves a connection,** from `data_freshness_mode.snaptrade`:
  `realtime` (it reads the brokerage on every call, so there is nothing to
  refresh, and on a Real-time plan SnapTrade refuses a refresh), `delayed` (it
  serves cached data, and a refresh, charged per call, reads it again), or not
  said.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import meridian

from .venue import Json, Snapshot

SOURCE = "snaptrade"

# How late an account that is late by design may be before it is also stale:
# a business day, a weekend and a holiday.
DELAYED_STALE_AFTER = timedelta(days=4)

# When a connection carries no data_freshness_mode, the brokerages known to be
# a business day late through SnapTrade (reference/broker-apis.md).
_LATE_BY_DESIGN_SLUGS = ("INTERACTIVE-BROKERS",)

# SnapTrade's instrument kinds (`instrument.kind` in `positions/all`) the
# product owner mapped, 2026-10-01, to the platform's asset class, by
# meridian.AssetClass's lowercase name. SnapTrade's other kinds -- adr, cef,
# future, future_option, cfd, tokenized_asset, other -- and any it adds are
# not here: their class is left for a person to set, never guessed.
_ASSET_CLASS = {
    "stock": "equity",
    "etf": "fund",
    "mutualfund": "fund",
    "bond": "debt",
    "option": "derivative",
    "crypto": "crypto_asset",
    # The product owner, 2026-10-01: a depositary receipt is a claim on
    # shares; a closed-end fund a share of a pool; futures, their options and
    # contracts for difference take their value from something else. A
    # tokenized asset's class depends on what the token stands for, and
    # "other" on nothing known, so both are left for a person.
    "adr": "equity",
    "cef": "fund",
    "future": "derivative",
    "future_option": "derivative",
    "cfd": "derivative",
}

_MIC = re.compile(r"^[A-Z0-9]{4}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# The calendar date a moment was written on, before any time: SnapTrade's
# `original_purchase_date` is a date-time, and its date part is taken as
# written, with no time-zone conversion that could move the day.
_DATE_PART = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ])")

# What the wire carries exactly (decisions/023): at most 18 decimal places and
# 38 digits. The SDK refuses more, never rounds; a number past either is found
# here, before its statement opens, rather than in the middle of one.
MOST_PLACES = 18
_TOO_MANY_DIGITS = 10**38


class SyncState(Enum):
    """The account-side contract's states (Q4), with what each asks of a person."""

    CURRENT = "current"
    STALE = "stale"
    NEEDS_SIGN_IN = "needs_sign_in"
    DISABLED = "disabled"
    DELAYED_BY_DESIGN = "delayed_by_design"
    # The product owner, 2026-09-28: the venue does not provide holdings
    # through this connection (SnapTrade's `holdings_unavailable`).
    HOLDINGS_UNAVAILABLE = "holdings_unavailable"


REMEDY: dict[SyncState, str] = {
    SyncState.CURRENT: "Nothing to do.",
    SyncState.STALE: (
        "Usually SnapTrade's to recover. If it lasts, refresh the connection on the "
        "Connections tab where it offers Refresh, or reconnect it."
    ),
    SyncState.NEEDS_SIGN_IN: "Somebody signs in to the brokerage again through SnapTrade.",
    SyncState.DISABLED: (
        "Reconnect it: open SnapTrade's Connection Portal from this page and sign in to the "
        "brokerage. Until then SnapTrade serves the last data it read."
    ),
    SyncState.DELAYED_BY_DESIGN: (
        "Nothing to do: this brokerage reaches SnapTrade a business day late."
    ),
    SyncState.HOLDINGS_UNAVAILABLE: (
        "Connect the account another way, or through another venue: its holdings will "
        "not arrive through this connection, however long it waits."
    ),
}


class Serving(Enum):
    """How SnapTrade serves a connection's data: what decides whether a manual
    refresh means anything (SnapTrade's `data_freshness_mode.snaptrade`)."""

    # SnapTrade reads the brokerage on every call: nothing to refresh.
    REAL_TIME = "real_time"
    # SnapTrade serves cached data; a refresh, charged per call, reads it again.
    DELAYED = "delayed"
    # SnapTrade did not say.
    UNKNOWN = "unknown"


class Side(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Identifier:
    """An identifier as W3 resolves it: a scheme, a value, and for a venue's
    own scheme, the venue."""

    scheme: str
    value: str
    source: str = ""


@dataclass(frozen=True)
class ExternalAccount:
    """An account a connection reaches, as the contract's ExternalAccountsEvent
    carries it, and what the plugin keeps beside it."""

    external_account_id: str
    # False when SnapTrade gave no institution_account_id, so the ID above is
    # SnapTrade's own and changes if the connection is made again.
    stable: bool
    # The custodian's own name for the account.
    name: str
    # The venue's own account type, verbatim, for display only (Q2). Empty
    # when SnapTrade did not say.
    account_type: str
    institution: str
    connection_id: str
    # SnapTrade's per-call handle. Stays inside the plugin.
    snaptrade_account_id: str
    # The brokerage's account number, as SnapTrade gives it, for matching the
    # account to one of the deployment's on the Account links tab and shown
    # there to the admin. Empty when SnapTrade gives none, or masks it
    # ("****3003"): a masked number matches nothing.
    number: str = ""


@dataclass(frozen=True)
class Freshness:
    state: SyncState
    # When the holdings SnapTrade serves are as of (its last successful sync
    # of holdings), and when the history is (transactions, by the day).
    holdings_as_of: datetime | None
    history_as_of: date | None
    detail: str

    @property
    def healthy(self) -> bool:
        """What today's `connection_healthy` can say: data that is as it should be."""
        return self.state in (SyncState.CURRENT, SyncState.DELAYED_BY_DESIGN)

    @property
    def remedy(self) -> str:
        return REMEDY[self.state]


@dataclass(frozen=True)
class Lot:
    """One lot of a holding, as SnapTrade lists it in `tax_lots`."""

    # Signed as its holding: negative on a short one.
    quantity: Decimal
    # The whole lot's cost, as reported, sign included; None where none is.
    cost: meridian.Money | None = None
    # The date part of its purchase date, as written; "" where none is.
    acquired_date: str = ""


@dataclass(frozen=True)
class Holding:
    identifiers: tuple[Identifier, ...]
    description: str
    # SnapTrade's instrument kind, or "cash". Shown, never sent.
    kind: str
    side: Side
    # The trade-date quantity, signed to match the side.
    quantity: Decimal
    # The currency the row is in: the position's currency, or the cash's.
    currency: str
    # True when SnapTrade stated no currency for the position and the one
    # above is this plugin's stated assumption.
    currency_assumed: bool = False
    settle_date_quantity: Decimal | None = None
    # None is "not reported", never zero.
    market_value: meridian.Money | None = None
    exchange_mic: str = ""
    # A money-market fund SnapTrade also counts in the account's cash.
    cash_equivalent: bool = False
    # The platform's asset class for the kind (`asset_class`), sent with a
    # miss; empty where it is not known, and for cash.
    asset_class: str = ""
    # SnapTrade's average cost per unit (per share for an option), as
    # reported; None where it reports none. Never multiplied out into a total.
    average_cost: meridian.Money | None = None
    # The venue's lots, in its order; none where it lists none.
    lots: tuple[Lot, ...] = ()


@dataclass(frozen=True)
class Statement:
    """The connector's snapshot of one account (Q3)."""

    external_statement_id: str
    as_of_date: str
    read_at_ns: int
    holdings: tuple[Holding, ...]
    # Buying power as SnapTrade reported it, per currency. Never derived.
    buying_power: tuple[meridian.Money, ...] = ()
    # The account's total value as the brokerage gave it to SnapTrade
    # (`balance.total`); None where it gave none.
    net_liquidation: meridian.Money | None = None


@dataclass(frozen=True)
class AccountView:
    """Everything one read says about one account."""

    account: ExternalAccount
    freshness: Freshness
    # None when nothing is to be recorded for it this time; `withheld` says why.
    statement: Statement | None
    withheld: str = ""
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectionView:
    connection_id: str
    name: str
    institution: str
    # read or trade, as SnapTrade says.
    access: str
    state: SyncState
    detail: str
    disabled_at: datetime | None
    accounts: tuple[AccountView, ...] = field(default=())
    serving: Serving = Serving.UNKNOWN


# ── Numbers ──────────────────────────────────────────────────────────────────


def to_decimal(value: Any, name: str) -> Decimal:
    """A number as SnapTrade sent it, exactly.

    A decimal string or a Decimal (SnapTrade's JSON read exactly) is taken as
    written; an int is exact; a float, should one get this far, is its
    shortest round-trip form, Decimal(repr(value)), never Decimal(value),
    which is its binary expansion.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is not a number: {value!r}")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{name} is not a number: {value!r}") from None
    else:
        raise ValueError(f"{name} is not a number: {value!r}")
    if not number.is_finite():
        raise ValueError(f"{name} is not a finite number: {value!r}")
    return number


def wire_decimal(value: Any, name: str) -> Decimal:
    """`to_decimal`, refused where the wire cannot carry it exactly: more than
    18 decimal places or 38 digits, which the SDK would refuse once its
    statement had opened."""
    number = to_decimal(value, name)
    _, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)  # finite, as to_decimal holds
    places = max(-exponent, 0)
    if places > MOST_PLACES:
        raise ValueError(
            f"{name} has {places} decimal places; at most {MOST_PLACES} cross the wire"
        )
    if int("".join(map(str, digits))) * 10 ** max(exponent, 0) >= _TOO_MANY_DIGITS:
        raise ValueError(f"{name} has more than 38 digits")
    return number


# ── Times ────────────────────────────────────────────────────────────────────


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def ns(moment: datetime) -> int:
    """Nanoseconds since the epoch, exactly, from a datetime's own fields."""
    since = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (since.days * 86_400 + since.seconds) * 1_000_000_000 + since.microseconds * 1_000


def day_ns(day: date) -> int:
    return ns(datetime.combine(day, time(), tzinfo=UTC))


# ── Accounts ─────────────────────────────────────────────────────────────────


def _dict(value: Any) -> Json:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def brokerage_slug(connection: Json) -> str:
    return _text(_dict(connection.get("brokerage")).get("slug"))


def external_account(account: Json, connection: Json) -> ExternalAccount:
    """The stable identity of an account, and its names."""
    institution = _text(account.get("institution_name"))
    held_at = brokerage_slug(connection) or institution.upper()
    institution_account_id = _text(account.get("institution_account_id"))
    snaptrade_id = _text(account.get("id"))
    if institution_account_id:
        external_id, stable = f"{held_at}:{institution_account_id}", True
    else:
        external_id, stable = f"snaptrade:{snaptrade_id}", False
    return ExternalAccount(
        external_account_id=external_id,
        stable=stable,
        name=_text(account.get("name")) or institution,
        account_type=_text(account.get("raw_type")),
        institution=institution,
        connection_id=_text(account.get("brokerage_authorization")),
        snaptrade_account_id=snaptrade_id,
        number=account_number(account),
    )


def account_number(account: Json) -> str:
    """The account's number as the brokerage writes it, or "" where SnapTrade
    gives none or masks part of it."""
    number = _text(account.get("number"))
    return "" if any(mark in number for mark in "*•") else number


def late_by_design(connection: Json) -> bool:
    """Whether the brokerage behind a connection is late on purpose."""
    mode = connection.get("data_freshness_mode")
    if isinstance(mode, dict) and "institution" in mode:
        return mode.get("institution") == "delayed"
    return brokerage_slug(connection).startswith(_LATE_BY_DESIGN_SLUGS)


def serving(connection: Json) -> Serving:
    """How SnapTrade serves a connection: `data_freshness_mode.snaptrade`,
    `realtime` or `delayed`, and unknown when it says neither."""
    said = _dict(connection.get("data_freshness_mode")).get("snaptrade")
    if said == "realtime":
        return Serving.REAL_TIME
    if said == "delayed":
        return Serving.DELAYED
    return Serving.UNKNOWN


def freshness(
    account: Json, connection: Json, now: datetime, stale_after: timedelta
) -> Freshness:
    """An account's sync state and freshness, from SnapTrade's connection and
    the account's own sync status. The first that applies wins."""
    status = _dict(account.get("sync_status"))
    holdings = _dict(status.get("holdings"))
    history = _dict(status.get("transactions"))
    holdings_as_of = _moment(holdings.get("last_successful_sync"))
    history_as_of = _day(history.get("last_successful_sync"))

    def said(state: SyncState, detail: str) -> Freshness:
        return Freshness(state, holdings_as_of, history_as_of, detail)

    if connection.get("disabled") is True:
        since = _moment(connection.get("disabled_date"))
        when = f" on {since:%Y-%m-%d}" if since else ""
        return said(
            SyncState.DISABLED,
            f"SnapTrade disabled this connection{when}; it still serves the last data it read.",
        )
    if holdings.get("initial_sync_completed") is False:
        return said(
            SyncState.STALE, "SnapTrade has not finished its first sync of this account."
        )
    if holdings.get("holdings_unavailable") is True:
        return said(
            SyncState.HOLDINGS_UNAVAILABLE,
            "The brokerage does not show this account's holdings to SnapTrade; an empty "
            "list from it does not mean an empty account.",
        )
    if holdings_as_of is None:
        return said(SyncState.STALE, "SnapTrade reports no successful sync of this account.")
    age = now - holdings_as_of
    if late_by_design(connection):
        if age > DELAYED_STALE_AFTER:
            return said(
                SyncState.STALE,
                f"Late by design, and later than that: last synced {holdings_as_of:%Y-%m-%d}.",
            )
        # The remedy says why; there is nothing else to say.
        return said(SyncState.DELAYED_BY_DESIGN, "")
    if age > stale_after:
        return said(SyncState.STALE, f"Last synced {holdings_as_of:%Y-%m-%d %H:%M} UTC.")
    return said(SyncState.CURRENT, "")


def _withheld(account: Json) -> str:
    """Why nothing is recorded for an account this time, or empty."""
    holdings = _dict(_dict(account.get("sync_status")).get("holdings"))
    if holdings.get("initial_sync_completed") is False:
        return "SnapTrade's first sync is not finished, so its positions are not yet all there."
    if holdings.get("holdings_unavailable") is True:
        return "The brokerage does not show SnapTrade this account's holdings."
    return ""


# ── Holdings ─────────────────────────────────────────────────────────────────


def _side(quantity: Decimal) -> Side:
    return Side.SHORT if quantity < 0 else Side.LONG


def _currency(value: Any) -> str:
    code = value.get("code") if isinstance(value, dict) else value
    code = _text(code).upper()
    return code if _CURRENCY.match(code) else ""


def asset_class(kind: str) -> str:
    """The platform's asset class for one of SnapTrade's instrument kinds, by
    meridian.AssetClass's lowercase name; "" for a kind not mapped, cash
    among them, so the class is left for a person to set."""
    return _ASSET_CLASS.get(kind, "")


def position_holding(
    position: Json, fallback_currency: str, problems: list[str] | None = None
) -> Holding:
    """One position from `positions/all`, as a row. A part of it that cannot
    be read exactly, its average cost or its lots, is left out and said in
    `problems`; the row stands without it."""
    said = problems if problems is not None else []
    instrument = _dict(position.get("instrument"))
    kind = _text(instrument.get("kind"))
    symbol = _text(instrument.get("symbol"))
    identifiers: list[Identifier] = []
    figi = _text(_dict(instrument.get("figi_instrument")).get("figi_code"))
    if figi:
        identifiers.append(Identifier("figi", figi))
    if symbol:
        # A ticker, or for an option its OCC symbol: SnapTrade's own symbol,
        # scoped to it until identifier schemes have agreed names.
        identifiers.append(Identifier("symbol", symbol, SOURCE))
    if not identifiers:
        raise ValueError(f"a {kind or 'position'} with neither FIGI nor symbol")
    named = symbol or figi
    quantity = to_decimal(position.get("units"), f"units of {named}")
    exchange = _text(instrument.get("exchange")).upper()
    stated = _currency(position.get("currency"))
    listed = _currency(instrument.get("currency"))
    currency = stated or listed or fallback_currency
    side = _side(quantity)
    average_cost: meridian.Money | None = None
    if position.get("cost_basis") is not None:
        try:
            average_cost = meridian.Money(
                wire_decimal(position["cost_basis"], f"the average cost of {named}"), currency
            )
        except ValueError as refused:
            said.append(f"{refused}; it is not sent")
    try:
        lots = _lots(position.get("tax_lots"), side, currency, named)
    except ValueError as refused:
        said.append(f"{refused}; none of {named}'s lots is sent")
        lots = ()
    return Holding(
        identifiers=tuple(identifiers),
        description=_text(instrument.get("description")) or symbol,
        kind=kind,
        side=side,
        quantity=quantity,
        currency=currency,
        currency_assumed=not stated,
        # SnapTrade reports no market value, and one is never made from `price`.
        market_value=None,
        exchange_mic=exchange if _MIC.match(exchange) else "",
        cash_equivalent=position.get("cash_equivalent") is True,
        asset_class=asset_class(kind),
        average_cost=average_cost,
        lots=lots,
    )


def _lots(given: Any, side: Side, currency: str, named: str) -> tuple[Lot, ...]:
    """A position's `tax_lots`, each read exactly, or ValueError naming the
    first that cannot be: then none is sent. Absent, null or empty is none."""
    if given is None:
        return ()
    if not isinstance(given, list):
        raise ValueError(f"{named}'s tax lots are not a list")
    return tuple(
        _lot(lot, side, currency, f"lot {i + 1} of {named}") for i, lot in enumerate(given)
    )


def _lot(lot: Any, side: Side, currency: str, named: str) -> Lot:
    if not isinstance(lot, dict):
        raise ValueError(f"{named} is not an object")
    if lot.get("quantity") is None:
        raise ValueError(f"{named} has no quantity")
    quantity = wire_decimal(lot["quantity"], f"the quantity of {named}")
    position_type = _text(lot.get("position_type")).upper()
    if position_type and position_type != side.name:
        raise ValueError(f"{named} is {position_type.lower()} on a {side.value} holding")
    if quantity < 0 and side is Side.LONG:
        raise ValueError(f"{named} has a negative quantity on a long holding")
    cost = lot.get("cost_basis")
    purchased = lot.get("original_purchase_date")
    acquired = ""
    if purchased is not None:
        part = _DATE_PART.match(purchased) if isinstance(purchased, str) else None
        try:
            acquired = date.fromisoformat(part.group(1)).isoformat() if part else ""
        except ValueError:
            acquired = ""
        if not acquired:
            raise ValueError(f"{named} has a purchase date that is not one: {purchased!r}")
    return Lot(
        # SnapTrade's magnitude, signed as its holding: a short holding's lots
        # are short.
        quantity=-abs(quantity) if side is Side.SHORT else quantity,
        cost=None
        if cost is None
        else meridian.Money(wire_decimal(cost, f"the cost of {named}"), currency),
        acquired_date=acquired,
    )


def cash_holding(balance: Json) -> Holding | None:
    """One currency's cash from `balances`, as a holding of its cash
    instrument; None when SnapTrade reported no cash figure for it."""
    code = _currency(balance.get("currency"))
    if not code:
        # Never a pseudo-currency, and never a guess at which it was.
        raise ValueError("a balance with no ISO 4217 currency")
    if balance.get("cash") is None:
        return None
    amount = to_decimal(balance.get("cash"), f"{code} cash")
    return Holding(
        identifiers=(Identifier("iso4217", code),),
        description=f"{code} cash",
        kind="cash",
        # A holding of a currency's cash instrument (the product owner,
        # 2026-10-01).
        asset_class="cash",
        # A margin debit is negative cash: a short row of the currency.
        side=_side(amount),
        quantity=amount,
        currency=code,
        # The cash itself, in its own currency, is what SnapTrade reported.
        market_value=meridian.Money(amount, code),
    )


def _merged(holdings: Iterable[Holding]) -> tuple[tuple[Holding, ...], tuple[str, ...]]:
    """One row per instrument and side (W2's invariant under the spec): two
    rows SnapTrade gave for the same instrument and side are summed, and what
    is said about it. Their lots are both rows' where both list lots, and none
    otherwise, since part of a list is not the list; two average costs are
    never combined into one, so the row has none."""
    rows: dict[tuple[tuple[Identifier, ...], Side], Holding] = {}
    problems: list[str] = []
    for holding in holdings:
        key = (holding.identifiers, holding.side)
        held = rows.get(key)
        if held is None:
            rows[key] = holding
            continue
        named = holding.identifiers[-1].value
        if held.average_cost is not None or holding.average_cost is not None:
            problems.append(
                f"SnapTrade listed {named} {holding.side.value} twice; its two average "
                "costs are not combined, so none is sent"
            )
        if bool(held.lots) != bool(holding.lots):
            problems.append(
                f"SnapTrade listed {named} {holding.side.value} twice, with lots only "
                "once; none of its lots is sent"
            )
        rows[key] = replace(
            held,
            quantity=held.quantity + holding.quantity,
            cash_equivalent=held.cash_equivalent or holding.cash_equivalent,
            average_cost=None,
            lots=held.lots + holding.lots if held.lots and holding.lots else (),
        )
    return tuple(rows.values()), tuple(problems)


def statement_id(external_account_id: str, read_at: datetime) -> str:
    """The connector's statement ID: the account and the moment it was read."""
    return f"{SOURCE}:{external_account_id}:{ns(read_at)}"


def statement(
    account: ExternalAccount,
    positions: Json,
    balances: list[Json],
    read_at: datetime,
    freshness: Freshness,
    total: Any = None,
) -> tuple[Statement, tuple[str, ...]]:
    """The statement for one account, and what could not be put in it.
    `total` is the account's `balance.total` as SnapTrade listed it."""
    problems: list[str] = []
    cash: list[Holding] = []
    buying_power: list[meridian.Money] = []
    for balance in balances:
        try:
            row = cash_holding(balance)
            if row is not None:
                cash.append(row)
            if balance.get("buying_power") is not None:
                code = _currency(balance.get("currency"))
                amount = to_decimal(balance["buying_power"], f"{code} buying power")
                buying_power.append(meridian.Money(amount, code))
        except ValueError as refused:
            problems.append(str(refused))
    # A position whose currency SnapTrade states neither for the position nor
    # for its listing is taken to be in the account's only cash currency, or
    # in US dollars when the account has several or none. Either way it is
    # marked as this plugin's assumption, never passed off as SnapTrade's.
    fallback = cash[0].currency if len({row.currency for row in cash}) == 1 else "USD"
    rows: list[Holding] = []
    for position in positions.get("results") or []:
        try:
            rows.append(position_holding(_dict(position), fallback, problems))
        except ValueError as refused:
            problems.append(str(refused))
    holdings, merging = _merged(rows + cash)
    problems.extend(merging)
    net_liquidation: meridian.Money | None = None
    try:
        net_liquidation = _total(total)
    except ValueError as refused:
        problems.append(f"{refused}; no net liquidation is sent")
    as_of = (
        _day(_dict(positions.get("data_freshness")).get("as_of"))
        or (freshness.holdings_as_of.date() if freshness.holdings_as_of else None)
        or read_at.date()
    )
    return (
        Statement(
            external_statement_id=statement_id(account.external_account_id, read_at),
            as_of_date=as_of.isoformat(),
            read_at_ns=ns(read_at),
            holdings=holdings,
            buying_power=tuple(buying_power),
            net_liquidation=net_liquidation,
        ),
        tuple(problems),
    )


def _total(total: Any) -> meridian.Money | None:
    """The account's `balance.total`, `{amount, currency}`, as its net
    liquidation; None where SnapTrade gives no amount."""
    if not isinstance(total, dict) or total.get("amount") is None:
        return None
    code = _currency(total.get("currency"))
    if not code:
        raise ValueError("the account's total value names no ISO 4217 currency")
    return meridian.Money(wire_decimal(total["amount"], f"the account's {code} total"), code)


# ── A whole read ─────────────────────────────────────────────────────────────


def views(snapshot: Snapshot, stale_after: timedelta) -> tuple[ConnectionView, ...]:
    """Every connection, with its accounts, as this read saw them."""
    by_connection: dict[str, list[AccountView]] = {}
    connections = {_text(c.get("id")): c for c in snapshot.connections}
    for raw in snapshot.accounts:
        connection = connections.get(_text(raw.get("brokerage_authorization")), {})
        account = external_account(raw, connection)
        fresh = freshness(raw, connection, snapshot.read_at, stale_after)
        withheld = _withheld(raw)
        failure = snapshot.failures.get(account.snaptrade_account_id, "")
        made: Statement | None = None
        problems: tuple[str, ...] = ()
        if not withheld and failure:
            withheld = failure
        if not withheld:
            made, problems = statement(
                account,
                snapshot.positions.get(account.snaptrade_account_id, {}),
                snapshot.balances.get(account.snaptrade_account_id, []),
                snapshot.read_at,
                fresh,
                _dict(raw.get("balance")).get("total"),
            )
        by_connection.setdefault(account.connection_id, []).append(
            AccountView(account, fresh, made, withheld, problems)
        )
    # An account whose connection SnapTrade did not list is still shown, under
    # a connection known only by its ID.
    known = list(connections) + [key for key in by_connection if key not in connections]
    return tuple(
        _connection_view(connections.get(key, {"id": key}), tuple(by_connection.get(key, ())))
        for key in known
    )


def _connection_view(connection: Json, accounts: tuple[AccountView, ...]) -> ConnectionView:
    brokerage = _dict(connection.get("brokerage"))
    if connection.get("disabled") is True:
        state, detail = SyncState.DISABLED, "SnapTrade disabled this connection."
    elif accounts:
        # The worst of its accounts', in the order a person should act on them:
        # what a person must do before what waiting may mend.
        order = [
            SyncState.DISABLED,
            SyncState.NEEDS_SIGN_IN,
            SyncState.HOLDINGS_UNAVAILABLE,
            SyncState.STALE,
            SyncState.DELAYED_BY_DESIGN,
            SyncState.CURRENT,
        ]
        worst = min(accounts, key=lambda view: order.index(view.freshness.state))
        state, detail = worst.freshness.state, worst.freshness.detail
    elif late_by_design(connection):
        state, detail = SyncState.DELAYED_BY_DESIGN, ""
    else:
        state, detail = SyncState.CURRENT, ""
    return ConnectionView(
        connection_id=_text(connection.get("id")),
        name=_text(connection.get("name")),
        institution=_text(brokerage.get("display_name")) or _text(brokerage.get("name")),
        access=_text(connection.get("type")),
        state=state,
        detail=detail,
        disabled_at=_moment(connection.get("disabled_date")),
        accounts=accounts,
        serving=serving(connection),
    )
