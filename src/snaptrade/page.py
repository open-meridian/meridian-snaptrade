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
  and reconnecting or refreshing a connection. They are served to a caller
  whose verified `deployment_admin` claim is true, and to nobody else;
  `/admin` sends a caller to Connections. The SnapTrade keys are entered in
  the dashboard's settings form, never here.

Which of the deployment's accounts an external account is linked to is what
cuts Statements to a reader, and the contract gives this plugin no read of
its links (sdk-contract/a-plugin-reads-its-own-links). So a reader sees an
account whose link this plugin can name: one made on the Account links tab
since it started. One it cannot name is shown to deployment admins only,
never guessed at. Nothing is stored for it: plugins are ephemeral.

Built on the kit (spec/plugin-pages-share-one-kit, requirement 5): the
dashboard serves Open Meridian's UI kit at /.meridian/ui/<version>/ on this
plugin's own host, and each page links its stylesheet and script, uses its
classes and its grid, and has no style, colour or theme code of its own.

Where the kit is not served a page still works, unstyled: each table is in
the HTML inside its <om-grid>, which a browser shows as it is until the kit's
grid replaces it; the grid's columns and rows sit beside it as JSON, read only
once the grid is defined; and every action is a plain form.

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
import hashlib
import hmac
import html
import http.server
import json
import secrets
import threading
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, TypeVar
from urllib.parse import parse_qs

import meridian

from .linking import Link, Links, LinkView, Offered, refusal
from .normalise import REMEDY, AccountView, ConnectionView, Holding, SyncState
from .settings import label
from .sync import Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
# The kit's version these pages were built against. The dashboard serves the
# deployment's; pinning one keeps the pages as they were built.
KIT = "/.meridian/ui/0.1.0/"
CSRF_FIELD = "csrf"
# A form here carries a token, an account, and a new account's name, custodian
# and type at most; anything longer is not one of this page's.
_MOST_BODY = 8192
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
_LINK_LABEL = {Link.LINKED: "Linked", Link.UNLINKED: "Not linked", Link.UNKNOWN: "Not known"}
_LINK_TONE = {Link.LINKED: "good", Link.UNLINKED: "warn", Link.UNKNOWN: ""}


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


def _document(body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{TITLE}</title>"
        f'<link rel="stylesheet" href="{KIT}meridian.css">'
        f'<script src="{KIT}meridian.js"></script>'
        f'</head><body><main class="page">{body}</main>'
        + (f'<script type="module">{script}</script>' if script else "")
        + "</body></html>"
    )


def _badge(state: SyncState) -> str:
    return f'<span class="badge {_STATE_TONE[state]}">{_STATE_LABEL[state]}</span>'


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def _button(
    action: str, said: str, token: str, kind: str = "", fields: Mapping[str, str] | None = None
) -> str:
    shown = f' class="{kind}"' if kind else ""
    carried = "".join(_hidden(name, value) for name, value in (fields or {}).items())
    return (
        f'<form method="post" action="{e(action)}" class="inline">'
        f"{_hidden(CSRF_FIELD, token)}{carried}"
        f"<button{shown}>{e(said)}</button></form>"
    )


# ── The grids ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    """One column of a grid, as the kit's om-grid takes it and as the plain
    table shown without the kit draws it. `hint` names a field shown
    under the value, `tone` the field holding a badge's tone, `ink` the field
    holding a text class, and `blank` is what an empty value says."""

    key: str
    label: str
    type: str = "text"
    group: bool = False
    hint: str = ""
    tone: str = ""
    ink: str = ""
    blank: str = ""
    strong: bool = False


def _cell(column: Column, row: dict[str, str]) -> str:
    value = row.get(column.key, "")
    if not value:
        shown = f'<span class="faint">{e(column.blank)}</span>' if column.blank else ""
    elif column.tone:
        shown = f'<span class="badge {e(row.get(column.tone, ""))}">{e(value)}</span>'
    elif column.type == "code":
        shown = f"<code>{e(value)}</code>"
    elif column.strong:
        shown = f"<strong>{e(value)}</strong>"
    elif column.ink and row.get(column.ink):
        shown = f'<span class="{e(row[column.ink])}">{e(value)}</span>'
    else:
        shown = e(value)
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
    """An om-grid over `rows`, with the same table inside it for a browser
    without the kit, and its columns and rows beside it for the kit's."""
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
    data = {"columns": [asdict(c) for c in columns], "rows": list(rows)}
    return (
        f'<om-grid id="{grid_id}" row-key="{key}" caption="{e(caption)}" empty="{e(empty)}"'
        f' data-grid="{grid_id}-data">'
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></om-grid>"
        f'<script type="application/json" id="{grid_id}-data">{_script_json(data)}</script>'
    )


