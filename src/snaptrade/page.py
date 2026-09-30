"""The plugin's pages: Statements, its user side, and two admin pages.

Ruled by the product owner on 2026-09-29
(intent/a-custody-plugin-serves-its-statement-receivers, "Admin pages and
user pages", step 1): admin pages are setup, user pages are daily work.

- **Statements** (`/`), the user side: per account, its sync state, its last
  statement and its rows, as the last read found them. It is shown to a
  person who may read (decisions/026), cut to the accounts they may read
  (`caller.may_read`), and to a deployment admin, who sees every account.
  Somebody with nothing to read is told so plainly. Its sections become tabs
  inside the page once there is more than one; today there is one.
- **Connections** and **Account links**, the admin pages, declared at
  registration (`ADMIN_PAGES`), which the dashboard's admin view of the
  instance shows as tabs after its own, each framing its path ("tabs at both
  levels", 2026-09-29). They cover what only SnapTrade knows and linking the
  accounts it reaches: its users under the key, the brokerage connections,
  connecting one through SnapTrade's Connection Portal, each connection's
  health and each account's sync state with what to do, linking each account
  to one of the deployment's (W6.4, point 8 of kernel/a-plugins-admin-view),
  and reconnecting a connection, or refreshing one SnapTrade serves on a
  delay (a real-time one has nothing to refresh, and on SnapTrade's Real-time
  plans it refuses to). They are served to a caller
  whose verified `deployment_admin` claim is true, and to nobody else;
  `/admin` sends a caller to Connections. The SnapTrade keys are entered in
  the dashboard's settings form, never here.

Which of the deployment's accounts an external account is linked to is what
cuts Statements to a reader. The plugin reads its links beside its account
scope (W4.11), each with the linked account's name, and holds the latest
delivery (linking.py): a reader sees each account linked to one they may
read, before a restart and after it. Nothing is stored for it: plugins are
ephemeral.

Built on the kit (spec/plugin-pages-share-one-kit, requirement 5): the
dashboard serves Open Meridian's UI kit at /.meridian/ui/<version>/ on this
plugin's own host, and each page links its stylesheet and script, uses its
classes and its components (om-grid, om-account-map, om-moment), and has no
style, colour, theme code or script of its own.

Each component reads its data from the JSON declared inside it (the kit's
"data without script"), and what else is inside it is what a browser shows
where the kit is not served: each grid's table, and the account map's plain
forms. So without the kit a page still works, unstyled, and every action is a
plain form.

Every action is a POST carrying a CSRF token in its form, checked before
anything is done. The plugin host has its own session cookie (decisions/021),
so without it a page elsewhere could have an administrator's browser post
here. The token is an HMAC of the verified caller's subject under a secret
this process makes at start: nobody else can make it, it is the same across
the per-request assertions the dashboard mints for one person, and a restart
makes every page open before it stale, which is only a reload. Statements has
no action.

The standard library's server, as the SDK's reference plugin uses.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import html
import http.server
import json
import secrets
import threading
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar
from urllib.parse import parse_qs

import meridian

from .linking import LINKS_AT_ONCE, Link, Links, LinkView, Offered, refusal
from .normalise import REMEDY, AccountView, ConnectionView, Holding, Serving, SyncState
from .settings import label
from .sync import Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
# The kit's version these pages were built against. The dashboard serves the
# deployment's; pinning one keeps the pages as they were built.
KIT = "/.meridian/ui/0.5.0/"
CSRF_FIELD = "csrf"
# A form here carries a token, an account, and a new account's name, custodian
# and type at most; anything longer is not one of this page's.
_MOST_BODY = 8192
# But the map's several-link form carries a pair of IDs for each link, a
# hundred bytes or so a pair: room for some thousands of accounts.
_MOST_LINKS_BODY = 1 << 20
# The longest name a new account is given here, and the longest custodian or
# type the deployment keeps for one (W6.3).
_MOST_NAME = 200

# The user side: what a person who may read is shown.
STATEMENTS = "/"
CONNECTIONS = "/admin/connections"
ACCOUNTS = "/admin/accounts"
# The admin pages, in the order the dashboard shows them as tabs: setup only.
ADMIN_PAGES = (
    meridian.Page(CONNECTIONS, "Connections"),
    meridian.Page(ACCOUNTS, "Account links"),
)

_Answer = TypeVar("_Answer")


def is_administrator(caller: meridian.Caller) -> bool:
    """Whether the verified caller is a deployment administrator: the claim
    the dashboard sets and the sidecar verifies (W6.9). Only True counts."""
    return caller.deployment_admin is True


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


class CsrfTokens:
    """Tokens for the admin page's forms, one per person, from a secret made
    when the plugin starts and held nowhere but this process."""

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret if secret is not None else secrets.token_bytes(32)

    def __repr__(self) -> str:
        return "CsrfTokens()"

    def token(self, caller: meridian.Caller) -> str:
        return hmac.new(self._secret, caller.subject.encode(), hashlib.sha256).hexdigest()

    def valid(self, caller: meridian.Caller, presented: str) -> bool:
        return bool(presented) and hmac.compare_digest(self.token(caller), presented)


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
# The states that ask a person to do something.
_ATTENTION = (SyncState.STALE, SyncState.NEEDS_SIGN_IN, SyncState.DISABLED)
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
_LINK_LABEL = {Link.LINKED: "Linked", Link.UNLINKED: "Not linked"}
_LINK_TONE = {Link.LINKED: "good", Link.UNLINKED: "warn"}
# The tones a grid column may take from a row's field, and the text class each
# is on a column that is not a badge; anything else is no tone.
_INK = {"good": "good-ink", "warn": "warn-ink", "bad": "bad-ink"}
_BADGE_TONES = frozenset({"good", "warn", "bad", "accent", "info"})


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _script_json(value: object) -> str:
    """JSON safe inside a <script> element: nothing in it can close the element."""
    text = json.dumps(value)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _moment(moment: datetime | date | None) -> str:
    """A moment as the page shows it, and as a grid sorts it: fixed width, UTC."""
    if moment is None:
        return ""
    if isinstance(moment, datetime):
        return f"{moment:%Y-%m-%d %H:%M} UTC"
    return f"{moment:%Y-%m-%d}"


def _when(moment: datetime | date | None) -> str:
    if moment is None:
        return '<span class="faint">not reported</span>'
    return f'<time datetime="{e(moment.isoformat())}">{e(_moment(moment))}</time>'


def _moment_of(moment: datetime, label: str = "") -> str:
    """The kit's om-moment for a moment to read, with what it shows until the
    kit draws it (or where the kit is not served) inside."""
    labelled = f' label="{e(label)}"' if label else ""
    shown = " ".join(filter(None, (label, _moment(moment))))
    return f'<om-moment{labelled} value="{e(moment.isoformat())}">{e(shown)}</om-moment>'


def _document(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{TITLE}</title>"
        f'<link rel="stylesheet" href="{KIT}meridian.css">'
        f'<script src="{KIT}meridian.js"></script>'
        f'</head><body><main class="page">{body}</main></body></html>'
    )


def _badge(state: SyncState) -> str:
    return f'<span class="badge {_STATE_TONE[state]}">{_STATE_LABEL[state]}</span>'


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def _button(
    action: str,
    said: str,
    token: str,
    kind: str = "",
    fields: Mapping[str, str] | None = None,
    header: str = "",
) -> str:
    """A one-button form posting to `action` with the page's token. `header`
    marks it one of the head's header actions (the kit's `data-om-action`):
    where the dashboard frames the page, it draws the button in its own header
    and, when it is pressed there, the kit presses this one, so the form still
    posts from the page with its token."""
    shown = f' class="{kind}"' if kind else ""
    marked = f' data-om-action="{e(header)}"' if header else ""
    carried = "".join(_hidden(name, value) for name, value in (fields or {}).items())
    return (
        f'<form method="post" action="{e(action)}" class="inline">'
        f"{_hidden(CSRF_FIELD, token)}{carried}"
        f"<button{shown}{marked}>{e(said)}</button></form>"
    )


# ── The grids ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    """One column of a grid, as the kit's om-grid takes it in its declared
    JSON and as the plain table shown without the kit draws it. `hint` names
    a field shown under the value, `tone` the field holding its tone (a
    badge's, or on another column its text's), and `blank` is what an empty
    value says."""

    key: str
    label: str
    type: str = "text"
    group: bool = False
    hint: str = ""
    tone: str = ""
    blank: str = ""
    strong: bool = False

    def declared(self) -> dict[str, object]:
        """As om-grid's columns take it, plain JSON: only what is set."""
        column: dict[str, object] = {"key": self.key, "label": self.label, "type": self.type}
        if self.group:
            column["group"] = True
        if self.hint:
            column["hint"] = self.hint
        if self.tone:
            column["tone"] = {"field": self.tone}
        if self.blank:
            column["blank"] = self.blank
        if self.strong:
            column["strong"] = True
        return column


