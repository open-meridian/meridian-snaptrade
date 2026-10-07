"""What this version declares beside its role (W8.1, W4.1, contract v11).

Built from the plugin's code and used twice: `__main__` registers with it,
and `meridian plugin upload` reads the same one from the built image
(pyproject.toml's `[tool.meridian] declaration`). Its secret settings' names
are the secret ones settings.py declares; nothing about what it supports, which
the custody role says.

What SnapTrade sends and this plugin does not carry, by name only, each with
why (spec/vendor-differences-have-a-place-in-the-contract, Q15): the evidence
that the common model may need to grow. Each is counted as it is seen, and
the count rides on the heartbeat, never leaving the deployment.

And the storage it asks for, at the edge, for SnapTrade's raw responses
(decisions/028), with the two kinds of raw record it keeps there (contract
v16; meridian-design spec/an-edge-plugins-older-records-move-to-the-
archive): a reported activity's record, seven years in storage by default
(the custody guidance of 2026-10-05), and each read's raw responses, 30 days
by default; both archivable, since a unit of either is files written once.
The SDK declares each kind's two settings from these, its window and what is
done past it (settings.py), and the plugin moves its records past their
window itself (archive.py). The retention stays the longest a window may be,
the SDK's bound, so the deployment never keeps the storage for less than an
admin may choose.
"""

from __future__ import annotations

from meridian.declaration import Declaration, NotCarried, RecordKind, Storage

from .normalise import to_decimal
from .raw import MOST_ACTIVITY_RETENTION_DAYS
from .settings import (
    ACTIVITY,
    DECLARED,
    DEFAULT_ACTIVITY_RETENTION_DAYS,
    DEFAULT_RAW_RETENTION_DAYS,
    RESPONSES,
)
from .venue import Snapshot

#: The kinds of raw record this version keeps, each with its default window
#: and whether a unit of it can be archived (W4.1, contract v16).
ACTIVITY_KIND = RecordKind(
    ACTIVITY, "Reported activity", window_days=DEFAULT_ACTIVITY_RETENTION_DAYS, archivable=True
)
RESPONSES_KIND = RecordKind(
    RESPONSES, "Raw responses", window_days=DEFAULT_RAW_RETENTION_DAYS, archivable=True
)
KINDS = (ACTIVITY_KIND, RESPONSES_KIND)

POSITION = "snaptrade:position"
LOT = "snaptrade:tax-lot"
ACCOUNT = "snaptrade:account"
ACTIVITY = "snaptrade:activity"

#: Each name not carried, with why: none has a meaning in the contract yet.
NOT_CARRIED = (
    NotCarried("custody", POSITION, "price", "no_contract_meaning"),
    NotCarried("custody", POSITION, "open_pnl", "no_contract_meaning"),
    NotCarried("custody", LOT, "lot_id", "no_contract_meaning"),
    NotCarried("custody", ACCOUNT, "is_paper", "no_contract_meaning"),
    NotCarried("custody", ACTIVITY, "fee", "no_contract_meaning"),
    NotCarried("custody", ACTIVITY, "fx_rate", "no_contract_meaning"),
)

DECLARATION = Declaration(
    settings=DECLARED,
    not_carried=NOT_CARRIED,
    storage=Storage(retention_days=MOST_ACTIVITY_RETENTION_DAYS, kinds=KINDS),
)


def seen_in_position(position: dict[str, object]) -> list[tuple[str, str]]:
    """The names not carried one of SnapTrade's positions shows, each once."""
    found = [
        (POSITION, name) for name in ("price", "open_pnl") if position.get(name) is not None
    ]
    lots = position.get("tax_lots")
    if isinstance(lots, list) and any(
        isinstance(lot, dict) and lot.get("lot_id") for lot in lots
    ):
        found.append((LOT, "lot_id"))
    return found


def seen_in_account(account: dict[str, object]) -> list[tuple[str, str]]:
    """The names not carried one of SnapTrade's accounts shows."""
    return [(ACCOUNT, "is_paper")] if account.get("is_paper") is not None else []


def seen_in_activity(activity: dict[str, object]) -> list[tuple[str, str]]:
    """The names not carried one of SnapTrade's activities shows: a fee or
    an exchange rate it states, which a custodial activity has no place for."""
    return [(ACTIVITY, name) for name in ("fee", "fx_rate") if _stated(activity.get(name))]


def _stated(value: object) -> bool:
    """Whether SnapTrade stated a value: a number other than 0, which it
    writes where none applies, or anything else not empty."""
    if value is None or value == "":
        return False
    try:
        return to_decimal(value, "a value") != 0
    except ValueError:
        return True


def seen(snapshot: Snapshot) -> list[tuple[str, str]]:
    """Each name not carried a read of SnapTrade showed, once for each
    account or position that showed it: what the heartbeat counts."""
    found = [name for account in snapshot.accounts for name in seen_in_account(account)]
    for body in snapshot.positions.values():
        results = body.get("results") if isinstance(body, dict) else None
        for position in results or []:
            if isinstance(position, dict):
                found.extend(seen_in_position(position))
    for listed in snapshot.activities.values():
        for activity in listed:
            found.extend(seen_in_activity(activity))
    return found
