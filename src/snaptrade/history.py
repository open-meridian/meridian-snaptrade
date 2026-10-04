"""An account's history as SnapTrade reports it, and lots proposed from it
(the product owner, 2026-10-04: "Yes to both, start them"; meridian-design
tasks/sdk-contract/snaptrade-offers-its-history-as-tools).

Two reads, each a page for a person and a read tool for an agent (contract
v12), at `write` and `read`, for an account the person may read:

- **Activities over a range** (`read_account_activities`): SnapTrade's
  account activities -- buys, sells, transfers, dividends -- traded from
  `start` to `end`, a page of them at a time, each as SnapTrade reports it,
  with the date of the account's first transaction SnapTrade holds
  (`first_transaction_date` in its sync status), so a reader knows how far
  back its history goes. A range is at most `MOST_DAYS` days and a page at
  most `MOST_LIMIT` activities; a refusal names the field by its path.
- **Lots proposed, never confirmed** (`read_proposed_lots`): for each
  position of the account's last statement that SnapTrade lists no tax lots
  for, lots proposed from its purchases in the activities, each naming its
  source: a lot is specific (the product owner, 2026-10-04: "lots need to be
  specific"), one purchase, its units, the amount paid for it and its trade
  date, as the activity states them ("SnapTrade activities, BUY on <date>,
  <id>"). Its fields are those an opening balance's lot takes
  (the sample operations plugin's `LotDraft`: quantity, cost, currency,
  acquired, source), for a person or an agent to carry into the draft and
  answer for there; nothing here sends a lot anywhere, and the statement
  carries only SnapTrade's own lots (the street's `lots`, as reported).

**What a proposal does not do.** No rule SnapTrade does not state: a
position SnapTrade's history shows sold from is not split into lots by FIFO
or any other order; a position that arrived by transfer, or that a corporate
action changed (a split, a stock dividend, an adjustment, an option
exercised or assigned), gets no lot from its history, which states no cost
or acquisition date for it, and says so. A position whose purchases do not
add up to what it holds -- its history starting after it was bought -- gets
none from them either. A short position and an option get none at all:
nothing SnapTrade reports states a short lot's terms, and an option's
activities name it by its option symbol, which is not matched here. A
position SnapTrade lists lots for gets nothing proposed: its lots are the
statement's. A position that gets no lot says why.

**The average purchase price** is SnapTrade's `cost_basis` on a position
from `positions/all`, its average per unit, which the statement already
carries as reported (`average_cost`, contract v7, its raw record beside
it). It is shown beside each position, as reported, for a person typing a
cost themselves, and never made into a lot: nothing multiplies a per-unit
average by a quantity (W2.3's Q-A, which binds a proposal too: the product
owner, 2026-10-04).

**The edge keeps its own** (decisions/028): every page of activities read
here is kept as a read of the account in the plugin's raw records (raw.py),
and each answer names the record it came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .normalise import Holding, Side, to_decimal
from .raw import activities_call, activities_failed
from .venue import MOST_PER_PAGE, Json, Venue, VenueError

#: The longest range read at once, in days: a year, leap day included.
MOST_DAYS = 366
#: The range read where none is given: the 30 days to its end.
DEFAULT_DAYS = 30
#: The most activities in one page, as SnapTrade answers at most, and the
#: page where none is given.
MOST_LIMIT = MOST_PER_PAGE
DEFAULT_LIMIT = 100
#: The furthest into a range a page may start.
MOST_OFFSET = 100_000
#: The most pages of a whole history read to propose lots: past this, its
#: history is not read whole, and nothing is proposed from it.
MOST_HISTORY_PAGES = 10
#: The longest account ID asked for.
MOST_ACCOUNT = 200

# SnapTrade's activity types (its API's `type`), as they bear on a holding.
#: An acquisition, stating its units, the amount paid and its trade date.
ACQUIRED = frozenset({"BUY", "REI"})
#: A disposal: which acquisitions it closed is not stated.
SOLD = frozenset({"SELL"})
#: Arrived or left by transfer: no cost or acquisition date stated.
TRANSFERRED = frozenset(
    {"TRANSFER", "EXTERNAL_ASSET_TRANSFER_IN", "EXTERNAL_ASSET_TRANSFER_OUT"}
)
#: A corporate action, or an option's, changing the holding.
CORPORATE = frozenset(
    {
        "SPLIT",
        "STOCK_DIVIDEND",
        "ADJUSTMENT",
        "OPTIONASSIGNMENT",
        "OPTIONEXERCISE",
        "OPTIONEXPIRATION",
    }
)
#: Cash alone, whatever units it names: it moves no shares.
CASH_ONLY = frozenset(
    {"DIVIDEND", "SUBSTITUTE_DIVIDEND", "INTEREST", "FEE", "TAX", "CONTRIBUTION", "WITHDRAWAL"}
)

#: A proposal's origin, as `PositionLots.proposed_from` says it.
FROM_ACTIVITIES = "activities"


# ── What the tools take and answer ──────────────────────────────────────────


@dataclass(frozen=True)
class HistoryAsked:
    """Which account's activities, traded over which days, and which page."""

    account: str = field(
        default="",
        metadata={
            "max_length": MOST_ACCOUNT,
            "description": "the deployment's account, one the person may read",
        },
    )
    start: date | None = field(
        default=None,
        metadata={"description": f"the first trade date read; {DEFAULT_DAYS} days before end"},
    )
    end: date | None = field(
        default=None,
        metadata={"description": "the last trade date read; today where not given"},
    )
    offset: int = field(
        default=0,
        metadata={
            "min": 0,
            "max": MOST_OFFSET,
            "description": "the first activity of the page",
        },
    )
    limit: int = field(
        default=DEFAULT_LIMIT,
        metadata={
            "min": 1,
            "max": MOST_LIMIT,
            "description": "the most activities in the page",
        },
    )


