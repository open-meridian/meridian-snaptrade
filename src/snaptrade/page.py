"""The plugin's pages, declared and served on the SDK's `meridian.Pages`:
Connections and Account links under Manage, Statements and Raw responses
under Open and View.

The dashboard's home opens the plugin by a button per level the person holds
on it (meridian-design sdk-contract/a-plugin-has-admins, the rulings of
2026-09-30): Manage at `admin`, Open at `write`, View at `read`. Each page
below is declared with the levels it serves, the list `__main__` sends at
registration, and the SDK answers 403 to a session at any other level before
the view runs. So the tab row and the plugin agree by construction.

- **Connections** (`/admin/connections`) and **Account links**
  (`/admin/accounts`), at `admin`: setup only, for whoever is admin on this
  plugin. They cover what only SnapTrade knows and linking the accounts it
  reaches: its users under the key, the brokerage connections, connecting one
  through SnapTrade's Connection Portal, each connection's health with what to
  do, refreshing one SnapTrade serves on a delay (a real-time one has nothing
  to refresh, and on SnapTrade's Real-time plans it refuses to) or
  reconnecting it, and linking each external account to one of the
  deployment's (W6.4). **A plugin admin is account agnostic** (the product
  owner, 2026-09-30): a Manage session holds no account's data, so these
  pages show none of what the plugin read for an account -- no holdings, rows,
  statements or sync state per account -- only external identities, their
  links, and how each connection is. A plugin admin links to any existing
  account; only a deployment admin names a new one (`caller.deployment_admin`,
  read for that alone). `/admin` sends a session to Connections. The
  SnapTrade keys are entered in the dashboard's settings form, never here.
  How many connections and accounts the last read reached, and when it was,
  are no tiles here: they are the plugin's figures on the Summary core draws
  first under Manage (sync.py).
- **Statements** (`/`), at `write` and `read`: per account, its sync state,
  its last statement and its rows, as the last read found them, for each
  account linked to one the person may read (`caller.read`, which under Open
  holds the accounts they may write too), and plainly nothing for somebody
  who may read none. Under Open it has Refresh, which reads SnapTrade now;
  under View it acts on nothing (intent/a-custody-plugin-serves-its-
  statement-receivers, the user-page split).
- **Raw responses** (`/raw`), at `write` and `read`: what SnapTrade answered
  each read, as received, for the same accounts (raw.py): the latest read
  per account, each call formatted, the older reads kept, each downloadable
  as JSON. An account's data, so never at `admin`.

Which of the deployment's accounts an external account is linked to is what
cuts Statements to a reader. The plugin reads its links beside its account
scope (W4.11), each with the linked account's name, and holds the latest
delivery (linking.py). Nothing is stored for it: plugins are ephemeral.

Each page is a view function here and a Jinja2 template under templates/,
rendered by `pages.render` on the kit's base template, `meridian/base.html`:
the kit the dashboard serves on this plugin's own host (spec/plugin-pages-
share-one-kit, requirement 5), the page's heading and the tab row of the
session's level, which the kit drops when the dashboard frames the page. A
template's `status` block is the head's status dot (`om-status
data-om-header`, kit 0.7.0) and its `head_actions` block its header action,
Refresh (`data-om-action`, kit 0.4.0); framed, the dashboard draws both in its
own header. Connections has no header action: its Refresh sits beside + Add in
the Connections card's header (the product owner, 2026-09-30). Every value is
escaped, and each component reads the JSON declared inside it; what else is
inside it is what a browser shows where the kit is not served, so without the
kit a page still works, unstyled, and every action is a plain form.

Every action is a POST, and the SDK refuses one without this plugin's CSRF
token before the view runs: each form carries `{{ csrf_input }}`, and the
account map is handed the token. The views run on the plugin's event loop,
where SnapTrade and the sidecar are asked.
"""

from __future__ import annotations

import asyncio
import http.server
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlencode

import meridian
from meridian.pages import CSRF_FIELD, REQUEST_SECONDS

from . import history, records
from .linking import LINKS_AT_ONCE, Link, Links, LinkView, Offered, refusal
from .normalise import REMEDY, AccountView, ConnectionView, Holding, Serving, SyncState
from .plan_codes import PlanCodeLink
from .raw import (
    CALLS,
    Record,
    activities_call,
    dumps,
    history_taken,
    parse_activity_key,
    parse_record_key,
)
from .settings import label
from .sync import ATTENTION, Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
# The kit's version these pages were built against. The dashboard serves the
# deployment's newest 0.x for it; pinning one keeps the pages as they were built.
KIT = "0.8.0"

# The largest body any request here may carry: the map's several-link form,
# a pair of IDs for each link, a hundred bytes or so a pair, room for some
# thousands of accounts. The SDK answers a larger one 413 before a view runs.
_MOST_LINKS_BODY = 1 << 20
# Every other form here carries a token, an account, and a new account's name,
# custodian and type at most; anything longer is not one of this page's.
_MOST_BODY = 8192

pages = meridian.Pages(
    TITLE,
    templates=Path(__file__).parent / "templates",
    kit=KIT,
    max_body=_MOST_LINKS_BODY,
)

# The pages, in the order each button's tab row shows them.
STATEMENTS = "/"
RAW = "/raw"
CONNECTIONS = "/admin/connections"
ACCOUNTS = "/admin/accounts"
# A kept read of SnapTrade's, as a JSON file to save.
RAW_DOWNLOAD = f"{RAW}/download"
# An account's history, and the lots proposed from it (history.py).
HISTORY = "/history"
LOTS = f"{HISTORY}/lots"
# The actions, each a POST.
READ = "/read"
CONNECT = "/admin/connect"
REFRESH = f"{CONNECTIONS}/refresh"
RECONNECT = f"{CONNECTIONS}/reconnect"
LINK = f"{ACCOUNTS}/link"
INTENTS = ("link", "create", "unlink")
SEVERAL = "link-several"
# Where the templates' forms post, named here once.
pages.environment.globals["paths"] = {
    "read": READ,
    "connect": CONNECT,
    "refresh": REFRESH,
    "reconnect": RECONNECT,
    "link": LINK,
    "raw": RAW,
    "history": HISTORY,
    "lots": LOTS,
}

# The longest name a new account is given here, and the longest custodian or
# type the deployment keeps for one (W6.3).
_MOST_NAME = 200
# How long one call to SnapTrade or the sidecar may take before the page says
# it failed.
_ASKING_SECONDS = 30.0

_Answer = TypeVar("_Answer")


# ── What the pages read ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Held:
    """What the views read and act on, once the plugin has registered: the
    syncer's last read, the plugin's links, and the event that wakes a read."""

    syncer: Syncer
    links: Links
    wake: asyncio.Event


_held: Held | None = None


def hold(syncer: Syncer, links: Links, wake: asyncio.Event) -> None:
    """Give the pages what they read. `pages` is declared at import, for
    registration; what it shows exists only once the plugin has registered."""
    global _held
    _held = Held(syncer, links, wake)


def _now() -> Held:
    if _held is None:
        raise RuntimeError("the pages are served once hold() has given them the syncer")
    return _held


def serve(
    plugin: meridian.Plugin, syncer: Syncer, links: Links, wake: asyncio.Event, port: int
) -> http.server.ThreadingHTTPServer:
    """Start the pages on 127.0.0.1:`port`, in a thread, each view run on the
    running loop, where the plugin lives; the returned server's `shutdown()`
    stops it."""
    hold(syncer, links, wake)
    return pages.serve(plugin, port)


# ── What is shown ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notice:
    """What an action came to, shown at the head of the page it answers with."""

    text: str
    # The kit's notice tones: info, good, warn or bad.
    tone: str = "info"
    # What each part of it came to, where it had several (several links):
    # those in `shown` listed under the text, and every one folded below.
    each: tuple[str, ...] = ()
    shown: tuple[str, ...] = ()


_STATE_LABEL = {
    SyncState.CURRENT: "Current",
    SyncState.STALE: "Stale",
    SyncState.NEEDS_SIGN_IN: "Needs sign-in",
    SyncState.DISABLED: "Disabled",
    SyncState.DELAYED_BY_DESIGN: "Delayed by design",
    SyncState.HOLDINGS_UNAVAILABLE: "Holdings unavailable",
}
# The kit's badge tones: status, never market direction.
_STATE_TONE = {
    SyncState.CURRENT: "good",
    SyncState.STALE: "warn",
    SyncState.NEEDS_SIGN_IN: "bad",
    SyncState.DISABLED: "bad",
    SyncState.DELAYED_BY_DESIGN: "info",
    # Like needing sign-in or disabled, a person's to mend: waiting will not.
    SyncState.HOLDINGS_UNAVAILABLE: "bad",
}
# What a connection says about refreshing it, by how SnapTrade serves it. A
# real-time one is offered no Refresh: SnapTrade reads the brokerage on every
# call, and on its Real-time plans (Personal, Pay as you go) it refuses one.
REAL_TIME = (
    "Real-time: SnapTrade fetches fresh data from the brokerage on every read, "
    "so there is nothing to refresh."
)
DELAYED = (
    "Delayed: SnapTrade serves this connection from its cache. Refresh asks it to "
    "read the brokerage again, and SnapTrade may charge for each refresh."
)
# What SnapTrade's refusal of a refresh (HTTP 403) is shown as.
REFRESH_REFUSED = (
    "SnapTrade doesn't allow a manual refresh for this connection on this plan; "
    "it reads fresh data on every sync."
)
# What a plugin admin who is not a deployment admin is told of a new account.
ONLY_DEPLOYMENT_ADMINS = (
    "Only a deployment admin names a new account: link it to an existing one, or ask "
    "one to create it."
)
_LINK_LABEL = {Link.LINKED: "Linked", Link.UNLINKED: "Not linked"}
_LINK_TONE = {Link.LINKED: "good", Link.UNLINKED: "warn"}
# The text class a statement's outcome takes from its tone.
_INK = {"good": "good-ink", "warn": "warn-ink", "bad": "bad-ink"}


