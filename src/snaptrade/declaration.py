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
(decisions/028): kept for the retention below, the reach of a backfill.
"""

from __future__ import annotations

from meridian.declaration import Declaration, NotCarried, Storage

from .settings import DECLARED
from .venue import Snapshot

#: How long a raw response is kept, in days: what the deployment's storage is
#: asked for, and the default of the plugin's own retention setting.
RETENTION_DAYS = 30

POSITION = "snaptrade:position"
LOT = "snaptrade:tax-lot"
ACCOUNT = "snaptrade:account"

#: Each name not carried, with why: none has a meaning in the contract yet.
NOT_CARRIED = (
    NotCarried("custody", POSITION, "price", "no_contract_meaning"),
    NotCarried("custody", POSITION, "open_pnl", "no_contract_meaning"),
    NotCarried("custody", LOT, "lot_id", "no_contract_meaning"),
    NotCarried("custody", ACCOUNT, "is_paper", "no_contract_meaning"),
)

DECLARATION = Declaration(
    settings=DECLARED,
    not_carried=NOT_CARRIED,
    storage=Storage(retention_days=RETENTION_DAYS),
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


def seen(snapshot: Snapshot) -> list[tuple[str, str]]:
    """Each name not carried a read of SnapTrade showed, once for each
    account or position that showed it: what the heartbeat counts."""
    found = [name for account in snapshot.accounts for name in seen_in_account(account)]
    for body in snapshot.positions.values():
        results = body.get("results") if isinstance(body, dict) else None
        for position in results or []:
            if isinstance(position, dict):
                found.extend(seen_in_position(position))
    return found
