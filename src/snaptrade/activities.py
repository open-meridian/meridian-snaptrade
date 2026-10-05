"""SnapTrade's account activities, as the custodian states them, converted
for the street (contract v14: W2.10; meridian-design
plans/the-custodians-activity-explains-a-break, "Design" -> SnapTrade).

Each of SnapTrade's activities becomes one `meridian.CustodialActivity`: its
own identifier, the redelivery key; its kind, converted from SnapTrade's type
into the closed list, or not known with the type as reported; the
instrument, resolved as a holding's is; its trade and settlement dates; the
units, price and amount it states; SnapTrade's description; and the raw
record it was converted from. Nothing is netted, merged or derived: activity
is evidence that explains a break, never a source (requirement 6), and a
sweep fund's purchases are sent as SnapTrade lists them.

The rules, each from W2.10's notes:

- **The kinds** (the plan): `BUY` purchase, `SELL` sale, `REI`
  reinvestment, `DIVIDEND`, `INTEREST`, `FEE`, `TAX`, `SPLIT`, a transfer in
  or out (SnapTrade's `EXTERNAL_ASSET_`, `INTERNAL_ASSET_` and
  `INTERNAL_CASH_TRANSFER_IN` and `_OUT`), `CONTRIBUTION`, `WITHDRAWAL`, and
  `JOURNALED` journal. `STOCK_DIVIDEND`, shares a corporate action added, is
  a corporate action other than a split. Any other type -- an option's
  expiry, an adjustment, a plain `TRANSFER` that says no direction -- is
  sent not known, with SnapTrade's type beside it as reported, for a person
  to map.
- **Signs are the platform's, set at the edge:** units added to the account
  are positive and units removed negative, so a purchase, a reinvestment and
  a transfer in are sent positive and a sale, a transfer out, and a fee or
  tax taken in units negative, whichever sign SnapTrade wrote; a split's
  units are as SnapTrade states them (a reverse split removes). The amount
  is SnapTrade's own, which already writes cash coming in positive and cash
  going out negative.
- **A value not stated is unset, never zero.** SnapTrade writes 0 where a
  value does not apply -- a cash dividend's units and price, a split's
  amount -- so a zero is sent unset; and a price is never worked out from
  the amount and the units.
- **The instrument, resolved as a holding's is:** an activity naming a
  security the account holds is resolved by that holding's identifiers, to
  the same instrument. A plan's own fund code (Fidelity's `OQKR` for the
  plan's VIGIX) that a person linked, in the plugin's settings, to an
  instrument record is that record, carrying who linked it and when as its
  provenance (requirement 3). Any other code SnapTrade names travels as
  reported, the instrument empty: resolving a symbol only an old activity
  names would mint a record for the deployment's admin to complete for
  every security the account ever traded, so nothing past the plugin
  interprets it until a person links it.
- **Exact or not at all:** a number that would not cross the wire exactly is
  caught here, and that activity is not sent, saying why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import meridian
from meridian.bounds import (
    AS_REPORTED_CODE_LENGTH,
    AS_REPORTED_TEXT_LENGTH,
    CUSTODIAL_ACTIVITY_DESCRIPTION_LENGTH,
    CUSTODIAL_ACTIVITY_EXTERNAL_ACTIVITY_ID_LENGTH,
)
from meridian.edge import as_reported

from .normalise import wire_decimal
from .plan_codes import PlanCodeLink, link_for
from .venue import Json

__all__ = ["PlanCodeLink", "link_for"]

#: Whose vocabulary an activity's type is, where it converts to no kind.
ACTIVITY_TYPE_SCHEME = "snaptrade:activity-type"
#: Whose vocabulary an activity's security code is, where it resolves to none.
SYMBOL_SCHEME = "snaptrade:symbol"

_K = meridian.ActivityKind

#: SnapTrade's activity types, each the kind it converts to (the plan's list).
KINDS: dict[str, meridian.ActivityKind] = {
    "BUY": _K.ACTIVITY_KIND_PURCHASE,
    "SELL": _K.ACTIVITY_KIND_SALE,
    "REI": _K.ACTIVITY_KIND_REINVESTMENT,
    "DIVIDEND": _K.ACTIVITY_KIND_DIVIDEND,
    "INTEREST": _K.ACTIVITY_KIND_INTEREST,
    "FEE": _K.ACTIVITY_KIND_FEE,
    "TAX": _K.ACTIVITY_KIND_TAX,
    "SPLIT": _K.ACTIVITY_KIND_SPLIT,
    "STOCK_DIVIDEND": _K.ACTIVITY_KIND_CORPORATE_ACTION,
    "EXTERNAL_ASSET_TRANSFER_IN": _K.ACTIVITY_KIND_TRANSFER_IN,
    "INTERNAL_ASSET_TRANSFER_IN": _K.ACTIVITY_KIND_TRANSFER_IN,
    "INTERNAL_CASH_TRANSFER_IN": _K.ACTIVITY_KIND_TRANSFER_IN,
    "EXTERNAL_ASSET_TRANSFER_OUT": _K.ACTIVITY_KIND_TRANSFER_OUT,
    "INTERNAL_ASSET_TRANSFER_OUT": _K.ACTIVITY_KIND_TRANSFER_OUT,
    "INTERNAL_CASH_TRANSFER_OUT": _K.ACTIVITY_KIND_TRANSFER_OUT,
    "CONTRIBUTION": _K.ACTIVITY_KIND_CONTRIBUTION,
    "WITHDRAWAL": _K.ACTIVITY_KIND_WITHDRAWAL,
    "JOURNALED": _K.ACTIVITY_KIND_JOURNAL,
}

#: The kinds whose units the account gained, and those whose units it lost.
ADDS_UNITS = frozenset(
    {_K.ACTIVITY_KIND_PURCHASE, _K.ACTIVITY_KIND_REINVESTMENT, _K.ACTIVITY_KIND_TRANSFER_IN}
)
REMOVES_UNITS = frozenset(
    {
        _K.ACTIVITY_KIND_SALE,
        _K.ACTIVITY_KIND_TRANSFER_OUT,
        _K.ACTIVITY_KIND_FEE,
        _K.ACTIVITY_KIND_TAX,
    }
)


@dataclass(frozen=True)
class Converted:
    """One of SnapTrade's activities, converted, ready for its instrument and
    its raw record: everything but what the deployment answers."""

    external_activity_id: str
    type: str
    kind: meridian.ActivityKind
    # The security SnapTrade names, by its symbol or its option's ticker,
    # and its words for it; "" where it names none.
    code: str
    code_text: str
    trade_date: str
    settlement_date: str
    units: Decimal | None
    price: meridian.Money | None
    amount: meridian.Money | None
    description: str

    @property
    def kind_as_reported(self) -> meridian.AsReported | None:
        """SnapTrade's type, where it converts to no kind."""
        if self.kind != _K.ACTIVITY_KIND_UNSPECIFIED:
            return None
        return as_reported(ACTIVITY_TYPE_SCHEME, self.type or "(none)")