# Set on each grid once the kit has defined om-grid; without the kit it never
# is, and the tables stay. A column's hint, tone, ink and blank become a
# `format` drawing what the table above draws; nothing here is a colour.
_GRIDS = """
const shown = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
};
const column = (c) => {
  const plain = { key: c.key, label: c.label, type: c.type, group: c.group };
  if (!c.hint && !c.tone && !c.ink && !c.blank && !c.strong) return plain;
  return {
    ...plain,
    type: c.type === "code" ? "text" : c.type,
    format(value, row) {
      const cell = document.createElement("span");
      if (value === undefined || value === null || value === "") {
        if (c.blank) cell.append(shown("span", "faint", c.blank));
      } else if (c.tone) cell.append(shown("span", `badge ${row[c.tone] || ""}`, value));
      else if (c.type === "code") cell.append(shown("code", "", value));
      else if (c.strong) cell.append(shown("strong", "", value));
      else cell.append(shown("span", (c.ink && row[c.ink]) || "", value));
      if (c.hint && row[c.hint]) cell.append(shown("span", "hint", row[c.hint]));
      return cell;
    },
  };
};
customElements.whenDefined("om-grid").then(() => {
  for (const grid of document.querySelectorAll("om-grid[data-grid]")) {
    const data = JSON.parse(document.getElementById(grid.dataset.grid).textContent);
    grid.columns = data.columns.map(column);
    grid.setRows(data.rows);
  }
});
"""