def _moment(moment: datetime | date | None) -> str:
    """A moment as the page shows it, and as a grid sorts it: fixed width, UTC."""
    if moment is None:
        return ""
    if isinstance(moment, datetime):
        return f"{moment:%Y-%m-%d %H:%M} UTC"
    return f"{moment:%Y-%m-%d}"


pages.environment.filters["moment"] = _moment


def _badge(state: SyncState) -> dict[str, str]:
    return {"label": _STATE_LABEL[state], "tone": _STATE_TONE[state]}


def _html(body: str) -> meridian.Response:
    """A page, never kept by a cache: it is the last read as of now."""
    return meridian.Response(body, headers=(("cache-control", "no-store"),))


def _said(text: str, status: int) -> meridian.Response:
    return meridian.Response(
        text, status, "text/plain; charset=utf-8", (("cache-control", "no-store"),)
    )


def _oversized(request: meridian.Request) -> meridian.Response | None:
    """The answer to a form larger than any of this page's but the several-link
    form, before anything is done."""
    if len(request.body) <= _MOST_BODY:
        return None
    return _said("This form is larger than any of this page's.", 413)


def _plural(count: int, one: str) -> str:
    return f"{count} {one}{'' if count == 1 else 's'}"


# ── The status dot ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reading:
    """How the plugin is reading, as the kit's om-status draws it: `state` is
    ok, busy or error; `at` the last read that succeeded, if one is held."""

    state: str
    label: str
    detail: str = ""
    at: datetime | None = None


def reading(status: Status, setup: bool = True) -> Reading:
    """Each of the syncer's states as a status dot (the product owner,
    2026-09-30: green for a read that succeeded, amber while reading, red for
    an error), in this order:

    - a read under way (`reading`): busy, saying how the read before it went;
    - a read that failed (`error`, which is safe to show): error, its message;
    - waiting for settings (`waiting`, with settings missing): error, naming
      them on a page at `admin`, where they are given (`setup`), and not on
      Statements, whose readers do not give them;
    - a read that succeeded (`read_at`): ok, when;
    - nothing read yet, before the settings first arrive: busy.
    """
    if status.reading:
        said = (
            "Synthetic mode: reading built-in responses, not SnapTrade"
            if status.mode == "synthetic"
            else "Reading SnapTrade"
        )
        before = f"The last read failed: {status.error}" if status.error else ""
        return Reading("busy", said, before, status.read_at)
    if status.error:
        return Reading("error", "The last read failed", status.error)
    if status.mode == "waiting" and status.missing:
        if not setup:
            return Reading("error", "Not reading SnapTrade", "SnapTrade is not being read yet.")
        wanted = ", ".join(label(name) for name in status.missing)
        return Reading("error", "Not reading SnapTrade", f"Waiting for settings: {wanted}.")
    if status.read_at is not None:
        said = (
            "Synthetic mode: built-in responses, not SnapTrade"
            if status.mode == "synthetic"
            else "SnapTrade read"
        )
        return Reading("ok", said, at=status.read_at)
    return Reading("busy", "Starting", "SnapTrade has not been read yet.")


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _dot(status: Status, setup: bool = True) -> dict[str, str]:
    """The head's om-status: its attributes, and what it shows until the kit
    draws it (or where the kit is not served) inside, as plain text."""
    said = reading(status, setup)
    shown = [said.label]
    if said.detail:
        shown.append(said.detail)
    if said.at is not None:
        shown.append(f"Last read {_moment(said.at)}")
    return {
        "state": said.state,
        "label": said.label,
        "detail": said.detail,
        "at": said.at.isoformat() if said.at is not None else "",
        "text": " ".join(_sentence(part) for part in shown),
    }


def _waiting_for(status: Status) -> str:
    """The settings a read waits for, by the form's labels, never their values."""
    if status.mode != "waiting":
        return ""
    return ", ".join(label(name) for name in status.missing)


def _last_read(status: Status) -> dict[str, str]:
    """A tile's figure is short: the time, with the day under it."""
    if status.read_at is None:
        return {"label": "Last read", "value": "Not yet"}
    return {
        "label": "Last read",
        "value": f"{status.read_at:%H:%M} UTC",
        "delta": f"{status.read_at:%Y-%m-%d}",
    }


# ── Connections, at admin ───────────────────────────────────────────────────


def _connection(connection: ConnectionView) -> dict[str, Any]:
    """One brokerage connection as its row shows it: its health and what to do,
    how SnapTrade serves it, and whether Refresh means anything for it."""
    access = {"read": "read-only", "trade": "trading"}.get(connection.access, connection.access)
    meta = [connection.name, access, _plural(len(connection.accounts), "account")]
    remedy = "" if connection.state is SyncState.CURRENT else REMEDY[connection.state]
    return {
        "connection_id": connection.connection_id,
        "institution": connection.institution or "Unknown brokerage",
        "badge": _badge(connection.state),
        "meta": " · ".join(filter(None, meta)),
        "said": " ".join(filter(None, (connection.detail, remedy))),
        # Refresh is offered unless SnapTrade says it serves the connection in
        # real time; where it does not say, a refusal is shown plainly
        # (REFRESH_REFUSED).
        "refreshing": {Serving.REAL_TIME: REAL_TIME, Serving.DELAYED: DELAYED}.get(
            connection.serving, ""
        ),
        "refresh": connection.serving is not Serving.REAL_TIME,
        # Reconnecting is what a connection needing sign-in asks for, so it
        # leads there; it cannot mend one whose brokerage SnapTrade turned off.
        "reconnect": "primary" if connection.state is SyncState.NEEDS_SIGN_IN else "",
    }


def _connections(notice: Notice | None = None, portal: str | None = None) -> meridian.Response:
    status = _now().syncer.status
    return _html(
        pages.render(
            "connections.html",
            dot=_dot(status),
            back=CONNECTIONS,
            notice=notice,
            portal=portal,
            waiting=_waiting_for(status),
            error=status.error,
            connections=[_connection(c) for c in status.connections],
            user_id=status.user_id,
            users=status.users,
        )
    )


def _connections_read(status: Status) -> records.ConnectionsRead:
    """The Connections tab as data: each connection the last read reached,
    how it is and what to do, and how many accounts it reaches; no account's
    data."""
    return records.ConnectionsRead(
        read_at=status.read_at.isoformat() if status.read_at else "",
        mode=status.mode,
        error=status.error,
        connections=[
            records.ConnectionRead(
                connection_id=c.connection_id,
                name=c.name,
                institution=c.institution,
                state=_STATE_LABEL[c.state],
                detail=c.detail,
                remedy="" if c.state is SyncState.CURRENT else REMEDY[c.state],
                serving=c.serving.value,
                disabled_at=c.disabled_at.isoformat() if c.disabled_at else "",
                accounts=len(c.accounts),
            )
            for c in status.connections
        ],
    )


@pages.page(
    CONNECTIONS,
    "Connections",
    levels="admin",
    answers=records.ConnectionsRead,
    name="read_connections",
    description=(
        "The SnapTrade connections the last read reached: each one's state, what to do "
        "about it, whether SnapTrade serves it in real time or on a delay (only a delayed "
        "one can be refreshed), and how many accounts it reaches. No account's data."
    ),
)
async def connections(request: meridian.Request) -> meridian.Response:
    if request.tool_name:
        return pages.answer("connections.html", _connections_read(_now().syncer.status))
    return _connections()


@pages.route(
    "/admin",
    levels="admin",
    tool=False,
    why="a browser's way in to Connections; an agent reads them with read_connections",
)
async def admin(request: meridian.Request) -> meridian.Response:
    """The first page under Manage. The frame's theme rides the query on first
    load; it goes along."""
    query = urlencode(request.query)
    where = CONNECTIONS + (f"?{query}" if query else "")
    return meridian.Response("", 303, headers=(("location", where),))


async def _asking(work: Awaitable[_Answer]) -> tuple[_Answer | None, Notice | None]:
    """What SnapTrade answered, or what its failure is shown as: a VenueError
    says what was asked, its type and HTTP status, never its text; anything
    else is named by its type alone, since its text is not known to be safe."""
    try:
        return await asyncio.wait_for(work, _ASKING_SECONDS), None
    except VenueError as failed:
        return None, Notice(str(failed), "bad")
    except Exception as failed:
        return None, Notice(f"That failed: {type(failed).__name__}", "bad")


_NO_VENUE = Notice("Nothing to ask SnapTrade until its settings are given.")
# What a tool is told of the Connection Portal it opened, or of synthetic
# mode's, which has none.
PORTAL_READY = (
    "SnapTrade's Connection Portal is ready: the person signs in to the brokerage at the "
    "link, once, in their browser; the connection appears on the next read."
)
NO_PORTAL = (
    "Synthetic mode has no Connection Portal; it opens once SnapTrade's settings are given."
)


def _portal(
    request: meridian.Request, portal: str | None, failed: Notice | None
) -> meridian.Response:
    """The Connection Portal as an action answers it: for a tool, its link
    or SnapTrade's failure; for a browser, Connections with it."""
    if not request.tool_name:
        return _connections(failed, portal)
    if failed is not None:
        pages.refuse(failed.text, reason="unavailable")
    return pages.answer(
        "connections.html", records.Portal(portal or "", PORTAL_READY if portal else NO_PORTAL)
    )