def _cell(column: Column, row: dict[str, str]) -> str:
    """One cell of the plain table, drawn as the kit's grid draws it."""
    value = row.get(column.key, "")
    tone = row.get(column.tone, "") if column.tone else ""
    if not value:
        shown = f'<span class="faint">{e(column.blank)}</span>' if column.blank else ""
    elif column.type == "badge":
        classes = " ".join(filter(None, ("badge", tone if tone in _BADGE_TONES else "")))
        shown = f'<span class="{classes}">{e(value)}</span>'
    elif column.type == "code":
        shown = f"<code>{e(value)}</code>"
    else:
        ink = _INK.get(tone, "")
        shown = f"<strong>{e(value)}</strong>" if column.strong else e(value)
        if ink:
            shown = f'<span class="{ink}">{shown}</span>'
    if column.hint and row.get(column.hint):
        # The space keeps the two apart where the kit is not there to.
        shown += f' <span class="hint">{e(row[column.hint])}</span>'
    numeric = ' class="num"' if column.type == "decimal" else ""
    return f"<td{numeric}>{shown}</td>"


def _grid(
    grid_id: str,
    caption: str,
    empty: str,
    key: str,
    columns: Sequence[Column],
    rows: Sequence[dict[str, str]],
) -> str:
    """An om-grid over `rows`, drawn as cards where it is narrow: its columns
    and rows declared inside it as JSON, which the kit's grid reads, and the
    same table beside them for a browser without the kit."""
    head = "".join(
        f'<th class="num">{e(c.label)}</th>'
        if c.type == "decimal"
        else f"<th>{e(c.label)}</th>"
        for c in columns
    )
    body = (
        "".join("<tr>" + "".join(_cell(c, row) for c in columns) + "</tr>" for row in rows)
        or f'<tr><td colspan="{len(columns)}">{e(empty)}</td></tr>'
    )
    data = {"columns": [c.declared() for c in columns], "rows": list(rows)}
    return (
        f'<om-grid id="{grid_id}" row-key="{key}" narrow="cards" caption="{e(caption)}"'
        f' empty="{e(empty)}">'
        f'<script type="application/json">{_script_json(data)}</script>'
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></om-grid>"
    )