@dataclass(frozen=True)
class Activity:
    """One activity, as SnapTrade reports it: its type in SnapTrade's words,
    its dates as written, and every number as written, none where it gave
    none."""

    id: str
    type: str
    trade_date: str
    settlement_date: str
    symbol: str
    option_symbol: str
    description: str
    units: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    fee: Decimal | None
    currency: str


@dataclass(frozen=True)
class Activities:
    """A page of an account's activities over a range, as SnapTrade reports
    them, with how far back its history goes and the raw record kept."""

    account: str
    external_account_id: str
    connection: str
    history_from: str = field(
        metadata={
            "description": "the account's first transaction SnapTrade holds "
            "(first_transaction_date), as reported; empty where it says none"
        }
    )
    start: date
    end: date
    offset: int
    limit: int
    total: int | None = field(
        metadata={"description": "how many the range holds, as SnapTrade says; null where not"}
    )
    activities: list[Activity] = field(default_factory=list)
    raw_record: str = ""
    said: str = ""


@dataclass(frozen=True)
class LotsAsked:
    """Which account's positions to propose lots for."""

    account: str = field(
        default="",
        metadata={
            "max_length": MOST_ACCOUNT,
            "description": "the deployment's account, one the person may read",
        },
    )


@dataclass(frozen=True)
class ProposedLot:
    """One lot proposed, as an opening balance's lot takes it: never
    confirmed here, and naming where it came from."""

    quantity: Decimal
    cost: Decimal | None = field(metadata={"description": "the lot's cost, in all"})
    currency: str
    acquired: date | None = field(
        metadata={"description": "when it was acquired; null for the person to supply"}
    )
    source: str


@dataclass(frozen=True)
class PositionLots:
    """One position of the account's last statement, its average purchase
    price as SnapTrade reports it, and the lots proposed for it, or why
    none is."""

    instrument: str
    description: str
    kind: str
    side: str
    quantity: Decimal
    currency: str
    average_purchase_price: Decimal | None = field(
        metadata={"description": "SnapTrade's average per unit, as reported; null where none"}
    )
    average_source: str
    lots_reported: int = field(metadata={"description": "the tax lots SnapTrade lists"})
    proposed_from: str = field(metadata={"description": "activities, or empty: none proposed"})
    proposed: list[ProposedLot] = field(default_factory=list)
    said: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProposedLots:
    """The lots proposed for an account's positions that SnapTrade lists no
    tax lots for, from its last statement and its whole history."""

    account: str
    external_account_id: str
    statement: str
    as_of: str
    read_at: str
    history_from: str
    history_read: str
    raw_record: str
    positions: list[PositionLots] = field(default_factory=list)
    said: str = ""


# ── Reading SnapTrade's activities ──────────────────────────────────────────