def _no_venue(request: meridian.Request) -> meridian.Response:
    if request.tool_name:
        pages.refuse(_NO_VENUE.text, reason="unavailable")
    return _connections(_NO_VENUE)


@pages.route(
    CONNECT,
    levels="admin",
    methods=["POST"],
    params=records.Connecting,
    answers=records.Portal,
    name="open_connection_portal",
)
async def connect(request: meridian.Request) -> meridian.Response:
    """Open SnapTrade's Connection Portal, to connect a brokerage: a link the
    person signs in to the brokerage at, once, in their browser."""
    if (refused := _oversized(request)) is not None:
        return refused
    venue = _now().syncer.venue
    if venue is None:
        return _no_venue(request)
    portal, failed = await _asking(venue.connection_portal())
    return _portal(request, portal, failed)


def _connection_asked(request: meridian.Request) -> str | meridian.Response:
    """The connection a form or a tool names, among those the last read
    reached; or the answer that says it is not one of them."""
    asked = request.params
    connection_id = asked.connection_id.strip() if asked is not None else ""
    reached = {c.connection_id for c in _now().syncer.status.connections}
    if connection_id in reached:
        return connection_id
    if request.tool_name:
        pages.refuse(
            "No such connection.",
            ("connection_id", "not a connection the last read reached"),
            reason="not_found",
        )
    return _said("No such connection.", 404)


def _refreshed(
    request: meridian.Request, asked: str, notice: Notice, outcome: str = "made"
) -> meridian.Response:
    if request.tool_name:
        return pages.answer(
            "connections.html", records.Refreshed(asked, notice.text), outcome=outcome
        )
    return _connections(notice)


@pages.route(
    REFRESH,
    levels="admin",
    methods=["POST"],
    params=records.ConnectionAsked,
    answers=records.Refreshed,
    name="refresh_connection",
)
async def refresh(request: meridian.Request) -> meridian.Response:
    """Ask SnapTrade to read a delayed connection's brokerage again (it may
    charge for each); a real-time connection has nothing to refresh, and
    SnapTrade's refusal is said plainly."""
    if (refused := _oversized(request)) is not None:
        return refused
    held = _now()
    venue = held.syncer.venue
    if venue is None:
        return _no_venue(request)
    asked = _connection_asked(request)
    if isinstance(asked, meridian.Response):
        return asked
    serving = {c.connection_id: c.serving for c in held.syncer.status.connections}
    if serving[asked] is Serving.REAL_TIME:
        # Offered no Refresh; a form from an older page is answered without
        # asking SnapTrade.
        return _refreshed(request, asked, Notice(REAL_TIME), "unchanged")
    try:
        said = await asyncio.wait_for(venue.refresh(asked), _ASKING_SECONDS)
    except VenueError as failed:
        # SnapTrade's refusal (Real-time plans refuse a refresh), said
        # plainly; any other failure is shown as every other is.
        if failed.status != 403:
            if request.tool_name:
                pages.refuse(str(failed), reason="unavailable")
            return _connections(Notice(str(failed), "bad"))
        if request.tool_name:
            pages.refuse(
                REFRESH_REFUSED, ("connection_id", REFRESH_REFUSED), reason="not_refreshable"
            )
        return _connections(Notice(REFRESH_REFUSED))
    except Exception as failed:
        said = f"That failed: {type(failed).__name__}"
        if request.tool_name:
            pages.refuse(said, reason="unavailable")
        return _connections(Notice(said, "bad"))
    held.wake.set()
    return _refreshed(request, asked, Notice(said))


@pages.route(
    RECONNECT,
    levels="admin",
    methods=["POST"],
    params=records.ConnectionAsked,
    answers=records.Portal,
    name="reconnect_connection",
)
async def reconnect(request: meridian.Request) -> meridian.Response:
    """Open SnapTrade's Connection Portal to sign in to a connection's
    brokerage again: a link the person signs in at, once, in their browser."""
    if (refused := _oversized(request)) is not None:
        return refused
    venue = _now().syncer.venue
    if venue is None:
        return _no_venue(request)
    asked = _connection_asked(request)
    if isinstance(asked, meridian.Response):
        return asked
    portal, failed = await _asking(venue.connection_portal(reconnect=asked))
    return _portal(request, portal, failed)


# ── Account links, at admin ─────────────────────────────────────────────────
#
# The kit's om-account-map (0.5.0): a dense table of every account the read
# reached with its link, searched, filtered, grouped by connection and paged,
# one row's choices opened at a time, with suggestions where a name or a
# number matches one of the deployment's accounts, from the JSON declared
# inside it. Its forms all post to LINK, saying what they mean in `intent`
# (`link`, `create`, `unlink`, or `link-several` with a pair of IDs for each
# link), with the page's token.
#
# Under Manage it is given each external account's identity and link, and
# nothing the plugin read for it: no sync state, statement or rows (kit
# 0.6.0's status and values are left out, so the map draws no Status column).
# For a plugin admin who is no deployment admin, the map is `no-new-account`
# (kit 0.7.1): it offers only existing accounts, as the plain forms do, and
# LINK refuses `create` from them whatever is sent.
#
# Inside it too, for a browser without the kit, the same as plain forms, in a
# size that holds for thousands of accounts: a list row per account (its link,
# and Unlink on a linked one), then one form to link any account to any open
# account and, for a deployment admin, one to create an account for any.

Shown = tuple[ConnectionView, AccountView]


def _where(connection: ConnectionView, view: AccountView) -> str:
    return " · ".join(
        (
            connection.institution or "Unknown brokerage",
            view.account.account_type or "type not given",
        )
    )


def _unstable(view: AccountView) -> str:
    if view.account.stable:
        return ""
    return (
        "SnapTrade gives no stable ID for this account: after a reconnect it appears "
        "as a new account, to be linked again."
    )


def _connection_label(connection: ConnectionView) -> str:
    """A connection as the map groups by it: its brokerage, and its own name."""
    brokerage = connection.institution or "Unknown brokerage"
    return f"{brokerage} · {connection.name}" if connection.name else brokerage