def _dict(value: Any) -> Json:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _day(value: Any) -> str:
    """The date part of a date SnapTrade wrote, or "" where it is none."""
    text = _text(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _stated(value: Any, name: str) -> Decimal | None:
    """A number SnapTrade stated, exactly; None where it gave none, or 0,
    which it writes where a value does not apply. Refused, naming it, where
    it would not cross the wire exactly."""
    if value is None or value == "":
        return None
    number = wire_decimal(value, name)
    return None if number == 0 else number


def _clip(text: str, most: int) -> str:
    return text if len(text) <= most else text[: most - 1] + "\N{HORIZONTAL ELLIPSIS}"


def convert(given: Json) -> Converted | str:
    """One of SnapTrade's activities converted, or why it cannot be sent."""
    activity_id = _text(given.get("id"))
    bound = CUSTODIAL_ACTIVITY_EXTERNAL_ACTIVITY_ID_LENGTH
    if not bound.least <= len(activity_id) <= bound.most:
        return "SnapTrade gives it no identifier the street can key it by"
    trade_date = _day(given.get("trade_date"))
    if not trade_date:
        return f"activity {activity_id} states no trade date"
    snaptrade_type = _text(given.get("type")).upper()
    kind = KINDS.get(snaptrade_type, _K.ACTIVITY_KIND_UNSPECIFIED)
    symbol = _dict(given.get("symbol"))
    option = _dict(given.get("option_symbol"))
    code = _text(symbol.get("symbol")) or _text(option.get("ticker"))
    code_text = _text(symbol.get("description")) or code
    currency = given.get("currency")
    currency_code = _text(
        _dict(currency).get("code") if isinstance(currency, dict) else currency
    )
    try:
        units = _stated(given.get("units"), f"activity {activity_id}'s units")
        price = _stated(given.get("price"), f"activity {activity_id}'s price")
        amount = _stated(given.get("amount"), f"activity {activity_id}'s amount")
    except ValueError as inexact:
        return str(inexact)
    if units is not None and kind in ADDS_UNITS:
        units = abs(units)
    elif units is not None and kind in REMOVES_UNITS:
        units = -abs(units)
    if (price is not None or amount is not None) and not currency_code:
        return f"activity {activity_id} states an amount in no currency"
    return Converted(
        external_activity_id=activity_id,
        type=_clip(snaptrade_type, AS_REPORTED_CODE_LENGTH.most),
        kind=kind,
        code=_clip(code, AS_REPORTED_CODE_LENGTH.most),
        code_text=_clip(code_text, AS_REPORTED_TEXT_LENGTH.most),
        trade_date=trade_date,
        settlement_date=_day(given.get("settlement_date")),
        units=units,
        price=None if price is None else meridian.Money(price, currency_code),
        amount=None if amount is None else meridian.Money(amount, currency_code),
        description=_clip(
            _text(given.get("description")), CUSTODIAL_ACTIVITY_DESCRIPTION_LENGTH.most
        ),
    )