def _dict(value: Any) -> Json:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> Decimal | None:
    """A number as SnapTrade wrote it, or None where it gave none or one
    that is not a number: shown as not reported, never guessed."""
    if value is None:
        return None
    try:
        return to_decimal(value, "a number")
    except ValueError:
        return None


def activity(given: Json) -> Activity:
    """One of SnapTrade's activities, as reported."""
    currency = given.get("currency")
    return Activity(
        id=_text(given.get("id")),
        type=_text(given.get("type")).upper(),
        trade_date=_text(given.get("trade_date")),
        settlement_date=_text(given.get("settlement_date")),
        symbol=_text(_dict(given.get("symbol")).get("symbol")),
        option_symbol=_text(_dict(given.get("option_symbol")).get("ticker")),
        description=_text(given.get("description")),
        units=_number(given.get("units")),
        price=_number(given.get("price")),
        amount=_number(given.get("amount")),
        fee=_number(given.get("fee")),
        currency=_text(_dict(currency).get("code") if isinstance(currency, dict) else currency),
    )


def day_of(activity: Activity) -> date | None:
    """The date part of an activity's trade date, as written."""
    try:
        return date.fromisoformat(activity.trade_date[:10])
    except ValueError:
        return None


def range_note(start: date | None, end: date, offset: int, limit: int) -> str:
    """What one page asked for, as its raw record notes it."""
    since = start.isoformat() if start is not None else "the first SnapTrade holds"
    return f"traded from {since} to {end.isoformat()}; {limit} from {offset}"


@dataclass(frozen=True)
class Whole:
    """An account's whole history as read to propose lots: its activities,
    or None with why where it could not be read whole; and each page's call,
    for the raw record."""

    activities: list[Activity] | None
    calls: list[Json]
    said: str


async def read_whole(venue: Venue, account_id: str, start: date | None, end: date) -> Whole:
    """Every activity SnapTrade holds for the account from `start` to `end`,
    page by page, up to MOST_HISTORY_PAGES pages."""
    calls: list[Json] = []
    found: list[Activity] = []
    for page in range(MOST_HISTORY_PAGES):
        offset = page * MOST_PER_PAGE
        note = range_note(start, end, offset, MOST_PER_PAGE)
        try:
            got = await venue.activity_page(account_id, start, end, offset, MOST_PER_PAGE)
        except VenueError as failed:
            calls.append(activities_failed(str(failed), note))
            return Whole(None, calls, f"SnapTrade's activities could not be read: {failed}")
        calls.append(activities_call(got.body, note))
        found.extend(activity(each) for each in got.activities)
        if len(got.activities) < MOST_PER_PAGE or (
            got.total is not None and offset + len(got.activities) >= got.total
        ):
            return Whole(found, calls, f"{len(found)} activities read")
    return Whole(
        None,
        calls,
        f"its history holds more than {MOST_HISTORY_PAGES * MOST_PER_PAGE} activities, more "
        "than is read at once",
    )


# ── Proposing lots ──────────────────────────────────────────────────────────


def moment(read_at: datetime) -> str:
    """A read's time, as a source names it."""
    return f"{read_at:%Y-%m-%d %H:%M} UTC"


def _symbol(holding: Holding) -> str:
    return next((i.value for i in holding.identifiers if i.scheme == "symbol"), "")


def _since(history_from: str) -> str:
    return f"from {history_from}" if history_from else "as far back as SnapTrade holds it"


def _plural(count: int, one: str) -> str:
    return f"{count} {one}{'' if count == 1 else 's'}"


