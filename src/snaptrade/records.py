"""The typed records the pages' routes take and answer, for a person's form
and an agent's tool alike (contract v12; spec/a-deployment-serves-its-mcp).

Each action's record names its inputs as its form names them
(`connection_id`, `external_account_id`), so one path names one field for a
person and one argument for an agent, and a refusal names it by that path.
The reads answer what their page shows, as data: under Manage, how each
connection is and which external accounts it reaches, linked or not, and
no account's data (a plugin admin is account agnostic); under Open and
View, the statements of the accounts the person may read. The history's
records are history.py's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: The longest ID or name a form here takes.
MOST_ID = 200


# ── Actions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Connecting:
    """Nothing to give: SnapTrade's Connection Portal opens for a new
    brokerage connection."""


@dataclass(frozen=True)
class ConnectionAsked:
    """Which connection, by the ID the Connections tab and read_connections
    name it by."""

    connection_id: str = field(
        default="",
        metadata={
            "max_length": MOST_ID,
            "description": "the connection, as read_connections names it",
        },
    )


@dataclass(frozen=True)
class Portal:
    """SnapTrade's Connection Portal, opened: the link a person signs in to
    the brokerage at, once, in their browser; empty in synthetic mode,
    which has none."""

    link: str
    said: str


@dataclass(frozen=True)
class Refreshed:
    """A connection SnapTrade was asked to read again, and what it said."""

    connection_id: str
    said: str


@dataclass(frozen=True)
class ReadAsked:
    """Read SnapTrade now; `back` is the page a browser is answered with."""

    back: str = field(default="", metadata={"max_length": MOST_ID})


@dataclass(frozen=True)
class ReadStarted:
    """A read of SnapTrade asked for: it runs at once, and the next read of
    the statements shows it."""

    said: str


@dataclass(frozen=True)
class LinkAsked:
    """Link one of the external accounts the last read reached to one of the
    deployment's (`link`), create one for it and link it (`create`, for a
    deployment admin alone), or unlink it (`unlink`)."""

    intent: str = field(default="", metadata={"choices": ("link", "create", "unlink")})
    external_account_id: str = field(
        default="",
        metadata={"max_length": MOST_ID, "description": "as read_account_links names it"},
    )
    account_id: str = field(
        default="",
        metadata={"max_length": MOST_ID, "description": "the deployment's account, to link"},
    )
    new_account_name: str = field(default="", metadata={"max_length": MOST_ID})
    new_account_custodian: str = field(default="", metadata={"max_length": MOST_ID})
    new_account_type: str = field(default="", metadata={"max_length": MOST_ID})


@dataclass(frozen=True)
class Linked:
    """An external account's link as it stands after the action."""

    external_account_id: str
    account_id: str
    account_name: str
    said: str


# ── Under Manage: connections and links, no account's data ─────────────────


@dataclass(frozen=True)
class ConnectionRead:
    """One brokerage connection: how it is, and what to do about it."""

    connection_id: str
    name: str
    institution: str
    state: str
    detail: str
    remedy: str
    serving: str = field(metadata={"description": "real_time, delayed, or unknown"})
    disabled_at: str
    accounts: int


@dataclass(frozen=True)
class ConnectionsRead:
    """The SnapTrade connections the last read reached."""

    read_at: str
    mode: str
    error: str
    connections: list[ConnectionRead] = field(default_factory=list)


@dataclass(frozen=True)
class LinkRead:
    """One external account, by its identity alone, and its link."""

    external_account_id: str
    name: str
    institution: str
    connection_id: str
    number: str
    stable: bool
    linked: bool
    account_id: str
    account_name: str


@dataclass(frozen=True)
class LinksRead:
    """The external accounts the last read reached, each linked or not."""

    accounts: list[LinkRead] = field(default_factory=list)


# ── Under Open and View: statements ─────────────────────────────────────────


@dataclass(frozen=True)
class LotRead:
    """One of SnapTrade's tax lots, as the statement carries it."""

    quantity: Decimal
    cost: Decimal | None
    acquired: str


@dataclass(frozen=True)
class RowRead:
    """One row of a statement, as recorded: SnapTrade's average per unit and
    lots as reported."""

    instrument: str
    description: str
    kind: str
    side: str
    quantity: Decimal
    settled: Decimal | None
    currency: str
    average_purchase_price: Decimal | None
    lots: list[LotRead] = field(default_factory=list)


@dataclass(frozen=True)
class StatementRead:
    """One account the person may read: its sync state and its last
    statement, as the last read found them."""

    account: str
    external_account_id: str
    name: str
    state: str
    detail: str
    holdings_as_of: str
    history_as_of: str
    history_from: str
    as_of_date: str
    recorded: str
    rows: list[RowRead] = field(default_factory=list)


@dataclass(frozen=True)
class StatementsRead:
    """The statements of every account linked to one the person may read."""

    read_at: str
    mode: str
    statements: list[StatementRead] = field(default_factory=list)