# Most wanted first, so a narrow frame shows it before the table scrolls.
_ACCOUNT_COLUMNS = (
    Column("account", "Account", hint="where", strong=True),
    Column("link", "Link", type="badge", tone="link_tone"),
    Column("state", "Sync state", type="badge", tone="tone", hint="todo"),
    Column("holdings_as_of", "Holdings as of", blank="not reported"),
    Column("history_as_of", "History as of", blank="not reported"),
    Column("recorded", "Last statement", tone="recorded_tone", hint="recorded_note"),
    Column("id", "Account ID", type="code", hint="id_note"),
)

# A statement's rows, on Statements, one grid per account.
_ROW_COLUMNS = (
    Column("instrument", "Instrument", type="code", hint="identifiers"),
    Column("quantity", "Quantity", type="decimal", group=True),
    Column("currency", "Currency"),
    Column("side", "Side"),
    Column("description", "Description", hint="notes"),
    Column("kind", "Kind"),
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


def _account_row(
    connection: ConnectionView, view: AccountView, status: Status, link: LinkView
) -> dict[str, str]:
    account, fresh = view.account, view.freshness
    todo = [fresh.detail] if fresh.detail else []
    if fresh.state is not SyncState.CURRENT:
        todo.append(REMEDY[fresh.state])
    recorded, tone, note = _recorded(view, status)
    return {
        "account": account.name,
        "where": _where(connection, view),
        "id": account.external_account_id,
        "id_note": _unstable(view),
        "link": _LINK_LABEL[link.state],
        "link_tone": _LINK_TONE[link.state],
        "state": _STATE_LABEL[fresh.state],
        "tone": _STATE_TONE[fresh.state],
        "todo": " ".join(todo),
        "holdings_as_of": _moment(fresh.holdings_as_of),
        "history_as_of": _moment(fresh.history_as_of),
        "recorded": recorded,
        "recorded_tone": tone,
        "recorded_note": note,
    }


def _unstable(view: AccountView) -> str:
    if view.account.stable:
        return ""
    return (
        "SnapTrade gives no stable ID for this account: after a reconnect it appears "
        "as a new account, to be linked again."
    )


def _where(connection: ConnectionView, view: AccountView) -> str:
    return " · ".join(
        (
            connection.institution or "Unknown brokerage",
            view.account.account_type or "type not given",
        )
    )


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


def _tile(label: str, value: str, delta: str = "", ink: str = "") -> str:
    said = f'<div class="tile-delta {ink}">{e(delta)}</div>' if delta else ""
    return (
        f'<div class="panel tile"><div class="tile-label">{e(label)}</div>'
        f'<div class="tile-value">{e(value)}</div>{said}</div>'
    )


def _last_read(status: Status) -> str:
    """A tile's figure is short: the time, with the day under it. The moment
    in full is the page head's om-moment."""
    if status.read_at is None:
        return _tile("Last read", "Not yet")
    return _tile("Last read", f"{status.read_at:%H:%M} UTC", f"{status.read_at:%Y-%m-%d}")


def _tiles(status: Status) -> str:
    connections = status.connections
    accounts = status.accounts
    attention = sum(1 for view in accounts if view.freshness.state in _ATTENTION)
    statements = [view.statement for view in accounts if view.statement is not None]
    rows = sum(len(statement.holdings) for statement in statements)
    return (
        '<div class="tiles">'
        + _tile("Connections", str(len(connections)))
        + _tile(
            "Accounts",
            str(len(accounts)),
            f"{attention} {'needs' if attention == 1 else 'need'} attention"
            if attention
            else "none need attention",
            "warn-ink" if attention else "",
        )
        + _tile(
            "Holdings read",
            str(rows),
            f"in {len(statements)} statement{'' if len(statements) == 1 else 's'}",
        )
        + _last_read(status)
        + "</div>"
    )


# ── The account map ──────────────────────────────────────────────────────────
#
# The kit's om-account-map (0.5.0): a dense table of every account the read
# reached, searched, filtered, grouped by connection and paged, one row's
# choices opened at a time, with suggestions where a name or a number matches
# one of the deployment's accounts, from the JSON declared inside it. Its
# forms all post to LINK, saying what they mean in `intent` (`link`, `create`,
# `unlink`, or `link-several` with a pair of IDs for each link), with the
# page's token.
#
# Inside it too, for a browser without the kit, the same as plain forms, in a
# size that holds for thousands of accounts: a list row per account (its link,
# and Unlink on a linked one), then one form to link any account to any open
# account and one to create an account for any, each choosing the account
# from one list rather than a picker on every row.

LINK = f"{ACCOUNTS}/link"
INTENTS = ("link", "create", "unlink")
SEVERAL = "link-several"


def _intent_form(intent: str, external_id: str, token: str, inside: str, kind: str = "") -> str:
    """A plain form for the map's `intent`, posted to LINK with the page's token."""
    shown = f' class="{kind}"' if kind else ""
    return (
        f'<form method="post" action="{LINK}"{shown}>'
        f"{_hidden(CSRF_FIELD, token)}{_hidden('intent', intent)}"
        f"{_hidden('external_account_id', external_id) if external_id else ''}{inside}</form>"
    )


def _external_choice(shown: Sequence[Shown]) -> str:
    """A list of every account the read reached, to choose the one a form is for."""
    options = "".join(
        f'<option value="{e(view.account.external_account_id)}">'
        f"{e(view.account.name)} ({e(_where(connection, view))})</option>"
        for connection, view in shown
    )
    return (
        '<label class="field"><span>The account</span>'
        '<select name="external_account_id" required>'
        f'<option value="">Choose one</option>{options}</select></label>'
    )


def _mapping_row(
    connection: ConnectionView, view: AccountView, link: LinkView, token: str
) -> str:
    """One account, as a list row: shown without the kit."""
    account = view.account
    external_id = account.external_account_id
    note = _unstable(view)
    number = f" · No. {e(account.number)}" if account.number else ""
    head = (
        '<div class="grow">'
        f'<div class="row"><span class="title">{e(account.name)}</span> '
        f'<span class="badge {_LINK_TONE[link.state]}">{_LINK_LABEL[link.state]}</span></div>'
        f'<div class="meta">{e(_where(connection, view))}{number} · '
        f"<code>{e(external_id)}</code></div>"
        + (
            f'<span class="hint">Linked to {e(link.account_name or link.account_id)} '
            f"(<code>{e(link.account_id)}</code>).</span>"
            if link.state is Link.LINKED
            else ""
        )
        + (f'<span class="hint">{e(note)}</span>' if note else "")
        + "</div>"
    )
    unlink = (
        _intent_form(
            "unlink", external_id, token, '<button class="danger">Unlink</button>', "inline"
        )
        if link.state is Link.LINKED
        else ""
    )
    return f'<div class="list-row" data-account="{e(external_id)}">{head}{unlink}</div>'


def _fallback_forms(shown: Sequence[Shown], offered: Offered, token: str) -> str:
    """Without the kit: link any account to any open account, or create one for it."""
    choices = [a for a in offered.accounts if a.open]
    if offered.refused:
        linking = (
            '<p class="hint">The deployment\'s accounts could not be read, so none is '
            "offered here.</p>"
        )
    elif not choices:
        linking = '<p class="hint">The deployment has no open accounts yet: create one.</p>'
    else:
        options = "".join(
            f'<option value="{e(a.account_id)}">{e(a.label())}</option>' for a in choices
        )
        linking = _intent_form(
            "link",
            "",
            token,
            f'<div class="field-row">{_external_choice(shown)}'
            '<label class="field"><span>An existing account</span>'
            '<select name="account_id" required><option value="">Choose an account</option>'
            f"{options}</select></label><button>Link</button></div>",
        )
    creating = _intent_form(
        "create",
        "",
        token,
        f'<div class="field-row">{_external_choice(shown)}'
        '<label class="field"><span>A new account</span>'
        f'<input type="text" name="new_account_name" required maxlength="{_MOST_NAME}"></label>'
        '<label class="field"><span>Custodian</span>'
        f'<input type="text" name="new_account_custodian" maxlength="{_MOST_NAME}"></label>'
        '<label class="field"><span>Type</span>'
        f'<input type="text" name="new_account_type" maxlength="{_MOST_NAME}"></label>'
        '<button class="primary">Create and link</button></div>',
    )
    return (
        '<div class="panel-body panel-section"><h3>Link an account</h3>'
        '<p class="muted">Linking a linked account to another replaces its link.</p>'
        f"{linking}<h3>Create an account for one, and link it</h3>{creating}</div>"
    )


def _map_data(
    status: Status, links: Mapping[str, LinkView], offered: Offered
) -> dict[str, object]:
    """What om-account-map takes: the external accounts (with the number the
    map matches on, and the connection it groups by), the deployment's
    accounts (null where they could not be read), and the links standing."""
    shown = [(c, view) for c in status.connections for view in c.accounts]
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


def _connection_label(connection: ConnectionView) -> str:
    """A connection as the map groups by it: its brokerage, and its own name."""
    brokerage = connection.institution or "Unknown brokerage"
    return f"{brokerage} · {connection.name}" if connection.name else brokerage


def _mapping(
    status: Status, links: Mapping[str, LinkView], offered: Offered, token: str
) -> str:
    empty = "No accounts yet: they appear here once SnapTrade is read."
    shown = [(c, view) for c in status.connections for view in c.accounts]
    fallback = (
        "".join(
            _mapping_row(connection, view, links[view.account.external_account_id], token)
            for connection, view in shown
        )
        + _fallback_forms(shown, offered, token)
        if shown
        else f'<div class="empty-state"><strong>{e(empty)}</strong></div>'
    )
    counted = [links[view.account.external_account_id].state for view in status.accounts]
    summary = ", ".join(
        f"{counted.count(state)} {_LINK_LABEL[state].lower()}"
        for state in Link
        if counted.count(state)
    )
    return (
        '<section class="panel">'
        '<div class="panel-body"><h2>Link each account</h2>'
        '<p class="muted">Each account SnapTrade reaches is recorded against the '
        "deployment's account it is linked to, and one nothing links is not recorded. "
        "Link it to an existing account, or create one for it."
        + (f" {e(summary.capitalize())}." if summary else "")
        + "</p></div>"
        f'<om-account-map action="{LINK}" token-name="{CSRF_FIELD}" token="{e(token)}"'
        f' group-by="connection" link-several empty="{e(empty)}">'
        f'<script type="application/json">{_script_json(_map_data(status, links, offered))}'
        f"</script>{fallback}</om-account-map>"
        "</section>"
    )


# ── The pages ────────────────────────────────────────────────────────────────


def _notices(status: Status, notice: Notice | None, portal: str | None) -> str:
    parts: list[str] = []
    if portal is not None:
        if portal:
            parts.append(
                '<div class="notice info" role="status"><p>SnapTrade\'s Connection Portal '
                "is ready. Sign in to the brokerage there; the new connection appears "
                "here on the next read.</p>"
                f'<p><a class="button primary" href="{e(portal)}" target="_blank" '
                'rel="noopener noreferrer">Open the Connection Portal</a></p></div>'
            )
        else:
            parts.append(
                '<div class="notice info" role="status">Synthetic mode has no Connection '
                "Portal. It opens once the SnapTrade settings are given and synthetic mode "
                "is off.</div>"
            )
    if notice is not None:
        role = "alert" if notice.tone == "bad" else "status"
        listed = "".join(f"<li>{e(item)}</li>" for item in notice.shown)
        folded = "".join(f"<li>{e(item)}</li>" for item in notice.each)
        parts.append(
            f'<div class="notice {e(notice.tone)}" role="{role}">{e(notice.text)}'
            + (f'<ul class="plain">{listed}</ul>' if listed else "")
            + (
                f"<details><summary>Each of the {len(notice.each)}</summary>"
                f'<ul class="plain">{folded}</ul></details>'
                if folded
                else ""
            )
            + "</div>"
        )
    if status.mode == "waiting":
        wanted = ", ".join(label(name) for name in status.missing)
        parts.append(
            '<div class="notice warn">Not reading SnapTrade: this plugin\'s settings need '
            f"{e(wanted)}. Give them in this plugin's settings in the dashboard, or turn "
            "on synthetic mode to try it without a key.</div>"
        )
    if status.error:
        parts.append(f'<div class="notice bad">The last read failed: {e(status.error)}</div>')
    return "".join(parts)


def _head(title: str, status: Status, token: str, back: str, primary: str = "") -> str:
    """A page's heading: what it is, how the plugin is reading, and its actions."""
    mode = {
        "synthetic": "Synthetic mode: built-in responses, not SnapTrade.",
        "snaptrade": "Reading SnapTrade.",
        "waiting": "Waiting for settings.",
    }[status.mode]
    read = f" {_moment_of(status.read_at, 'Last read')}." if status.read_at else ""
    refresh_all = _button(
        "/admin/read", "Refresh", token, fields={"back": back}, header="refresh"
    )
    return (
        '<header class="page-head">'
        f"<div><h1>{e(title)}</h1><p>{e(mode)}{read}</p></div>"
        '<div class="actions">'
        f"{refresh_all}"
        f"{primary}</div></header>"
    )


def _connection(connection: ConnectionView, token: str) -> str:
    key = e(connection.connection_id)
    access = {"read": "read-only", "trade": "trading"}.get(connection.access, connection.access)
    count = len(connection.accounts)
    meta = [connection.name, access, f"{count} account{'' if count == 1 else 's'}"]
    remedy = "" if connection.state is SyncState.CURRENT else REMEDY[connection.state]
    said = " ".join(filter(None, (connection.detail, remedy)))
    # Reconnecting is what a disabled connection asks for, so it leads there.
    reconnect = "primary" if connection.state is SyncState.DISABLED else ""
    # Refresh is offered unless SnapTrade says it serves the connection in real
    # time; where it does not say, a refusal is shown plainly (REFRESH_REFUSED).
    refreshing = {Serving.REAL_TIME: REAL_TIME, Serving.DELAYED: DELAYED}.get(
        connection.serving, ""
    )
    refresh = (
        ""
        if connection.serving is Serving.REAL_TIME
        else _button(f"{CONNECTIONS}/{key}/refresh", "Refresh", token)
    )
    return (
        '<div class="list-row"><div class="grow">'
        f'<div class="row"><span class="title">'
        f"{e(connection.institution or 'Unknown brokerage')}</span> {_badge(connection.state)}"
        "</div>"
        f'<div class="meta">{e(" · ".join(filter(None, meta)))}</div>'
        + (f'<span class="hint">{e(said)}</span>' if said else "")
        + (f'<span class="hint">{e(refreshing)}</span>' if refreshing else "")
        + "</div>"
        f"{refresh}"
        f"{_button(f'{CONNECTIONS}/{key}/reconnect', 'Reconnect', token, reconnect)}"
        "</div>"
    )


def render_connections(
    status: Status, token: str, notice: Notice | None = None, portal: str | None = None
) -> str:
    """The Connections tab: the brokerages connected through SnapTrade, and
    the SnapTrade user they belong to."""
    connections = "".join(_connection(c, token) for c in status.connections) or (
        '<div class="empty-state"><strong>No brokerage connections yet</strong>'
        "<p>Connect one through SnapTrade's Connection Portal to begin.</p></div>"
    )
    users = (
        "".join(
            f"<li><code>{e(user)}</code>"
            + (
                ' <span class="badge accent">this instance</span>'
                if user == status.user_id
                else ""
            )
            + "</li>"
            for user in status.users
        )
        or '<li class="faint">None listed.</li>'
    )
    return _document(
        _head(
            "Brokerage connections",
            status,
            token,
            CONNECTIONS,
            _button("/admin/connect", "Connect a brokerage", token, "primary"),
        )
        + _notices(status, notice, portal)
        + _tiles(status)
        + '<section class="panel">'
        '<div class="panel-body"><h2>Connections</h2>'
        '<p class="muted">Each brokerage connected through SnapTrade, and how it is.</p>'
        f"</div>{connections}</section>"
        '<section class="panel padded">'
        "<h2>SnapTrade user</h2>"
        + (
            "<p>This instance reads the brokerage connections of "
            f"<code>{e(status.user_id)}</code>.</p>"
            if status.user_id
            else '<p class="muted">No SnapTrade user is set, as with a personal key.</p>'
        )
        + f'<p class="hint">Users registered under this key:</p><ul class="plain">{users}</ul>'
        "</section>"
    )


def render_accounts(
    status: Status,
    token: str,
    links: Mapping[str, LinkView],
    offered: Offered,
    notice: Notice | None = None,
) -> str:
    """The Account links tab: each account the connections reach, linked to one of
    the deployment's here, and its sync state. `links` is each account's link
    by its external ID; `offered`, the deployment's accounts read for the
    admin viewing the page."""
    accounts = [
        _account_row(connection, view, status, links[view.account.external_account_id])
        for connection in status.connections
        for view in connection.accounts
    ]
    refused = (
        '<div class="notice bad" role="alert">The deployment\'s accounts could not be '
        f"read: {e(offered.refused)}</div>"
        if offered.refused
        else ""
    )
    return _document(
        _head("Account links", status, token, ACCOUNTS)
        + _notices(status, notice, None)
        + refused
        + _mapping(status, links, offered, token)
        + '<section class="panel">'
        '<div class="panel-body"><h2>Sync state</h2>'
        '<p class="muted">How fresh SnapTrade\'s data about each account is, what to '
        "do about it, and what the last read recorded.</p></div>"
        + _grid("accounts", "Accounts", "No accounts yet.", "id", _ACCOUNT_COLUMNS, accounts)
        + "</section>"
    )


def render_admins_only() -> str:
    """What anybody but a deployment administrator gets on an admin page."""
    return _document(
        '<section class="panel padded narrow">'
        "<h1>This page is for the deployment's administrators</h1>"
        "<p>It sets up how SnapTrade is read. What SnapTrade reads for the accounts you "
        f'may read is on <a href="{STATEMENTS}">Statements</a>.</p>'
        "</section>"
    )


def render_nothing_here() -> str:
    """What somebody with nothing to read gets on Statements."""
    return _document(
        '<section class="panel padded narrow">'
        "<h1>Nothing here for you</h1>"
        "<p>This plugin brings brokerage accounts into this deployment through SnapTrade, "
        "and you may read none of them. If you should, ask whoever administers this "
        "deployment.</p>"
        "</section>"
    )


Shown = tuple[ConnectionView, AccountView]


def visible(
    status: Status, links: Mapping[str, LinkView], caller: meridian.Caller
) -> list[Shown]:
    """The accounts Statements shows this caller: every one to a deployment
    administrator, and to anybody else each one linked to an account they may
    read."""
    everyone = is_administrator(caller)
    return [
        (connection, view)
        for connection in status.connections
        for view in connection.accounts
        if everyone or _readable(links.get(view.account.external_account_id), caller)
    ]


def _readable(link: LinkView | None, caller: meridian.Caller) -> bool:
    return (
        link is not None
        and link.state is Link.LINKED
        and bool(link.account_id)
        and caller.may_read(link.account_id)
    )


def _statement_tiles(status: Status, shown: Sequence[Shown]) -> str:
    attention = sum(1 for _, view in shown if view.freshness.state in _ATTENTION)
    statements = [view.statement for _, view in shown if view.statement is not None]
    rows = sum(len(statement.holdings) for statement in statements)
    brokerages = {connection.institution or connection.connection_id for connection, _ in shown}
    return (
        '<div class="tiles">'
        + _tile(
            "Accounts",
            str(len(shown)),
            f"{attention} not up to date" if attention else "all up to date",
            "warn-ink" if attention else "",
        )
        + _tile("Brokerages", str(len(brokerages)))
        + _tile(
            "Rows",
            str(rows),
            f"in {len(statements)} statement{'' if len(statements) == 1 else 's'}",
        )
        + _last_read(status)
        + "</div>"
    )


def _statement(
    index: int, connection: ConnectionView, view: AccountView, status: Status
) -> str:
    """One account on Statements: its sync state, its last statement and its rows."""
    account, fresh, statement = view.account, view.freshness, view.statement
    recorded, tone, note = _recorded(view, status)
    ink = _INK.get(tone, "")
    said_recorded = f'<span class="{ink}">{e(recorded)}</span>' if ink else e(recorded)
    last = (
        f"Last statement as of {e(statement.as_of_date)}: {said_recorded}"
        if statement is not None
        else f"No statement on the last read: {e(recorded.lower())}"
    )
    said = [fresh.detail] if fresh.detail else []
    if note and statement is not None:
        said.append(f"{note[0].upper()}{note[1:]}.")
    rows = [_holding_row(view, holding) for holding in statement.holdings] if statement else []
    empty = (view.withheld or "Nothing was read.") if statement is None else "No rows."
    return (
        f'<section class="panel" data-account="{e(account.external_account_id)}">'
        '<div class="list-row"><div class="grow">'
        f'<div class="row"><span class="title">{e(account.name)}</span> '
        f"{_badge(fresh.state)}</div>"
        f'<div class="meta">{e(_where(connection, view))}</div>'
        f'<div class="meta">Holdings as of {_when(fresh.holdings_as_of)} · '
        f"history as of {_when(fresh.history_as_of)}</div>"
        f'<div class="meta">{last}</div>'
        + "".join(f'<span class="hint">{e(line)}</span>' for line in said)
        + "</div></div>"
        + _grid(f"rows-{index}", f"{account.name}: rows", empty, "key", _ROW_COLUMNS, rows)
        + "</section>"
    )


def render_statements(status: Status, shown: Sequence[Shown], everyone: bool) -> str:
    """Statements, the user side: for each account in `shown`, its sync
    state, its last statement and its rows, as the last read found them.
    `everyone` says the viewer is a deployment administrator, shown every
    account."""
    whose = (
        "every account SnapTrade reaches; as a deployment administrator you see them all"
        if everyone
        else "each account you may read"
    )
    mode = {
        "synthetic": " Synthetic mode: every figure here is invented, not read from SnapTrade.",
        "snaptrade": "",
        "waiting": " SnapTrade is not being read yet.",
    }[status.mode]
    read = f" {_moment_of(status.read_at, 'Last read')}." if status.read_at else ""
    failed = (
        '<div class="notice warn" role="status">The last read of SnapTrade failed, so '
        "nothing is shown from it.</div>"
        if status.error
        else ""
    )
    accounts = "".join(
        _statement(index, connection, view, status)
        for index, (connection, view) in enumerate(shown)
    ) or (
        '<section class="panel"><div class="empty-state"><strong>No statements yet</strong>'
        + (
            "<p>Accounts appear here once SnapTrade is read.</p>"
            if everyone
            else "<p>None of the accounts you may read has come through SnapTrade here yet. "
            "An account appears once it is linked to yours.</p>"
        )
        + "</div></section>"
    )
    return _document(
        '<header class="page-head"><div><h1>Statements</h1>'
        f"<p>What the last read of SnapTrade found in {e(whose)}. Quantities are exact, "
        f"as SnapTrade reported them.{e(mode)}{read}</p></div></header>"
        + failed
        + (_statement_tiles(status, shown) if shown else "")
        + accounts
    )


# ── The server ───────────────────────────────────────────────────────────────


def _field(form: Mapping[str, list[str]], name: str) -> str:
    """A form's one value for `name`, or "" where it has none or several."""
    values = form.get(name, [])
    return values[0].strip() if len(values) == 1 else ""


def serve(
    syncer: Syncer,
    links: Links,
    loop: asyncio.AbstractEventLoop,
    port: int,
    wake: asyncio.Event,
    tokens: CsrfTokens | None = None,
) -> http.server.ThreadingHTTPServer:
    """Start the pages on 127.0.0.1:`port`, in a thread; the returned server's
    `shutdown()` stops it. SnapTrade and the sidecar are asked on `loop`, where
    the plugin lives."""
    csrf = tokens if tokens is not None else CsrfTokens()

    def on_loop(work: Coroutine[Any, Any, _Answer], timeout: float = 30) -> _Answer:
        return asyncio.run_coroutine_threadsafe(work, loop).result(timeout=timeout)

    def links_of(status: Status) -> dict[str, LinkView]:
        return {
            view.account.external_account_id: links.of(view.account.external_account_id)
            for view in status.accounts
        }

    def offered_to(caller: meridian.Caller) -> Offered:
        try:
            return on_loop(links.offered(caller.header))
        except Exception as failed:
            # Named by its type only: its text is not known to be safe to show.
            return Offered(refused=f"that failed: {type(failed).__name__}")

    def render(
        path: str,
        caller: meridian.Caller,
        notice: Notice | None = None,
        portal: str | None = None,
    ) -> str:
        status, token = syncer.status, csrf.token(caller)
        if path == ACCOUNTS:
            return render_accounts(status, token, links_of(status), offered_to(caller), notice)
        return render_connections(status, token, notice, portal)

    def statements(caller: meridian.Caller) -> str:
        status = syncer.status
        everyone = is_administrator(caller)
        if not everyone and not caller.read:
            return render_nothing_here()
        return render_statements(status, visible(status, links_of(status), caller), everyone)

    class Page(http.server.BaseHTTPRequestHandler):
        def _caller(self) -> meridian.Caller | None:
            presented = self.headers.get_all("Meridian-Caller") or []
            if len(presented) != 1:
                self._send(401, "Open this page from the dashboard.", "text/plain")
                return None
            try:
                return meridian.Caller.from_header(presented[0])
            except Exception:
                self._send(400, "The caller could not be read.", "text/plain")
                return None

        def _administrator(self) -> meridian.Caller | None:
            caller = self._caller()
            if caller is None:
                return None
            if not is_administrator(caller):
                self._send(403, render_admins_only())
                return None
            return caller

        def _send(self, status: int, body: str | bytes, kind: str = "text/html") -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where: str) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - the server's name
            path, _, query = self.path.partition("?")
            # The frame's theme rides the query on first load; it goes along.
            connections = CONNECTIONS + (f"?{query}" if query else "")
            if path == STATEMENTS:
                caller = self._caller()
                if caller is not None:
                    self._send(200, statements(caller))
                return
            if path in ("/admin", CONNECTIONS, ACCOUNTS):
                caller = self._administrator()
                if caller is None:
                    return
                if path == "/admin":
                    self._redirect(connections)
                else:
                    self._send(200, render(path, caller))
                return
            self._send(404, "No such page.", "text/plain")

        def _form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length") or 0)
            most = _MOST_LINKS_BODY if self.path.split("?", 1)[0] == LINK else _MOST_BODY
            if length > most:
                return {}
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            return parse_qs(body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                form = self._form()
            except ValueError:
                form = {}
            caller = self._administrator()
            if caller is None:
                return
            if not csrf.valid(caller, _field(form, CSRF_FIELD)):
                # Refused before anything is asked of SnapTrade or the sidecar.
                self._send(
                    403,
                    "This form has expired or did not come from this page. "
                    "Reload the page and try again.",
                    "text/plain",
                )
                return
            path = self.path.split("?", 1)[0]
            if path == LINK:
                self._link(caller, form)
                return
            parts = path.strip("/").split("/")
            venue = syncer.venue
            serving = {c.connection_id: c.serving for c in syncer.status.connections}
            notice, portal, answer = None, None, CONNECTIONS
            try:
                if parts == ["admin", "read"]:
                    loop.call_soon_threadsafe(wake.set)
                    notice = Notice("Reading SnapTrade now. Reload in a moment.")
                    back = _field(form, "back")
                    answer = back if back in (CONNECTIONS, ACCOUNTS) else CONNECTIONS
                elif venue is None:
                    notice = Notice("Nothing to ask SnapTrade until its settings are given.")
                elif parts == ["admin", "connect"]:
                    portal = on_loop(venue.connection_portal())
                elif (
                    len(parts) == 4
                    and parts[:2] == ["admin", "connections"]
                    and parts[2] in serving
                    and parts[3] in ("refresh", "reconnect")
                ):
                    if parts[3] == "refresh" and serving[parts[2]] is Serving.REAL_TIME:
                        # Offered no Refresh; a form from an older page is
                        # answered without asking SnapTrade.
                        notice = Notice(REAL_TIME)
                    elif parts[3] == "refresh":
                        try:
                            notice = Notice(on_loop(venue.refresh(parts[2])))
                        except VenueError as failed:
                            # SnapTrade's refusal (Real-time plans refuse a
                            # refresh), said plainly; any other failure is
                            # shown as every other is, below.
                            if failed.status != 403:
                                raise
                            notice = Notice(REFRESH_REFUSED)
                        else:
                            loop.call_soon_threadsafe(wake.set)
                    else:
                        portal = on_loop(venue.connection_portal(reconnect=parts[2]))
                else:
                    self._send(404, "No such action.", "text/plain")
                    return
            except VenueError as failed:
                notice = Notice(str(failed), "bad")
            except Exception as failed:
                # Named by its type only: its text is not known to be safe to show.
                notice = Notice(f"That failed: {type(failed).__name__}", "bad")
            self._send(200, render(answer, caller, notice, portal))

        def _link(self, caller: meridian.Caller, form: Mapping[str, list[str]]) -> None:
            """Link, create and link, or unlink one of the accounts the last
            read reached, as the form's `intent` says, for the admin who sent
            it (W6.4). A link to another account replaces the one standing."""
            intent = _field(form, "intent")
            if intent == SEVERAL:
                self._link_several(caller, form)
                return
            external_id = _field(form, "external_account_id")
            views = {view.account.external_account_id: view for view in syncer.status.accounts}
            if intent not in INTENTS:
                self._send(400, "This form does not say what to do.", "text/plain")
                return
            if external_id not in views:
                self._send(404, "No such account.", "text/plain")
                return
            named = views[external_id].account.name or external_id
            account_id = _field(form, "account_id") if intent == "link" else ""
            name = _field(form, "new_account_name")[:_MOST_NAME] if intent == "create" else ""
            # Where it is held and what it is, as the admin left the venue's
            # words; longer than the deployment keeps is refused by it, by name.
            custodian = _field(form, "new_account_custodian") if intent == "create" else ""
            account_type = _field(form, "new_account_type") if intent == "create" else ""
            if intent == "link" and not account_id:
                notice = Notice("Choose the account to link it to.", "warn")
            elif intent == "create" and not name:
                notice = Notice("Name the new account.", "warn")
            else:
                try:
                    sent = links.link(
                        caller.header, external_id, account_id, name, custodian, account_type
                    )
                    on_loop(sent)
                except meridian.MeridianError as refused:
                    notice = Notice(f"The sidecar refused this: {refusal(refused)}", "bad")
                except Exception as failed:
                    notice = Notice(f"That failed: {type(failed).__name__}", "bad")
                else:
                    # Read again, so its rows follow the link.
                    loop.call_soon_threadsafe(wake.set)
                    now = links.of(external_id)
                    to = now.account_name if now.account_id == account_id else ""
                    notice = Notice(
                        f"Unlinked {named}."
                        if intent == "unlink"
                        else f"Created {name} and linked {named} to it."
                        if intent == "create"
                        else f"Linked {named} to {to or account_id}.",
                        "good",
                    )
            self._send(200, render(ACCOUNTS, caller, notice))

        def _link_several(self, caller: meridian.Caller, form: Mapping[str, list[str]]) -> None:
            """Link several accounts, each to the account paired with it: the
            map's `link-several` form, one `external_account_id` and one
            `account_id` for each link, in order, under the one token already
            checked. Each is its own link, and the page answering says how each
            went. A form whose IDs do not pair up is refused whole."""
            externals = [value.strip() for value in form.get("external_account_id", [])]
            accounts = [value.strip() for value in form.get("account_id", [])]
            if (
                not externals
                or len(externals) != len(accounts)
                or len(set(externals)) != len(externals)
                or not all(externals)
                or not all(accounts)
            ):
                self._send(
                    400,
                    "This form's accounts do not pair up: one account to link, and one "
                    "to link it to, for each link.",
                    "text/plain",
                )
                return
            views = {view.account.external_account_id: view for view in syncer.status.accounts}
            reached = [(x, a) for x, a in zip(externals, accounts, strict=True) if x in views]
            # Thirty seconds, and a second more for each round of links sent at once.
            waited = 30 + len(reached) / LINKS_AT_ONCE
            try:
                said = (
                    on_loop(links.link_several(caller.header, reached), waited)
                    if reached
                    else []
                )
            except concurrent.futures.TimeoutError:
                # Still going: which went through is for the account scope to say.
                loop.call_soon_threadsafe(wake.set)
                notice = Notice(
                    f"Sent {len(reached)} links; not every one was answered within "
                    f"{round(waited)} seconds. Reload in a moment to see which are linked.",
                    "warn",
                )
                self._send(200, render(ACCOUNTS, caller, notice))
                return
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
                    now = links.of(external_id)
                    to = now.account_name if now.account_id == account_id else ""
                    each.append(f"Linked {named} to {to or account_id}.")
            if linked:
                # Read again, so their rows follow the links.
                loop.call_soon_threadsafe(wake.set)
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
            self._send(200, render(ACCOUNTS, caller, notice))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
