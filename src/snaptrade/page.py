"""The plugin's pages, declared and served on the SDK's `meridian.Pages`:
Connections and Account links under Manage, Statements under Open and View.

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
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlencode

import meridian
from meridian.pages import CSRF_FIELD, REQUEST_SECONDS

from .linking import LINKS_AT_ONCE, Link, Links, LinkView, Offered, refusal
from .normalise import REMEDY, AccountView, ConnectionView, Holding, Serving, SyncState
from .settings import label
from .sync import ATTENTION, Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
# The kit's version these pages were built against. The dashboard serves the
# deployment's newest 0.x for it; pinning one keeps the pages as they were built.
KIT = "0.7.0"

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
CONNECTIONS = "/admin/connections"
ACCOUNTS = "/admin/accounts"
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
}
# The kit's badge tones: status, never market direction.
_STATE_TONE = {
    SyncState.CURRENT: "good",
    SyncState.STALE: "warn",
    SyncState.NEEDS_SIGN_IN: "bad",
    SyncState.DISABLED: "bad",
    SyncState.DELAYED_BY_DESIGN: "info",
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
        # Reconnecting is what a disabled connection asks for, so it leads there.
        "reconnect": "primary" if connection.state is SyncState.DISABLED else "",
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


@pages.page(CONNECTIONS, "Connections", levels="admin")
async def connections(request: meridian.Request) -> meridian.Response:
    return _connections()


@pages.route("/admin", levels="admin")
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


@pages.route(CONNECT, levels="admin", methods=["POST"])
async def connect(request: meridian.Request) -> meridian.Response:
    """SnapTrade's Connection Portal, to connect a brokerage."""
    if (refused := _oversized(request)) is not None:
        return refused
    venue = _now().syncer.venue
    if venue is None:
        return _connections(_NO_VENUE)
    portal, failed = await _asking(venue.connection_portal())
    return _connections(failed, portal)


def _connection_asked(request: meridian.Request) -> str | meridian.Response:
    """The connection a form names, among those the last read reached; or the
    answer that says it is not one of them."""
    connection_id = request.form.get("connection_id", "").strip()
    reached = {c.connection_id for c in _now().syncer.status.connections}
    return connection_id if connection_id in reached else _said("No such connection.", 404)


@pages.route(REFRESH, levels="admin", methods=["POST"])
async def refresh(request: meridian.Request) -> meridian.Response:
    """Ask SnapTrade to read a connection's brokerage again, where that means
    something; its refusal is said plainly."""
    if (refused := _oversized(request)) is not None:
        return refused
    held = _now()
    venue = held.syncer.venue
    if venue is None:
        return _connections(_NO_VENUE)
    asked = _connection_asked(request)
    if isinstance(asked, meridian.Response):
        return asked
    serving = {c.connection_id: c.serving for c in held.syncer.status.connections}
    if serving[asked] is Serving.REAL_TIME:
        # Offered no Refresh; a form from an older page is answered without
        # asking SnapTrade.
        return _connections(Notice(REAL_TIME))
    try:
        said = await asyncio.wait_for(venue.refresh(asked), _ASKING_SECONDS)
    except VenueError as failed:
        # SnapTrade's refusal (Real-time plans refuse a refresh), said
        # plainly; any other failure is shown as every other is.
        if failed.status != 403:
            return _connections(Notice(str(failed), "bad"))
        return _connections(Notice(REFRESH_REFUSED))
    except Exception as failed:
        return _connections(Notice(f"That failed: {type(failed).__name__}", "bad"))
    held.wake.set()
    return _connections(Notice(said))


@pages.route(RECONNECT, levels="admin", methods=["POST"])
async def reconnect(request: meridian.Request) -> meridian.Response:
    """SnapTrade's Connection Portal, to sign in to a connection's brokerage again."""
    if (refused := _oversized(request)) is not None:
        return refused
    venue = _now().syncer.venue
    if venue is None:
        return _connections(_NO_VENUE)
    asked = _connection_asked(request)
    if isinstance(asked, meridian.Response):
        return asked
    portal, failed = await _asking(venue.connection_portal(reconnect=asked))
    return _connections(failed, portal)


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
    shown: Sequence[Shown], links: Mapping[str, LinkView], offered: Offered
) -> dict[str, object]:
    """What om-account-map takes: the external accounts' identities (with the
    number the map matches on, and the connection it groups by), the
    deployment's accounts (null where they could not be read), and the links
    standing."""
    accounts = (
        None
        if offered.refused
        else [
            {
                "account_id": account.account_id,
                "name": account.name,
                "custodian": account.custodian,
                "account_type": account.account_type,
                "open": account.open,
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
                "account_type": view.account.account_type,
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
    of the deployment's here, in the one account map."""
    status = _now().syncer.status
    links = _links_of(status)
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
            map=_map_data(shown, links, offered),
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
                if a.open
            ],
            most_name=_MOST_NAME,
            token_name=CSRF_FIELD,
        )
    )


@pages.page(ACCOUNTS, "Account links", levels="admin")
async def accounts(request: meridian.Request) -> meridian.Response:
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
    `hint` names a field shown under the value."""

    key: str
    label: str
    type: str = "text"
    group: bool = False
    hint: str = ""

    def declared(self) -> dict[str, object]:
        """As om-grid's columns take it, plain JSON: only what is set."""
        column: dict[str, object] = {"key": self.key, "label": self.label, "type": self.type}
        if self.group:
            column["group"] = True
        if self.hint:
            column["hint"] = self.hint
        return column


# A statement's rows, one grid per account.
_ROW_COLUMNS = (
    Column("instrument", "Instrument", type="code", hint="identifiers"),
    Column("quantity", "Quantity", type="decimal", group=True),
    Column("currency", "Currency"),
    Column("side", "Side"),
    Column("description", "Description", hint="notes"),
    Column("kind", "Kind"),
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
    if outcome.placeholders:
        extra.append(f"{outcome.placeholders} awaiting an instrument")
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
        notes.append(f"SnapTrade stated no currency; {holding.currency} is assumed")
    if holding.cash_equivalent:
        notes.append("SnapTrade counts it in cash too")
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
        "kind": holding.kind or "not given",
        "side": holding.side.value.capitalize(),
        # Exact, as read: a decimal string, never a float.
        "quantity": format(holding.quantity, "f"),
        "currency": holding.currency,
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


@pages.page(STATEMENTS, "Statements", levels=["write", "read"])
async def statements(request: meridian.Request) -> meridian.Response:
    return _statements(request.caller)


# ── Reading now: Refresh ────────────────────────────────────────────────────


@pages.route(READ, levels=["admin", "write"], methods=["POST"])
async def read_now(request: meridian.Request) -> meridian.Response:
    """Read SnapTrade now: Refresh, in the Connections card beside + Add, and
    in the head on Statements under Open (View acts on nothing; Account
    links has none, the product owner 2026-09-30). It answers with the page
    it was asked from, among those of the session's level."""
    if (refused := _oversized(request)) is not None:
        return refused
    _now().wake.set()
    notice = Notice("Reading SnapTrade now. Reload in a moment.")
    if not request.caller.admin:
        return _statements(request.caller, notice)
    if request.form.get("back", "").strip() == ACCOUNTS:
        return await _accounts(request, notice)
    return _connections(notice)