def from_activities(
    holding: Holding, history: Sequence[Activity], history_from: str
) -> tuple[list[ProposedLot], str]:
    """The lots a long position's purchases in its history state, one per
    purchase, where they are all of it and nothing else moved it; or none,
    and why."""
    symbol = _symbol(holding)
    mine = [a for a in history if a.symbol == symbol and not a.option_symbol]
    for each in mine:
        if each.type in ACQUIRED | SOLD | CASH_ONLY:
            continue
        if each.type not in TRANSFERRED | CORPORATE and not each.units:
            continue
        when = day_of(each)
        on = f" on {when.isoformat()}" if when is not None else ""
        how = (
            "it arrived or left by transfer"
            if each.type in TRANSFERRED
            else "a corporate action changed it"
            if each.type in CORPORATE
            else "an activity SnapTrade does not class moved it"
        )
        return [], (
            f"No lot from its history: {how} ({each.type}{on}, {each.id}), for which "
            "SnapTrade states no cost or acquisition date."
        )
    sold = [a for a in mine if a.type in SOLD]
    if sold:
        return [], (
            f"No lot from its history: SnapTrade's history shows {_plural(len(sold), 'sale')} "
            "of it, and which purchases a sale closed is a rule SnapTrade does not state; "
            "none is applied here (no FIFO)."
        )
    bought = [a for a in mine if a.type in ACQUIRED]
    if not bought:
        return [], (
            f"No lot from its history: SnapTrade's history {_since(history_from)} shows no "
            f"purchase of {symbol}."
        )
    lots: list[ProposedLot] = []
    for each in bought:
        when = day_of(each)
        named = f"{each.type} {each.id}"
        if each.units is None or each.units <= 0:
            return [], f"No lot from its history: {named} states no units bought."
        if each.amount is None or each.amount >= 0:
            return [], f"No lot from its history: {named} states no amount paid."
        if when is None:
            return [], f"No lot from its history: {named} states no trade date."
        if not each.currency:
            return [], f"No lot from its history: {named} states no currency."
        lots.append(
            ProposedLot(
                quantity=each.units,
                # The amount paid: SnapTrade writes cash leaving the account
                # negative, so the lot's cost is that amount without its sign.
                cost=-each.amount,
                currency=each.currency,
                acquired=when,
                source=f"SnapTrade activities, {each.type} on {when.isoformat()}, {each.id}",
            )
        )
    bought_units = sum((lot.quantity for lot in lots), Decimal(0))
    if bought_units != holding.quantity:
        return [], (
            f"No lot from its history: its purchases {_since(history_from)} come to "
            f"{format(bought_units, 'f')}, and it holds {format(holding.quantity, 'f')}, so "
            "its history does not account for all of it."
        )
    lots.sort(key=lambda lot: lot.acquired or date.min)
    return lots, ""


def propose(
    holding: Holding,
    history: Sequence[Activity] | None,
    unread: str,
    history_from: str,
    read_at: datetime,
) -> PositionLots:
    """One position's lots proposed from its history, or why none is.
    `history` is the account's whole history, None where it could not be
    read whole (`unread` says why). Its average purchase price is beside it,
    as reported, never a lot."""
    average = holding.average_cost
    said: list[str] = []
    proposed: list[ProposedLot] = []
    proposed_from = ""

    def answer() -> PositionLots:
        return PositionLots(
            instrument=_symbol(holding) or holding.identifiers[-1].value,
            description=holding.description,
            kind=holding.kind,
            side=holding.side.value,
            quantity=holding.quantity,
            currency=holding.currency,
            average_purchase_price=Decimal(average.amount) if average is not None else None,
            average_source=(
                f"SnapTrade's average per unit (positions cost_basis), as reported, read "
                f"{moment(read_at)}"
                if average is not None
                else ""
            ),
            lots_reported=len(holding.lots),
            proposed_from=proposed_from,
            proposed=proposed,
            said=said,
        )

    if holding.lots:
        said.append(
            f"SnapTrade lists {_plural(len(holding.lots), 'tax lot')} for it, on the "
            "statement: nothing is proposed."
        )
        return answer()
    if holding.lots_unread:
        said.append(
            "SnapTrade lists lots for it that could not be read exactly, so none is proposed "
            "in their place."
        )
        return answer()
    if holding.side is Side.SHORT:
        said.append(
            "A short position: nothing SnapTrade reports states a short lot's terms, so none "
            "is proposed."
        )
        return answer()
    if holding.kind == "option":
        said.append(
            "An option: SnapTrade names its activities by option symbol, which is not "
            "matched here, so none is proposed."
        )
        return answer()
    if history is None:
        said.append(f"No lot from its history: {unread}.")
    else:
        lots, why = from_activities(holding, history, history_from)
        if lots:
            proposed, proposed_from = lots, FROM_ACTIVITIES
            return answer()
        said.append(why)
    said.append(
        "Its lots are the person's to supply."
        + (
            " SnapTrade's average purchase price is beside it, as reported, never a lot."
            if average is not None
            else ""
        )
    )
    return answer()