# Most wanted first, so a narrow frame shows it before the table scrolls.
_ACCOUNT_COLUMNS = (
    Column("account", "Account", hint="where", strong=True),
    Column("link", "Link", tone="link_tone"),
    Column("state", "Sync state", tone="tone", hint="todo"),
    Column("holdings_as_of", "Holdings as of", blank="not reported"),
    Column("history_as_of", "History as of", blank="not reported"),
    Column("recorded", "Last statement", ink="recorded_ink", hint="recorded_note"),
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
    """What the last statement came to: its text, a text class, and a note."""
    outcome = status.outcomes.get(view.account.external_account_id)
    if view.statement is None:
        return "Nothing recorded", "", view.withheld
    if outcome is None:
        return "Not yet recorded", "", ""
    if outcome.stopped:
        return (
            f"Stopped at {outcome.recorded} of {outcome.rows} rows",
            "bad-ink",
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
    recorded, ink, note = _recorded(view, status)
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
        "recorded_ink": ink,
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
        + _tile(
            "Last read",
            f"{status.read_at:%H:%M} UTC" if status.read_at else "Not yet",
            f"{status.read_at:%Y-%m-%d}" if status.read_at else "",
        )
        + "</div>"
    )


# ── The account mapping ──────────────────────────────────────────────────────
#
# Each external account with its link and what can be done about it, drawn
# with the kit's classes: a list row per account, a badge for its link, and
# for one not linked, two plain forms side by side (the kit's grid-2, one
# column on a phone): an existing account from a picker, or a new account
# named from the external one. The kit has no component for this yet.


def _picker(external_id: str, offered: Offered, token: str) -> str:
    """Link to one of the deployment's open accounts."""
    choices = [account for account in offered.accounts if account.open]
    if offered.refused:
        return (
            '<p class="hint">The deployment\'s accounts could not be read, so none is '
            "offered here.</p>"
        )
    if not choices:
        return '<p class="hint">The deployment has no open accounts yet: create one.</p>'
    options = "".join(
        f'<option value="{e(account.account_id)}">{e(account.label())}</option>'
        for account in choices
    )
    return (
        f'<form method="post" action="{ACCOUNTS}/link">'
        f"{_hidden(CSRF_FIELD, token)}{_hidden('external_account_id', external_id)}"
        '<label class="field"><span>Link to an existing account</span>'
        '<select name="account_id" required><option value="">Choose an account</option>'
        f"{options}</select></label>"
        # In a field of its own, so a stacked form below it keeps its distance.
        '<div class="field"><button>Link</button></div></form>'
    )


def _create(connection: ConnectionView, view: AccountView, token: str) -> str:
    """Create a new account, named from the external one, held at the
    connection's brokerage and of the venue's account type, each editable,
    and link it."""
    return (
        f'<form method="post" action="{ACCOUNTS}/create">'
        f"{_hidden(CSRF_FIELD, token)}"
        f"{_hidden('external_account_id', view.account.external_account_id)}"
        '<label class="field"><span>Create a new account</span>'
        f'<input type="text" name="new_account_name" value="{e(view.account.name)}" '
        f'required maxlength="{_MOST_NAME}"></label>'
        '<label class="field"><span>Custodian</span>'
        f'<input type="text" name="new_account_custodian" value="{e(connection.institution)}" '
        f'maxlength="{_MOST_NAME}"></label>'
        '<label class="field"><span>Type</span>'
        f'<input type="text" name="new_account_type" value="{e(view.account.account_type)}" '
        f'maxlength="{_MOST_NAME}"></label>'
        '<div class="field"><button class="primary">Create and link</button></div></form>'
    )


def _mapping_row(
    connection: ConnectionView, view: AccountView, link: LinkView, offered: Offered, token: str
) -> str:
    account = view.account
    external_id = account.external_account_id
    said = [link.how]
    if link.account_id:
        said.insert(0, f"Linked to {offered.name_of(link.account_id) or link.account_id}.")
    note = _unstable(view)
    head = (
        '<div class="grow">'
        f'<div class="row"><span class="title">{e(account.name)}</span> '
        f'<span class="{" ".join(filter(None, ("badge", _LINK_TONE[link.state])))}">'
        f"{_LINK_LABEL[link.state]}</span></div>"
        f'<div class="meta">{e(_where(connection, view))} · '
        f"<code>{e(external_id)}</code></div>"
        f'<span class="hint">{e(" ".join(said))}</span>'
        + (f'<span class="hint">{e(note)}</span>' if note else "")
    )
    unlink = _button(
        f"{ACCOUNTS}/unlink",
        "Unlink",
        token,
        "danger",
        {"external_account_id": external_id},
    )
    if link.state is Link.LINKED:
        return (
            f'<div class="list-row" data-account="{e(external_id)}">{head}</div>{unlink}</div>'
        )
    forms = (
        f'<div class="grid-2">{_picker(external_id, offered, token)}'
        f"{_create(connection, view, token)}</div>"
    )
    # Where it is not known, it may be linked: removing the link is offered
    # too, under the forms, so a phone's width is left to them.
    also = (
        f'<div class="row"><span class="muted">It may be linked already.</span>{unlink}</div>'
        if link.state is Link.UNKNOWN
        else ""
    )
    return (
        f'<div class="list-row" data-account="{e(external_id)}">{head}{forms}{also}</div></div>'
    )


def _mapping(
    status: Status, links: Mapping[str, LinkView], offered: Offered, token: str
) -> str:
    rows = [
        _mapping_row(connection, view, links[view.account.external_account_id], offered, token)
        for connection in status.connections
        for view in connection.accounts
    ]
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
        + (
            "".join(rows)
            or '<div class="empty-state"><strong>No accounts yet</strong>'
            "<p>They appear here once SnapTrade is read.</p></div>"
        )
        + "</section>"
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
        parts.append(
            f'<div class="notice {e(notice.tone)}" role="{role}">{e(notice.text)}</div>'
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
    read = f" Last read {_when(status.read_at)}." if status.read_at else ""
    return (
        '<header class="page-head">'
        f"<div><h1>{e(title)}</h1><p>{e(mode)}{read}</p></div>"
        '<div class="actions">'
        f"{_button('/admin/read', 'Read now', token, fields={'back': back})}"
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
    return (
        '<div class="list-row"><div class="grow">'
        f'<div class="row"><span class="title">'
        f"{e(connection.institution or 'Unknown brokerage')}</span> {_badge(connection.state)}"
        "</div>"
        f'<div class="meta">{e(" · ".join(filter(None, meta)))}</div>'
        + (f'<span class="hint">{e(said)}</span>' if said else "")
        + "</div>"
        f"{_button(f'{CONNECTIONS}/{key}/refresh', 'Refresh', token)}"
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
        + "</section>",
        _GRIDS,
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
    administrator, and to anybody else each one whose link this plugin can
    name, to an account they may read. One whose link it cannot name is not
    guessed at."""
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
        + _tile(
            "Last read",
            f"{status.read_at:%H:%M} UTC" if status.read_at else "Not yet",
            f"{status.read_at:%Y-%m-%d}" if status.read_at else "",
        )
        + "</div>"
    )


def _statement(
    index: int, connection: ConnectionView, view: AccountView, status: Status
) -> str:
    """One account on Statements: its sync state, its last statement and its rows."""
    account, fresh, statement = view.account, view.freshness, view.statement
    recorded, ink, note = _recorded(view, status)
    said_recorded = f'<span class="{e(ink)}">{e(recorded)}</span>' if ink else e(recorded)
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
    read = f" Last read {_when(status.read_at)}." if status.read_at else ""
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
        + accounts,
        _GRIDS if shown else "",
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

    def on_loop(work: Coroutine[Any, Any, _Answer]) -> _Answer:
        return asyncio.run_coroutine_threadsafe(work, loop).result(timeout=30)

    def links_of(status: Status) -> dict[str, LinkView]:
        return {
            view.account.external_account_id: links.of(
                view, status.outcomes.get(view.account.external_account_id)
            )
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
            if length > _MOST_BODY:
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
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if parts[:2] == ["admin", "accounts"]:
                self._link(caller, parts[2:], form)
                return
            venue = syncer.venue
            known = {c.connection_id for c in syncer.status.connections}
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
                    and parts[2] in known
                    and parts[3] in ("refresh", "reconnect")
                ):
                    if parts[3] == "refresh":
                        notice = Notice(on_loop(venue.refresh(parts[2])))
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

        def _link(
            self, caller: meridian.Caller, action: list[str], form: Mapping[str, list[str]]
        ) -> None:
            """Link, create and link, or unlink one of the accounts the last
            read reached, for the admin who sent the form (W6.4)."""
            external_id = _field(form, "external_account_id")
            views = {view.account.external_account_id: view for view in syncer.status.accounts}
            if action not in (["link"], ["create"], ["unlink"]) or external_id not in views:
                self._send(404, "No such account.", "text/plain")
                return
            named = views[external_id].account.name or external_id
            account_id = _field(form, "account_id") if action == ["link"] else ""
            name = _field(form, "new_account_name")[:_MOST_NAME] if action == ["create"] else ""
            # Where it is held and what it is, as the admin left the venue's
            # words; longer than the deployment keeps is refused by it, by name.
            custodian = _field(form, "new_account_custodian") if action == ["create"] else ""
            account_type = _field(form, "new_account_type") if action == ["create"] else ""
            if action == ["link"] and not account_id:
                notice = Notice("Choose the account to link it to.", "warn")
            elif action == ["create"] and not name:
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
                    notice = Notice(
                        f"Unlinked {named}."
                        if action == ["unlink"]
                        else f"Created {name} and linked {named} to it."
                        if action == ["create"]
                        else f"Linked {named}.",
                        "good",
                    )
            self._send(200, render(ACCOUNTS, caller, notice))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