def _map_data(
    shown: Sequence[Shown],
    links: Mapping[str, LinkView],
    offered: Offered,
    taken: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """What om-account-map takes: the external accounts' identities (with the
    number the map matches on, and the connection it groups by), the
    deployment's accounts (null where they could not be read), and the links
    standing. An account in `taken`, already linked to one of this plugin's
    external accounts, is not offered for another: the map offers only an
    account marked open, so it is given as not open, and still named."""
    accounts = (
        None
        if offered.refused
        else [
            {
                "account_id": account.account_id,
                "name": account.name,
                "custodian": account.custodian,
                "account_type": account.account_type,
                "open": account.open and account.account_id not in taken,
            }
            for account in offered.accounts
        ]
    )
    return {
        "external_accounts": [
            {
                "external_account_id": view.account.external_account_id,
                "name": view.account.name,
                "detail": _where(connection, view),
                "custodian": connection.institution,
                # The kind, converted, pre-fills a new account's type, never
                # SnapTrade's own type (contract v11, Q13).
                "account_type": view.account.kind,
                "note": _unstable(view),
                "number": view.account.number,
                "connection": _connection_label(connection),
                "connection_id": connection.connection_id,
            }
            for connection, view in shown
        ],
        "accounts": accounts,
        "links": [
            {
                "external_account_id": view.account.external_account_id,
                "account_id": link.account_id,
                "account_name": link.account_name,
            }
            for _, view in shown
            if (link := links[view.account.external_account_id]).state is Link.LINKED
        ],
    }


def _mapping_row(
    connection: ConnectionView, view: AccountView, link: LinkView
) -> dict[str, Any]:
    """One account as its plain row shows it without the kit: who it is, and
    its link."""
    account = view.account
    return {
        "external_account_id": account.external_account_id,
        "name": account.name,
        "link": {"label": _LINK_LABEL[link.state], "tone": _LINK_TONE[link.state]},
        "linked": link.state is Link.LINKED,
        "account_id": link.account_id,
        "account_name": link.account_name or link.account_id,
        "where": _where(connection, view),
        "number": account.number,
        "note": _unstable(view),
    }


def _links_of(status: Status) -> dict[str, LinkView]:
    links = _now().links
    return {
        view.account.external_account_id: links.of(view.account.external_account_id)
        for view in status.accounts
    }


async def _offered_to(caller: meridian.Caller) -> Offered:
    """The deployment's accounts, read for the admin viewing the page:
    identities only, which a Manage session may read (W4.9)."""
    try:
        return await asyncio.wait_for(_now().links.offered(caller.header), _ASKING_SECONDS)
    except Exception as failed:
        # Named by its type only: its text is not known to be safe to show.
        return Offered(refused=f"that failed: {type(failed).__name__}")


async def _accounts(
    request: meridian.Request, notice: Notice | None = None
) -> meridian.Response:
    """The Account links tab: each account the connections reach, linked to one
    of the deployment's here, in the one account map; and, one line each, how
    many plan-code links the plugin's settings hold and how long each reported
    activity's record is kept, both set in the dashboard's Settings form."""
    status = _now().syncer.status
    links = _links_of(status)
    # One external account per account of the deployment's (v7): one this
    # plugin already links is offered to none of its others.
    taken = _now().links.taken()
    offered = await _offered_to(request.caller)
    shown = [(c, view) for c in status.connections for view in c.accounts]
    counted = [links[view.account.external_account_id].state for view in status.accounts]
    summary = ", ".join(
        f"{counted.count(state)} {_LINK_LABEL[state].lower()}"
        for state in Link
        if counted.count(state)
    )
    return _html(
        pages.render(
            "accounts.html",
            dot=_dot(status),
            back=ACCOUNTS,
            notice=notice,
            waiting=_waiting_for(status),
            error=status.error,
            refused=offered.refused,
            summary=summary.capitalize(),
            # Only a deployment admin names a new account (W6.4).
            creates=request.caller.deployment_admin,
            map=_map_data(shown, links, offered, taken),
            rows=[
                _mapping_row(c, view, links[view.account.external_account_id])
                for c, view in shown
            ],
            externals=[
                {
                    "external_account_id": view.account.external_account_id,
                    "label": f"{view.account.name} ({_where(c, view)})",
                }
                for c, view in shown
            ],
            choices=[
                {"account_id": a.account_id, "label": a.label()}
                for a in offered.accounts
                if a.open and a.account_id not in taken
            ],
            # Open accounts there are, every one linked already.
            offered_any=any(a.open for a in offered.accounts),
            most_name=_MOST_NAME,
            token_name=CSRF_FIELD,
            settings_said=_settings_said(),
        )
    )


def _plan_code_reads(links: Sequence[PlanCodeLink]) -> list[records.PlanCodeRead]:
    return [
        records.PlanCodeRead(
            link.external_account_id,
            link.code,
            link.instrument_id,
            link.changed_by,
            link.changed_at,
        )
        for link in links
    ]


def _settings_said() -> dict[str, Any]:
    """What the plugin's settings hold that this tab speaks of, read only:
    the plan-code links, the positions counted as cash, and how long each
    activity's record is kept -- as set, and as kept, never shorter than the
    history SnapTrade reported."""
    held = _now()
    return {
        "plan_codes": len(held.syncer.config.plan_codes),
        "counted_as_cash": len(held.syncer.config.counted_as_cash),
        **_retention_read(),
    }


def _links_read(status: Status) -> records.LinksRead:
    """The Account links tab as data: each external account the last read
    reached, by its identity alone, and its link; no account's data."""
    links = _links_of(status)
    return records.LinksRead(
        [
            records.LinkRead(
                external_account_id=view.account.external_account_id,
                name=view.account.name,
                institution=connection.institution,
                connection_id=connection.connection_id,
                number=view.account.number,
                stable=view.account.stable,
                linked=(link := links[view.account.external_account_id]).state is Link.LINKED,
                account_id=link.account_id,
                account_name=link.account_name,
            )
            for connection in status.connections
            for view in connection.accounts
        ],
        plan_codes=_plan_code_reads(_now().syncer.config.plan_codes),
        **_retention_read(),
    )


def _retention_read() -> dict[str, int]:
    held = _now()
    raw = held.syncer.raw
    days = held.syncer.config.activity_retention.days
    return {
        "activity_retention_days": days,
        "kept_for_days": raw.kept_for().days if raw is not None else days,
        "history_reach_days": raw.history_reach_days() if raw is not None else 0,
    }


@pages.page(
    ACCOUNTS,
    "Account links",
    levels="admin",
    answers=records.LinksRead,
    name="read_account_links",
    description=(
        "The external accounts SnapTrade's connections reach, by identity alone -- name, "
        "institution, the brokerage's number where SnapTrade gives it, whether its ID is "
        "stable -- each linked to one of the deployment's accounts or not; the plan-code "
        "links people made, each a plan's own fund code on one account linked to the "
        "instrument it is; and how long each reported activity's record is kept. No "
        "account's data."
    ),
)
async def accounts(request: meridian.Request) -> meridian.Response:
    if request.tool_name:
        return pages.answer("accounts.html", _links_read(_now().syncer.status))
    return await _accounts(request)


def _field(form: Mapping[str, list[str]], name: str) -> str:
    """A form's one value for `name`, or "" where it has none or several."""
    values = form.get(name, [])
    return values[0].strip() if len(values) == 1 else ""


@pages.route(LINK, levels="admin", methods=["POST"])
async def link(request: meridian.Request) -> meridian.Response:
    """Link, create and link, or unlink one of the accounts the last read
    reached, as the form's `intent` says, for the admin who sent it (W6.4). A
    link to another account replaces the one standing."""
    # Read whole, since the several-link form repeats its fields.
    form = parse_qs(request.body.decode("utf-8", errors="replace"))
    intent = _field(form, "intent")
    if intent == SEVERAL:
        return await _link_several(request, form)
    if (refused := _oversized(request)) is not None:
        return refused
    held, caller = _now(), request.caller
    external_id = _field(form, "external_account_id")
    views = {view.account.external_account_id: view for view in held.syncer.status.accounts}
    if intent not in INTENTS:
        return _said("This form does not say what to do.", 400)
    if external_id not in views:
        return _said("No such account.", 404)
    named = views[external_id].account.name or external_id
    account_id = _field(form, "account_id") if intent == "link" else ""
    name = _field(form, "new_account_name")[:_MOST_NAME] if intent == "create" else ""
    # Where it is held and what it is, as the admin left the venue's words;
    # longer than the deployment keeps is refused by it, by name.
    custodian = _field(form, "new_account_custodian") if intent == "create" else ""
    account_type = _field(form, "new_account_type") if intent == "create" else ""
    if intent == "link" and not account_id:
        notice = Notice("Choose the account to link it to.", "warn")
    elif intent == "create" and not caller.deployment_admin:
        # The sidecar refuses it too; said here, and nothing is sent.
        notice = Notice(ONLY_DEPLOYMENT_ADMINS, "warn")
    elif intent == "create" and not name:
        notice = Notice("Name the new account.", "warn")
    else:
        try:
            await asyncio.wait_for(
                held.links.link(
                    caller.header, external_id, account_id, name, custodian, account_type
                ),
                _ASKING_SECONDS,
            )
        except meridian.MeridianError as refused_link:
            notice = Notice(f"The sidecar refused this: {refusal(refused_link)}", "bad")
        except Exception as failed:
            notice = Notice(f"That failed: {type(failed).__name__}", "bad")
        else:
            # Read again, so its rows follow the link.
            held.wake.set()
            now = held.links.of(external_id)
            to = now.account_name if now.account_id == account_id else ""
            notice = Notice(
                f"Unlinked {named}."
                if intent == "unlink"
                else f"Created {name} and linked {named} to it."
                if intent == "create"
                else f"Linked {named} to {to or account_id}.",
                "good",
            )
    return await _accounts(request, notice)


@pages.tool(
    replaces=LINK,
    params=records.LinkAsked,
    answers=records.Linked,
    name="link_account",
    description=(
        "Link one of the external accounts read_account_links lists to one of the "
        "deployment's accounts (intent link, with account_id; a link to another account "
        "replaces the one standing), create a new account for it and link it (intent "
        "create, with new_account_name: a deployment admin alone), or unlink it (intent "
        "unlink). Each of the deployment's accounts takes one external account."
    ),
)
async def link_tool(request: meridian.Request) -> meridian.Response:
    """The Account links form's one link, as an agent's call."""
    asked: records.LinkAsked = request.params
    held, caller = _now(), request.caller
    views = {view.account.external_account_id: view for view in held.syncer.status.accounts}
    external_id = asked.external_account_id.strip()
    if external_id not in views:
        pages.refuse(
            "No such account.",
            ("external_account_id", "not an account the last read reached"),
            reason="not_found",
        )
    if not asked.intent:
        pages.refuse("Say what to do.", ("intent", "link, create or unlink"))
    named = views[external_id].account.name or external_id
    account_id = asked.account_id.strip() if asked.intent == "link" else ""
    name = asked.new_account_name.strip() if asked.intent == "create" else ""
    if asked.intent == "link" and not account_id:
        pages.refuse("Choose the account to link it to.", ("account_id", "name the account"))
    if asked.intent == "create" and not caller.deployment_admin:
        pages.refuse(
            ONLY_DEPLOYMENT_ADMINS,
            ("intent", ONLY_DEPLOYMENT_ADMINS),
            reason="permission_denied",
        )
    if asked.intent == "create" and not name:
        pages.refuse("Name the new account.", ("new_account_name", "name the new account"))
    try:
        linked_to = await asyncio.wait_for(
            held.links.link(
                caller.header,
                external_id,
                account_id,
                name,
                asked.new_account_custodian.strip() if asked.intent == "create" else "",
                asked.new_account_type.strip() if asked.intent == "create" else "",
            ),
            _ASKING_SECONDS,
        )
    except meridian.MeridianError as refused_link:
        pages.refuse(f"The sidecar refused this: {refusal(refused_link)}")
    except Exception as failed:
        pages.refuse(f"That failed: {type(failed).__name__}", reason="unavailable")
    # Read again, so its rows follow the link.
    held.wake.set()
    now = held.links.of(external_id)
    to = now.account_name if now.account_id == linked_to else ""
    said = (
        f"Unlinked {named}."
        if asked.intent == "unlink"
        else f"Created {name} and linked {named} to it."
        if asked.intent == "create"
        else f"Linked {named} to {to or linked_to}."
    )
    return pages.answer("accounts.html", records.Linked(external_id, linked_to, to, said))


# Links still being sent after the page answering them stopped waiting: held
# here so the loop finishes them.
_sending: set[asyncio.Future[list[str]]] = set()


async def _link_several(
    request: meridian.Request, form: Mapping[str, list[str]]
) -> meridian.Response:
    """Link several accounts, each to the account paired with it: the map's
    `link-several` form, one `external_account_id` and one `account_id` for
    each link, in order, under the one token. Each is its own link, and the
    page answering says how each went. A form whose IDs do not pair up is
    refused whole."""
    held = _now()
    externals = [value.strip() for value in form.get("external_account_id", [])]
    accounts = [value.strip() for value in form.get("account_id", [])]
    if (
        not externals
        or len(externals) != len(accounts)
        or len(set(externals)) != len(externals)
        or not all(externals)
        or not all(accounts)
    ):
        return _said(
            "This form's accounts do not pair up: one account to link, and one "
            "to link it to, for each link.",
            400,
        )
    views = {view.account.external_account_id: view for view in held.syncer.status.accounts}
    reached = [(x, a) for x, a in zip(externals, accounts, strict=True) if x in views]
    # Thirty seconds, and a second more for each round of links sent at once,
    # within the time the page's server gives a view.
    waited = min(30 + len(reached) / LINKS_AT_ONCE, REQUEST_SECONDS - 5)
    said: list[str] = []
    if reached:
        sending = asyncio.ensure_future(held.links.link_several(request.caller.header, reached))
        _sending.add(sending)
        sending.add_done_callback(_sending.discard)
        try:
            said = await asyncio.wait_for(asyncio.shield(sending), waited)
        except TimeoutError:
            # Still going: which went through is for the account scope to say.
            held.wake.set()
            notice = Notice(
                f"Sent {len(reached)} links; not every one was answered within "
                f"{round(waited)} seconds. Reload in a moment to see which are linked.",
                "warn",
            )
            return await _accounts(request, notice)
        except Exception as failed:
            said = [f"that failed: {type(failed).__name__}"] * len(reached)
    refused = dict(zip((x for x, _ in reached), said, strict=True))
    each: list[str] = []
    failures: list[str] = []
    linked = 0
    for external_id, account_id in zip(externals, accounts, strict=True):
        named = views[external_id].account.name if external_id in views else external_id
        if external_id not in views:
            failures.append(
                f"{named}: not linked, as the last read of SnapTrade did not reach it."
            )
            each.append(failures[-1])
        elif refused[external_id]:
            failures.append(
                f"{named}: not linked. The sidecar refused it: {refused[external_id]}"
            )
            each.append(failures[-1])
        else:
            linked += 1
            now = held.links.of(external_id)
            to = now.account_name if now.account_id == account_id else ""
            each.append(f"Linked {named} to {to or account_id}.")
    if linked:
        # Read again, so their rows follow the links.
        held.wake.set()
    asked = len(externals)
    plural = "account" if asked == 1 else "accounts"
    if not failures:
        notice = Notice(f"Linked {asked} {plural}.", "good", tuple(each))
    elif linked:
        notice = Notice(
            f"Linked {linked} of {asked} {plural}; {len(failures)} not linked:",
            "warn",
            tuple(each),
            tuple(failures),
        )
    else:
        notice = Notice(
            f"Linked none of the {asked} {plural}:", "bad", tuple(each), tuple(failures)
        )
    return await _accounts(request, notice)


# ── Statements, at write and read ───────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    """One column of a statement's grid, as the kit's om-grid takes it in its
    declared JSON and as the plain table shown without the kit draws it.
    `hint` names a field shown under the value; `blank` is what an empty
    value says, faint (the kit's rich cells: plain JSON, no script)."""

    key: str
    label: str
    type: str = "text"
    group: bool = False
    hint: str = ""
    blank: str = ""

    def declared(self) -> dict[str, object]:
        """As om-grid's columns take it, plain JSON: only what is set."""
        column: dict[str, object] = {"key": self.key, "label": self.label, "type": self.type}
        if self.group:
            column["group"] = True
        if self.hint:
            column["hint"] = self.hint
        if self.blank:
            column["blank"] = self.blank
        return column


# A statement's rows, one grid per account.
_ROW_COLUMNS = (
    Column("instrument", "Instrument", type="code", hint="identifiers"),
    Column("quantity", "Quantity", type="decimal", group=True),
    Column("currency", "Currency"),
    # SnapTrade's average per unit, as reported (`average_cost` on the street).
    Column("average", "Average price", type="decimal", blank="not reported"),
    Column("side", "Side"),
    Column("description", "Description", hint="notes"),
    Column("kind", "Kind", blank="not given"),
)


def visible(
    status: Status, links: Mapping[str, LinkView], caller: meridian.Caller
) -> list[Shown]:
    """The accounts Statements shows this caller: each one linked to an
    account they may read, and no other. Nobody sees an account by being an
    admin of anything (the product owner, 2026-09-30)."""
    return [
        (connection, view)
        for connection in status.connections
        for view in connection.accounts
        if _readable(links.get(view.account.external_account_id), caller)
    ]


def _readable(link: LinkView | None, caller: meridian.Caller) -> bool:
    return (
        link is not None
        and link.state is Link.LINKED
        and bool(link.account_id)
        and caller.may_read(link.account_id)
    )


def _recorded(view: AccountView, status: Status) -> tuple[str, str, str]:
    """What the last statement came to: its text, its tone, and a note."""
    outcome = status.outcomes.get(view.account.external_account_id)
    if view.statement is None:
        return "Nothing recorded", "", view.withheld
    if outcome is None:
        return "Not yet recorded", "", ""
    if outcome.stopped:
        return (
            f"Stopped at {outcome.recorded} of {outcome.rows} rows",
            "bad",
            outcome.stopped,
        )
    if outcome.already_recorded:
        return "Already recorded", "", ""
    extra = []
    if outcome.minted:
        extra.append(f"{outcome.minted} new to the deployment's instrument records")
    if outcome.ambiguous:
        extra.append(f"{outcome.ambiguous} ambiguous")
    return f"{outcome.recorded} rows", "", ", ".join(extra)


def _holding_row(view: AccountView, holding: Holding) -> dict[str, str]:
    # Shown by SnapTrade's symbol where there is one, a ticker or an option's
    # OCC symbol, and by its first identifier otherwise.
    shown = next(
        (i for i in holding.identifiers if i.scheme == "symbol"), holding.identifiers[0]
    )
    others = [f"{i.scheme.upper()} {i.value}" for i in holding.identifiers if i is not shown]
    if holding.exchange_mic:
        others.append(f"on {holding.exchange_mic}")
    notes = []
    if holding.currency_assumed:
        notes.append(
            f"SnapTrade stated no currency; {holding.currency} is derived: the account's "
            "only cash currency, or US dollars"
        )
    statement = view.statement
    netted = (
        next((n for n in statement.netted if n.currency == holding.currency), None)
        if statement is not None
        else None
    )
    if holding.kind == "cash" and netted is not None:
        # The custodian's own figure, beside what was sent (contract v11):
        # SnapTrade's cash counts the fund, and the street gets each asset once.
        funds = ", ".join(
            f"{symbol}, {format(units, 'f')} at {format(price, 'f')}"
            for symbol, units, price in netted.funds
        )
        notes.append(
            f"SnapTrade reports {format(netted.gross, 'f')} of {netted.currency} cash, "
            f"counting {funds}; sent net of it, {format(netted.net, 'f')}: derived, cash net "
            "of the money market funds the brokerage counts in cash"
        )
    elif holding.cash_equivalent:
        notes.append(
            f"a money market fund SnapTrade counts in {holding.currency} cash too: sent as a "
            "fund, and the cash net of it"
        )
    for deposit in (
        statement.as_cash if statement is not None and holding.kind == "cash" else ()
    ):
        if deposit.currency != holding.currency:
            continue
        held = (
            f'{deposit.symbol}, "{deposit.description}", {format(deposit.units, "f")} at '
            f"{format(deposit.price, 'f')}"
        )
        if deposit.by == "flag":
            notes.append(
                f"{held}: a deposit SnapTrade marks a cash equivalent and counts in this cash; "
                "sent as this cash, not as a holding: derived, the cash SnapTrade reports, "
                "counting it"
            )
        else:
            notes.append(
                f"{held}: listed as cash in the plugin's settings by {deposit.person}; "
                f"{format(deposit.amount, 'f')} added to this cash, not sent as a holding"
            )
    if holding.pending:
        pending = ", ".join(
            f"{format(p.quantity, 'f')} on {p.value_date}" for p in holding.pending
        )
        notes.append(f"pending {pending}, as SnapTrade's activities date the trades")
    if holding.settle_date_quantity is not None and statement is not None:
        how = (
            "derived from SnapTrade's activities"
            if statement.activities_read
            else "taken as the quantity: SnapTrade's activities could not be read"
        )
        notes.append(f"settled {format(holding.settle_date_quantity, 'f')}, {how}")
    return {
        # One row per instrument and side in each account (normalise._merged).
        "key": " ".join(
            [view.account.external_account_id, holding.side.value]
            + [f"{i.scheme}:{i.source}:{i.value}" for i in holding.identifiers]
        ),
        "instrument": shown.value,
        "identifiers": " · ".join(others),
        "description": holding.description,
        "notes": "; ".join(notes),
        # Empty where SnapTrade gave none: the column says so (`blank`).
        "kind": holding.kind,
        "side": holding.side.value.capitalize(),
        # Exact, as read: a decimal string, never a float.
        "quantity": format(holding.quantity, "f"),
        "currency": holding.currency,
        "average": format(Decimal(holding.average_cost.amount), "f")
        if holding.average_cost
        else "",
    }


def _statement(
    index: int, connection: ConnectionView, view: AccountView, status: Status
) -> dict[str, Any]:
    """One account on Statements: its sync state, its last statement and its rows."""
    account, fresh, statement = view.account, view.freshness, view.statement
    recorded, tone, note = _recorded(view, status)
    said = [fresh.detail] if fresh.detail else []
    if note and statement is not None:
        said.append(f"{note[0].upper()}{note[1:]}.")
    rows = [_holding_row(view, holding) for holding in statement.holdings] if statement else []
    return {
        "external_account_id": account.external_account_id,
        "name": account.name,
        "badge": _badge(fresh.state),
        "where": _where(connection, view),
        "holdings_as_of": fresh.holdings_as_of,
        "history_as_of": fresh.history_as_of,
        "as_of_date": statement.as_of_date if statement is not None else "",
        "recorded": recorded,
        "ink": _INK.get(tone, ""),
        "said": said,
        "grid": {
            "id": f"rows-{index}",
            "caption": f"{account.name}: rows",
            "empty": (view.withheld or "Nothing was read.")
            if statement is None
            else "No rows.",
            "rows": rows,
            "data": {"columns": [c.declared() for c in _ROW_COLUMNS], "rows": rows},
        },
    }


def _statement_tiles(status: Status, shown: Sequence[Shown]) -> list[dict[str, str]]:
    attention = sum(1 for _, view in shown if view.freshness.state in ATTENTION)
    statements = [view.statement for _, view in shown if view.statement is not None]
    rows = sum(len(statement.holdings) for statement in statements)
    brokerages = {connection.institution or connection.connection_id for connection, _ in shown}
    return [
        {
            "label": "Accounts",
            "value": str(len(shown)),
            "delta": f"{attention} not up to date" if attention else "all up to date",
            "ink": "warn-ink" if attention else "",
        },
        {"label": "Brokerages", "value": str(len(brokerages))},
        {
            "label": "Rows",
            "value": str(rows),
            "delta": f"in {_plural(len(statements), 'statement')}",
        },
        _last_read(status),
    ]


_MODE = {
    "synthetic": " Synthetic mode: every figure here is invented, not read from SnapTrade.",
    "snaptrade": "",
    "waiting": " SnapTrade is not being read yet.",
}


def _statements(caller: meridian.Caller, notice: Notice | None = None) -> meridian.Response:
    """Statements: for each account linked to one the caller may read, its
    sync state, its last statement and its rows, as the last read found them;
    or plainly nothing, for somebody who may read none."""
    status = _now().syncer.status
    shown = visible(status, _links_of(status), caller) if caller.read else []
    return _html(
        pages.render(
            "statements.html",
            dot=_dot(status, setup=False),
            back=STATEMENTS,
            notice=notice,
            # Who may read nothing here is told so, and nothing is read for them.
            nothing=not caller.read,
            mode=_MODE[status.mode],
            read_at=status.read_at,
            failed=bool(status.error),
            tiles=_statement_tiles(status, shown) if shown else [],
            columns=_ROW_COLUMNS,
            statements=[
                _statement(index, connection, view, status)
                for index, (connection, view) in enumerate(shown)
            ],
        )
    )


def _statements_read(caller: meridian.Caller) -> records.StatementsRead:
    """Statements as data: each account linked to one the caller may read,
    its sync state and its last statement's rows, as recorded."""
    status = _now().syncer.status
    links = _links_of(status)
    shown = visible(status, links, caller) if caller.read else []
    made: list[records.StatementRead] = []
    for _, view in shown:
        fresh, statement = view.freshness, view.statement
        recorded, _, _ = _recorded(view, status)
        made.append(
            records.StatementRead(
                account=links[view.account.external_account_id].account_id,
                external_account_id=view.account.external_account_id,
                name=view.account.name,
                state=_STATE_LABEL[fresh.state],
                detail=fresh.detail or view.withheld,
                holdings_as_of=fresh.holdings_as_of.isoformat() if fresh.holdings_as_of else "",
                history_as_of=fresh.history_as_of.isoformat() if fresh.history_as_of else "",
                history_from=view.history_from,
                as_of_date=statement.as_of_date if statement is not None else "",
                recorded=recorded,
                rows=[_row_read(holding) for holding in statement.holdings]
                if statement is not None
                else [],
            )
        )
    return records.StatementsRead(
        read_at=status.read_at.isoformat() if status.read_at else "",
        mode=status.mode,
        statements=made,
    )


def _row_read(holding: Holding) -> records.RowRead:
    shown = next(
        (i for i in holding.identifiers if i.scheme == "symbol"), holding.identifiers[0]
    )
    return records.RowRead(
        instrument=shown.value,
        description=holding.description,
        kind=holding.kind,
        side=holding.side.value,
        quantity=holding.quantity,
        settled=holding.settle_date_quantity,
        currency=holding.currency,
        average_purchase_price=Decimal(holding.average_cost.amount)
        if holding.average_cost
        else None,
        lots=[
            records.LotRead(
                quantity=lot.quantity,
                cost=Decimal(lot.cost.amount) if lot.cost else None,
                acquired=lot.acquired_date,
            )
            for lot in holding.lots
        ],
    )


@pages.page(
    STATEMENTS,
    "Statements",
    levels=["write", "read"],
    answers=records.StatementsRead,
    name="read_statements",
    description=(
        "Each account linked to one the person may read: its sync state, how far back "
        "SnapTrade holds its history, and its last statement's rows as recorded -- quantity, "
        "settled quantity, SnapTrade's average purchase price per unit and its tax lots, as "
        "reported."
    ),
)
async def statements(request: meridian.Request) -> meridian.Response:
    if request.tool_name:
        return pages.answer("statements.html", _statements_read(request.caller))
    return _statements(request.caller)


# ── History, at write and read ──────────────────────────────────────────────
#
# An account's activities over a range, read from SnapTrade when asked, and
# the lots proposed from the purchases in its whole history (history.py):
# for each account linked to one the person may read, by the deployment's
# account, as the opening balance names it. Each read is kept
# as a raw record of the account's (raw.py), and named in what it answers.

# How long the whole of an account's history may take to read, within the
# time the page's server gives a view.
_HISTORY_SECONDS = REQUEST_SECONDS - 15


# An account's activities, one grid; and a position's proposed lots, one each.
_ACTIVITY_COLUMNS = (
    Column("trade_date", "Traded"),
    Column("type", "Type", type="code"),
    Column("symbol", "Symbol", type="code", hint="description", blank="none"),
    Column("units", "Units", type="decimal", blank="not given"),
    Column("price", "Price", type="decimal", blank="not given"),
    Column("amount", "Amount", type="decimal", blank="not given"),
    Column("currency", "Currency"),
    Column("settlement_date", "Settles"),
)
_PROPOSED_COLUMNS = (
    Column("quantity", "Quantity", type="decimal"),
    Column("cost", "Cost", type="decimal", blank="not stated"),
    Column("currency", "Currency"),
    Column("acquired", "Acquired", blank="for the person to supply"),
    Column("source", "Source"),
)


def _figure(value: Decimal | None) -> str:
    """A number as the page shows it: as written, never a float."""
    return "" if value is None else format(value, "f")


def _grid(
    grid_id: str,
    caption: str,
    empty: str,
    columns: Sequence[Column],
    rows: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "id": grid_id,
        "caption": caption,
        "empty": empty,
        "rows": rows,
        "data": {"columns": [c.declared() for c in columns], "rows": rows},
    }


def _activity_grid(data: history.Activities) -> dict[str, Any]:
    rows = [
        {
            "key": f"{index}:{each.id}",
            "trade_date": each.trade_date[:10],
            "type": each.type,
            "symbol": each.symbol or each.option_symbol,
            "description": each.description,
            "units": _figure(each.units),
            "price": _figure(each.price),
            "amount": _figure(each.amount),
            "currency": each.currency,
            "settlement_date": each.settlement_date[:10],
        }
        for index, each in enumerate(data.activities)
    ]
    return _grid(
        "activities",
        "Activities",
        "No activities traded in this range.",
        _ACTIVITY_COLUMNS,
        rows,
    )


def _history_href(data: history.Activities, offset: int) -> str:
    return _raw_href(
        HISTORY,
        account=data.account,
        start=data.start.isoformat(),
        end=data.end.isoformat(),
        limit=data.limit,
        offset=offset,
    )


def _page_href(data: history.Activities, *, earlier: bool) -> str:
    """The link to the page before this one, or the one after, where there is one."""
    if earlier:
        return _history_href(data, max(data.offset - data.limit, 0)) if data.offset else ""
    more = (
        data.offset + len(data.activities) < data.total
        if data.total is not None
        else len(data.activities) == data.limit
    )
    return _history_href(data, data.offset + data.limit) if more else ""


def _shown_positions(data: history.ProposedLots) -> list[dict[str, Any]]:
    return [
        {
            "instrument": each.instrument,
            "description": each.description,
            "side": each.side,
            "quantity": _figure(each.quantity),
            "currency": each.currency,
            "average": _figure(each.average_purchase_price),
            "said": each.said,
            "grid": _grid(
                f"lots-{index}",
                f"{each.instrument}: lots proposed",
                "No lot proposed.",
                _PROPOSED_COLUMNS,
                [
                    {
                        "key": f"{index}:{n}",
                        "quantity": _figure(lot.quantity),
                        "cost": _figure(lot.cost),
                        "currency": lot.currency,
                        "acquired": lot.acquired.isoformat() if lot.acquired else "",
                        "source": lot.source,
                    }
                    for n, lot in enumerate(each.proposed)
                ],
            ),
        }
        for index, each in enumerate(data.positions)
    ]


@dataclass(frozen=True)
class Found:
    """An account the person may read, and the external account linked to it
    as the last read reached it."""

    account: str
    connection: ConnectionView
    view: AccountView


@dataclass(frozen=True)
class NotFound:
    """Why an account asked for is not one to read here: the refusal's
    words, its field's, and its reason."""

    detail: str
    message: str
    reason: str


def _readable_accounts(caller: meridian.Caller) -> list[dict[str, str]]:
    """The deployment's accounts the caller may read that one of SnapTrade's
    is linked to, as the History tab offers them."""
    status = _now().syncer.status
    links = _links_of(status)
    offered = []
    for connection, view in visible(status, links, caller):
        link = links[view.account.external_account_id]
        offered.append(
            {
                "account_id": link.account_id,
                "label": f"{link.account_name or link.account_id} "
                f"({view.account.name}, {connection.institution or 'Unknown brokerage'})",
            }
        )
    return offered


def _found(caller: meridian.Caller, account: str) -> Found | NotFound:
    """The account asked for, linked and reached; or why not, the same for
    an account that is not the caller's and one that does not exist."""
    if not account:
        return NotFound(
            "Name the account.", "the deployment's account, one you may read", "refused"
        )
    if not caller.may_read(account):
        return NotFound(
            "Not an account you may read.", "not an account you may read", "permission_denied"
        )
    status = _now().syncer.status
    links = _links_of(status)
    for connection in status.connections:
        for view in connection.accounts:
            link = links[view.account.external_account_id]
            if link.state is Link.LINKED and link.account_id == account:
                return Found(account, connection, view)
    return NotFound(
        "No account SnapTrade's last read reached is linked to it.",
        "no SnapTrade account is linked to it",
        "not_linked",
    )


def _history_context(request: meridian.Request, account: str) -> dict[str, Any]:
    status = _now().syncer.status
    return {
        "dot": _dot(status, setup=False),
        "nothing": not request.caller.read,
        "accounts": _readable_accounts(request.caller),
        "chosen": account,
        "mode": _MODE[status.mode],
        "most_days": history.MOST_DAYS,
        "most_limit": history.MOST_LIMIT,
    }


def _history_refused(
    request: meridian.Request,
    template: str,
    account: str,
    detail: str,
    *fields: tuple[str, str],
    reason: str = "refused",
) -> meridian.Response:
    """A refusal by path for a tool; for a browser, the page saying it."""
    if request.tool_name:
        pages.refuse(detail, *fields, reason=reason)
    words = "; ".join([detail] + [f"{path}: {message}" for path, message in fields])
    return _html(
        pages.render(
            template,
            data=None,
            notice=Notice(words, "warn"),
            **_history_context(request, account),
        )
    )


def _kept(found: Found, calls: list[dict[str, Any]]) -> str:
    """The raw record a history read is kept as, named as a row names its
    own (the account, the read and the call); "" where nothing is kept."""
    if not calls:
        return ""
    held = _now()
    key = held.syncer.keep_history(
        history_taken(
            found.view.account, held.syncer.now(), calls, held.syncer.config.synthetic
        )
    )
    return f"{found.view.account.external_account_id}/{key}/activities" if key else ""


def _connection_named(connection: ConnectionView) -> str:
    return _connection_label(connection) + f" ({connection.connection_id})"


@pages.page(
    HISTORY,
    "History",
    levels=["write", "read"],
    params=history.HistoryAsked,
    answers=history.Activities,
    name="read_account_activities",
    description=(
        "An account's activities as SnapTrade reports them -- buys, sells, transfers, "
        "dividends, each with its type, dates, symbol, units, price, amount and currency as "
        f"written -- traded from start to end (at most {history.MOST_DAYS} days; the "
        f"{history.DEFAULT_DAYS} days to today by default), limit of them from offset (at "
        f"most {history.MOST_LIMIT}), for an account the person may read, named as the "
        "deployment names it. history_from is the account's first transaction SnapTrade "
        "holds: nothing before it can be read."
    ),
)
async def account_history(request: meridian.Request) -> meridian.Response:
    """An account's activities over a range, as SnapTrade reports them."""
    asked = request.params or history.HistoryAsked()
    account = asked.account.strip()
    held = _now()
    template = "history.html"
    if not request.tool_name:
        if request.param_errors:
            fields = tuple((p.path, p.message) for p in request.param_errors)
            return _history_refused(request, template, account, "Check the range.", *fields)
        if not account:
            return _html(
                pages.render(template, data=None, notice=None, **_history_context(request, ""))
            )
    today = held.syncer.now().date()
    end = asked.end or today
    start = asked.start or end - timedelta(days=history.DEFAULT_DAYS)
    if end > today:
        return _history_refused(
            request, template, account, "The range ends after today.", ("end", f"after {today}")
        )
    if start > end:
        return _history_refused(
            request,
            template,
            account,
            "The range starts after it ends.",
            ("start", f"after {end}"),
        )
    if (end - start).days >= history.MOST_DAYS:
        return _history_refused(
            request,
            template,
            account,
            f"The range is longer than {history.MOST_DAYS} days.",
            (
                "start",
                f"more than {history.MOST_DAYS - 1} days before end; read a year at a time",
            ),
        )
    found = _found(request.caller, account)
    if isinstance(found, NotFound):
        return _history_refused(
            request,
            template,
            account,
            found.detail,
            ("account", found.message),
            reason=found.reason,
        )
    venue = held.syncer.venue
    if venue is None:
        return _history_refused(
            request, template, account, _NO_VENUE.text, reason="unavailable"
        )
    snaptrade_id = found.view.account.snaptrade_account_id
    try:
        got = await asyncio.wait_for(
            venue.activity_page(snaptrade_id, start, end, asked.offset, asked.limit),
            _ASKING_SECONDS,
        )
    except VenueError as failed:
        return _history_refused(request, template, account, str(failed), reason="unavailable")
    except Exception as failed:
        said = f"That failed: {type(failed).__name__}"
        return _history_refused(request, template, account, said, reason="unavailable")
    note = history.range_note(start, end, asked.offset, asked.limit)
    ref = _kept(found, [activities_call(got.body, note)])
    rows = [history.activity(each) for each in got.activities]
    words = [
        f"{len(rows)} of {got.total if got.total is not None else 'an unstated number of'} "
        f"activities traded from {start} to {end}, from {asked.offset}."
    ]
    since = found.view.history_from
    if since and start.isoformat() < since:
        words.append(f"SnapTrade holds this account's history from {since}; nothing before it.")
    elif not since:
        words.append("SnapTrade states no first transaction date for this account.")
    data = history.Activities(
        account=account,
        external_account_id=found.view.account.external_account_id,
        connection=_connection_named(found.connection),
        history_from=since,
        start=start,
        end=end,
        offset=asked.offset,
        limit=asked.limit,
        total=got.total,
        activities=rows,
        raw_record=ref,
        said=" ".join(words),
    )
    return pages.answer(
        template,
        data,
        notice=None,
        grid=_activity_grid(data),
        columns=_ACTIVITY_COLUMNS,
        earlier=_page_href(data, earlier=True),
        later=_page_href(data, earlier=False),
        **_history_context(request, account),
    )


@pages.route(
    LOTS,
    levels=["write", "read"],
    params=history.LotsAsked,
    answers=history.ProposedLots,
    name="read_proposed_lots",
    description=(
        "Lots proposed, never confirmed, for each position of an account's last statement "
        "that SnapTrade lists no tax lots for: from its purchases in SnapTrade's activities "
        "(one lot per purchase: its units, the amount paid and its trade date), each naming "
        "its source, in the fields an opening balance's lot takes. No FIFO and no other rule "
        "SnapTrade does not state: a position whose purchases do not account for it (a sale, "
        "a transfer, a corporate action) gets no lot, and says why; SnapTrade's average "
        "purchase price is beside each position, as reported, never a lot. For an account "
        "the person may read, named as the deployment names it."
    ),
)
async def proposed_lots(request: meridian.Request) -> meridian.Response:
    """Lots proposed for an account's positions that SnapTrade lists none for."""
    asked = request.params or history.LotsAsked()
    account = asked.account.strip()
    held = _now()
    template = "lots.html"
    found = _found(request.caller, account)
    if isinstance(found, NotFound):
        return _history_refused(
            request,
            template,
            account,
            found.detail,
            ("account", found.message),
            reason=found.reason,
        )
    statement = found.view.statement
    status = held.syncer.status
    if statement is None or status.read_at is None:
        said = f"No statement on the last read: {found.view.withheld or 'nothing was read'}."
        return _history_refused(request, template, account, said, reason="no_statement")
    venue = held.syncer.venue
    since = found.view.history_from
    start = date.fromisoformat(since) if since else None
    end = held.syncer.now().date()
    if venue is None:
        whole = history.Whole(None, [], "SnapTrade is not being read")
    else:
        try:
            whole = await asyncio.wait_for(
                history.read_whole(venue, found.view.account.snaptrade_account_id, start, end),
                _HISTORY_SECONDS,
            )
        except TimeoutError:
            whole = history.Whole(
                None, [], f"SnapTrade did not answer within {round(_HISTORY_SECONDS)} seconds"
            )
    ref = _kept(found, whole.calls)
    positions = [
        history.propose(holding, whole.activities, whole.said, since, status.read_at)
        for holding in statement.holdings
        if holding.kind != "cash"
    ]
    proposing = sum(1 for each in positions if each.proposed)
    data = history.ProposedLots(
        account=account,
        external_account_id=found.view.account.external_account_id,
        statement=statement.external_statement_id,
        as_of=statement.as_of_date,
        read_at=status.read_at.isoformat(),
        history_from=since,
        history_read=(
            f"{whole.said}, traded from {since or 'the first SnapTrade holds'} to {end}"
            if whole.activities is not None
            else f"not read whole: {whole.said}"
        ),
        raw_record=ref,
        positions=positions,
        said=(
            f"Lots proposed for {proposing} of {len(positions)} positions: proposals, never "
            "confirmed here. Each names its source; the person checks them and answers for "
            "them in the opening balance."
        ),
    )
    return pages.answer(
        template,
        data,
        notice=None,
        shown=_shown_positions(data),
        columns=_PROPOSED_COLUMNS,
        **_history_context(request, account),
    )


# ── Raw responses, at write and read ────────────────────────────────────────
#
# SnapTrade's responses to each read, as received, per account (raw.py): the
# latest read of each account the person may read, each call by name and
# request with its JSON body formatted, then the older reads still kept, each
# opened here and each downloadable as JSON. An account's data, so never at
# admin: Manage shows no account's data. Cut to the person by the plugin's
# links, as Statements is, and not by what the last read reached, so a read
# kept before a restart is shown before the next read.

# The older reads listed at once for an account; `older` pages through them.
OLDER_AT_ONCE = 20


@dataclass(frozen=True)
class RawAccount:
    """An account whose raw responses this person may read."""

    external_account_id: str
    name: str
    where: str


def raw_accounts(caller: meridian.Caller) -> list[RawAccount]:
    """The external accounts linked to an account `caller` may read: those
    the last read reached first, in its order, then any other the plugin's
    links name, whose kept reads may predate this process."""
    if not caller.read:
        return []
    held = _now()
    reached = [
        RawAccount(view.account.external_account_id, view.account.name, _where(c, view))
        for c in held.syncer.status.connections
        for view in c.accounts
    ]
    known = {account.external_account_id for account in reached}
    others = [
        RawAccount(
            link.external_account_id, link.external_account_id, "Not reached by the last read"
        )
        for link in sorted(held.links.scope.links, key=lambda link: link.external_account_id)
        if link.external_account_id not in known
    ]
    return [
        account
        for account in reached + others
        if _readable(held.links.of(account.external_account_id), caller)
    ]


def _raw_href(path: str, **query: str | int) -> str:
    return f"{path}?{urlencode(query)}"


def _shown_call(call: Mapping[str, Any]) -> dict[str, str]:
    """One call of a kept read, as the tab shows it: by name and request, its
    body formatted (every number as SnapTrade wrote it), or why it failed."""
    failed = call.get("failed")
    return {
        "call": str(call.get("call", "")),
        "request": str(call.get("request", "")),
        "note": str(call.get("note", "")),
        "failed": str(failed) if failed else "",
        "json": "" if failed else dumps(call.get("body"), indent=2),
    }


def _shown_record(account: RawAccount, record: Record) -> dict[str, Any]:
    return {
        "key": record.key,
        "read_at": record.read_at,
        "synthetic": record.synthetic,
        "calls": [_shown_call(call) for call in record.calls],
        "download": _raw_href(
            RAW_DOWNLOAD, account=account.external_account_id, read=record.key
        ),
    }


def _number(text: str) -> int:
    return int(text) if text.isdigit() else 0


def _raw(request: meridian.Request, notice: Notice | None = None) -> meridian.Response:
    """Raw responses: for each account the person may read (or the one the
    query names), the read asked for or the latest, then the older reads kept."""
    caller = request.caller
    held = _now()
    store = held.syncer.raw
    accounts = raw_accounts(caller)
    asked = request.query.get("account", "").strip()
    # A row's reference to its raw record (contract v11), followed here: the
    # account, the read and the call it names.
    ref = request.query.get("ref", "").strip()
    ref_read, ref_call, ref_activity = "", "", ""
    # An activity's reference (contract v14): its own record, of the account.
    activity_named = parse_activity_key(ref) if ref else None
    if activity_named is not None:
        asked, ref_activity = activity_named
    elif ref:
        named = parse_record_key(ref)
        if named is None:
            return _said("No such record.", 404)
        asked, ref_read, ref_call = named
    if asked and asked not in {a.external_account_id for a in accounts}:
        # Not one of theirs, or no such account: the same answer for both.
        return _said("No such account.", 404)
    chosen = [a for a in accounts if a.external_account_id == asked] if asked else accounts
    key = (ref_read if ref else request.query.get("read", "").strip()) if asked else ""
    older = _number(request.query.get("older", "")) if asked else 0
    shown: list[dict[str, Any]] = []
    for account in chosen:
        reads = store.reads(account.external_account_id) if store is not None else []
        record = None
        if store is not None and ref_activity:
            record = store.activity_record(account.external_account_id, ref_activity)
        elif store is not None:
            record = (
                store.record(account.external_account_id, key)
                if key
                else store.latest(account.external_account_id)
            )
        current = record.key if record is not None else ""
        others = [r for r in reads if r.key != current]
        page = others[older : older + OLDER_AT_ONCE]
        here = {"account": account.external_account_id}
        shown.append(
            {
                "external_account_id": account.external_account_id,
                "name": account.name,
                "where": account.where,
                "record": _shown_record(account, record) if record is not None else None,
                # A read asked for by key that is no longer kept.
                "gone": bool(key or ref_activity) and record is None,
                "older": [
                    {
                        "read_at": r.read_at,
                        "href": _raw_href(RAW, **here, read=r.key),
                        "download": _raw_href(RAW_DOWNLOAD, **here, read=r.key),
                    }
                    for r in page
                ],
                "older_total": len(others),
                "older_from": older + 1 if page else 0,
                "older_to": older + len(page),
                "earlier": _raw_href(RAW, **here, older=older + OLDER_AT_ONCE)
                if older + OLDER_AT_ONCE < len(others)
                else "",
                "later": _raw_href(RAW, **here, older=max(older - OLDER_AT_ONCE, 0))
                if older > 0
                else "",
                "all": _raw_href(RAW, **here),
            }
        )
    status = held.syncer.status
    return _html(
        pages.render(
            "raw.html",
            dot=_dot(status, setup=False),
            nothing=not caller.read,
            accounts=shown,
            focused=bool(asked),
            back=RAW,
            notice=notice
            or (
                Notice(
                    f"The record a row references: SnapTrade's answer to "
                    f"\u201c{CALLS[ref_call][0]}\u201d in this read."
                )
                if ref_call
                else Notice(
                    "The record an activity references: SnapTrade's entry for it, as the "
                    "read that first reported it received it."
                )
                if ref_activity
                else None
            ),
            kept=store is not None,
            failure=store.failure if store is not None else "",
            retention_days=store.retention.days if store is not None else 0,
            mode=_MODE[status.mode],
        )
    )


@pages.page(
    RAW,
    "Raw responses",
    levels=["write", "read"],
    tool=False,
    why=(
        "SnapTrade's answers as received, any JSON it sent, which no typed record holds; "
        "an agent reads what they came to with read_statements, read_account_activities "
        "and read_proposed_lots, each naming its raw record"
    ),
)
async def raw_responses(request: meridian.Request) -> meridian.Response:
    return _raw(request)


def _filename(external_account_id: str, key: str) -> str:
    plain = "".join(c if c.isalnum() or c in "-." else "-" for c in external_account_id)
    named = "".join(c if c.isalnum() or c in "-." else "-" for c in key.rpartition("/")[2])
    return f"snaptrade-raw-{plain}-{named}.json"


@pages.route(
    RAW_DOWNLOAD,
    levels=["write", "read"],
    tool=False,
    why="a file for a person to save: SnapTrade's answers as received, as kept",
)
async def raw_download(request: meridian.Request) -> meridian.Response:
    """One kept read of one account the person may read, as the JSON file it
    is kept as, to save: the read the query names, or the latest."""
    store = _now().syncer.raw
    asked = request.query.get("account", "").strip()
    readable = {a.external_account_id for a in raw_accounts(request.caller)}
    if store is None or not asked or asked not in readable:
        return _said("No such account.", 404)
    key = request.query.get("read", "").strip()
    activity = parse_activity_key(key)
    if activity is not None:
        record = store.activity_record(asked, activity[1]) if activity[0] == asked else None
    else:
        record = store.record(asked, key) if key else store.latest(asked)
    if record is None:
        return _said("That read is not kept.", 404)
    return meridian.Response(
        dumps(record.document, indent=2) + "\n",
        200,
        "application/json; charset=utf-8",
        (
            ("content-disposition", f'attachment; filename="{_filename(asked, record.key)}"'),
            ("cache-control", "no-store"),
        ),
    )


# ── Reading now: Refresh ────────────────────────────────────────────────────


READING = "Reading SnapTrade now. Reload in a moment."


@pages.route(
    READ,
    levels=["admin", "write"],
    methods=["POST"],
    params=records.ReadAsked,
    answers=records.ReadStarted,
    name="read_snaptrade_now",
    description=(
        "Read SnapTrade now, every connection and account, and record what it reads; "
        "read_statements shows it once the read is done. Changes nothing at SnapTrade."
    ),
)
async def read_now(request: meridian.Request) -> meridian.Response:
    """Read SnapTrade now: Refresh, in the Connections card beside + Add, and
    in the head on Statements and Raw responses under Open (View acts on
    nothing; Account links has none, the product owner 2026-09-30). It answers with the page
    it was asked from, among those of the session's level."""
    if (refused := _oversized(request)) is not None:
        return refused
    _now().wake.set()
    if request.tool_name:
        return pages.answer("statements.html", records.ReadStarted(READING))
    notice = Notice(READING)
    asked = request.params
    back = asked.back.strip() if asked is not None else ""
    if not request.caller.admin:
        if back == RAW:
            return _raw(request, notice)
        return _statements(request.caller, notice)
    if back == ACCOUNTS:
        return await _accounts(request, notice)
    return _connections(notice)
