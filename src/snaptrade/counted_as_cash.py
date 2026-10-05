"""The positions an admin of the plugin counts as cash, read from its settings
(the product owner, 2026-10-05; meridian-design tasks/sdk-contract/snaptrade-
counts-a-core-deposit-as-cash).

A custodian may hold an account's cash as a position: Fidelity's core
position in a Traditional IRA is an FDIC-insured bank deposit, which
SnapTrade reports as a position of kind `other`, 2.99 at a price of 1, beside
cash of 0.00. Where SnapTrade itself marks such a position a cash equivalent,
and it is no fund, the plugin sends it as cash by that rule (normalise.py).
Where it does not, an admin of the plugin may say so, once, in the table
setting `counted_as_cash`, declared in settings.py and drawn as the Cash
links tab: optionally the external account it applies to (blank: every
account), SnapTrade's symbol for the position, and the currency it is cash
in, in the order of Plan-code links' columns (the product owner, 2026-10-05).
A row is keyed by column name, so the order it was saved in does not matter.
Nothing is ever decided from what a symbol looks like.

The plugin only reads the setting, as any setting arrives, and never sets it.
Each row arrives with `changed_by` and `changed_at`, which the conductor
stamps when a row is added or changed; the cash a row counts names them as
its provenance, "supplied by" that person at that time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from meridian.bounds import PROVENANCE_PERSON_LENGTH

#: The setting's columns, by name.
SYMBOL = "symbol"
CURRENCY = "currency"
ACCOUNT = "account"

#: An ISO 4217 code as the setting takes it: three letters, in capitals once
#: read. A row whose cell is not one counts nothing, and the account holding
#: its symbol says why (normalise.py).
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class CountedAsCash:
    """A position SnapTrade names by `symbol`, counted as cash in `currency`
    by a named person: on one external account, or on every one where
    `external_account_id` is empty."""

    symbol: str
    #: An ISO 4217 code, or "" where the cell is not one (`given` says what
    #: it was): such a row counts nothing.
    currency: str
    external_account_id: str = ""
    given: str = ""
    #: Who added or last changed the row, and when, as the conductor stamped.
    changed_by: str = ""
    changed_at: str = ""

    @property
    def person(self) -> str:
        """Who said so and when, as the cash's provenance names them, within
        its bound."""
        said = f"{self.changed_by}, {self.changed_at}" if self.changed_at else self.changed_by
        return said[: PROVENANCE_PERSON_LENGTH.most]


def currency_of(cell: object) -> str:
    """A currency cell as the setting holds it, in capitals, or "" where it
    is not an ISO 4217 code's shape."""
    code = cell.strip().upper() if isinstance(cell, str) else ""
    return code if _CURRENCY.match(code) else ""


def rows_of(rows: object) -> tuple[CountedAsCash, ...]:
    """The rows a delivered `counted_as_cash` holds: each naming a symbol, a
    currency and an account or none. A row naming no symbol is no row; one
    whose currency is not a code's shape is kept with none, to say why it
    counts nothing; nothing is guessed from either."""
    if not isinstance(rows, list):
        return ()
    counted: list[CountedAsCash] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = row.get(SYMBOL)
        given = row.get(CURRENCY)
        account = row.get(ACCOUNT) or ""
        if not (isinstance(symbol, str) and symbol.strip() and isinstance(account, str)):
            continue
        counted.append(
            CountedAsCash(
                symbol=symbol.strip(),
                currency=currency_of(given),
                external_account_id=account.strip(),
                given="" if currency_of(given) else str(given or "").strip(),
                changed_by=str(row.get("changed_by", "")),
                changed_at=str(row.get("changed_at", "")),
            )
        )
    return tuple(counted)


def row_for(
    rows: Iterable[CountedAsCash], external_account_id: str, symbol: str
) -> CountedAsCash | None:
    """The row counting this symbol as cash on this account: one naming the
    account before one naming none; None where no row does."""
    matching = [row for row in rows if row.symbol == symbol]
    return next(
        (row for row in matching if row.external_account_id == external_account_id),
        next((row for row in matching if not row.external_account_id), None),
    )
