"""The plugin's admin portal, and nothing for anybody else.

Ruled by the product owner on 2026-09-28 (spec/plugin-pages-share-one-kit,
Q5, and the amendment the same day): this plugin has an admin portal and no
user portal. The admin portal covers what only SnapTrade knows: its users
under the key, the brokerage connections, connecting one through SnapTrade's
Connection Portal, each connection's health and each account's sync state with
what to do, what the last read found, and reconnecting or refreshing a
connection. The SnapTrade keys are entered in the dashboard's settings form,
never here; linking accounts and granting access are the dashboard's too.
People see accounts and holdings through a reporting plugin, not through
custody.

`/` sends a deployment administrator to `/admin` and tells anybody else there
is no page for them. `/admin` is served to deployment administrators only,
decided from the caller the sidecar verified (contract.is_administrator).

Built on the kit (spec/plugin-pages-share-one-kit, requirement 5): the
dashboard serves Open Meridian's UI kit at /.meridian/ui/<version>/ on this
plugin's own host, and the page links its stylesheet and script, uses its
classes and its grid, and has no style, colour or theme code of its own. The
dashboard's frame draws the plugin's name, the way back and the person; the
page draws only its content.

Where the kit is not served the page still works, unstyled: each table is in
the HTML inside its <om-grid>, which a browser shows as it is until the kit's
grid replaces it; the grid's columns and rows sit beside it as JSON, read only
once the grid is defined; and every action is a plain form.

Every action is a POST carrying a CSRF token in its form, checked before
anything is done. The plugin host has its own session cookie (decisions/021),
so without it a page elsewhere could have an administrator's browser post
here. The token is an HMAC of the verified caller's subject under a secret
this process makes at start: nobody else can make it, it is the same across
the per-request assertions the dashboard mints for one person, and a restart
makes every page open before it stale, which is only a reload.

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
from collections.abc import Coroutine, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs

import meridian

from .contract import is_administrator
from .normalise import REMEDY, AccountView, ConnectionView, Holding, SyncState
from .settings import CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET
from .sync import Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
# The kit's version this page was built against. The dashboard serves the
# deployment's; pinning one keeps the page as it was built.
KIT = "/.meridian/ui/0.1.0/"
CSRF_FIELD = "csrf"
# A form here carries a token and nothing else; anything longer is not one.
_MOST_BODY = 4096


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
_SETTING_LABEL = {
    CLIENT_ID: "SnapTrade client ID",
    CONSUMER_KEY: "SnapTrade consumer key",
    USER_ID: "SnapTrade user ID",
    USER_SECRET: "SnapTrade user secret",
}


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


def _button(action: str, label: str, token: str, kind: str = "") -> str:
    shown = f' class="{kind}"' if kind else ""
    return (
        f'<form method="post" action="{e(action)}" class="inline">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{e(token)}">'
        f"<button{shown}>{e(label)}</button></form>"
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
    Column("state", "Sync state", tone="tone", hint="todo"),
    Column("holdings_as_of", "Holdings as of", blank="not reported"),
    Column("history_as_of", "History as of", blank="not reported"),
    Column("recorded", "Last statement", ink="recorded_ink", hint="recorded_note"),
    Column("id", "Account ID", type="code", hint="id_note"),
)

_HOLDING_COLUMNS = (
    Column("instrument", "Instrument", type="code", hint="identifiers"),
    Column("quantity", "Quantity", type="decimal", group=True),
    Column("currency", "Currency"),
    Column("side", "Side"),
    Column("description", "Description", hint="notes"),
    Column("kind", "Kind"),
    Column("account", "Account"),
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
    connection: ConnectionView, view: AccountView, status: Status
) -> dict[str, str]:
    account, fresh = view.account, view.freshness
    todo = [fresh.detail] if fresh.detail else []
    if fresh.state is not SyncState.CURRENT:
        todo.append(REMEDY[fresh.state])
    recorded, ink, note = _recorded(view, status)
    return {
        "account": account.name,
        "where": " · ".join(
            (
                connection.institution or "Unknown brokerage",
                account.account_type or "type not given",
            )
        ),
        "id": account.external_account_id,
        "id_note": ""
        if account.stable
        else (
            "SnapTrade gives no stable ID for this account: after a reconnect it "
            "appears as a new account, to be linked again."
        ),
        "state": _STATE_LABEL[fresh.state],
        "tone": _STATE_TONE[fresh.state],
        "todo": " ".join(todo),
        "holdings_as_of": _moment(fresh.holdings_as_of),
        "history_as_of": _moment(fresh.history_as_of),
        "recorded": recorded,
        "recorded_ink": ink,
        "recorded_note": note,
    }


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
        "account": view.account.name,
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


# ── The pages ────────────────────────────────────────────────────────────────


def _notices(status: Status, notice: str, portal: str | None) -> str:
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
    if notice:
        parts.append(f'<div class="notice info" role="status">{e(notice)}</div>')
    if status.mode == "waiting":
        wanted = ", ".join(_SETTING_LABEL.get(name, name) for name in status.missing)
        parts.append(
            '<div class="notice warn">Not reading SnapTrade: this plugin\'s settings need '
            f"{e(wanted)}. Give them in this plugin's settings in the dashboard, or turn "
            "on synthetic mode to try it without a key.</div>"
        )
    if status.error:
        parts.append(f'<div class="notice bad">The last read failed: {e(status.error)}</div>')
    return "".join(parts)


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
        f"{_button(f'/admin/connections/{key}/refresh', 'Refresh', token)}"
        f"{_button(f'/admin/connections/{key}/reconnect', 'Reconnect', token, reconnect)}"
        "</div>"
    )


def render_admin(
    status: Status, token: str, notice: str = "", portal: str | None = None
) -> str:
    """The admin portal for the last read, its forms carrying `token`."""
    mode = {
        "synthetic": "Synthetic mode: built-in responses, not SnapTrade.",
        "snaptrade": "Reading SnapTrade.",
        "waiting": "Waiting for settings.",
    }[status.mode]
    read = f" Last read {_when(status.read_at)}." if status.read_at else ""
    connections = "".join(_connection(c, token) for c in status.connections) or (
        '<div class="empty-state"><strong>No brokerage connections yet</strong>'
        "<p>Connect one through SnapTrade's Connection Portal to begin.</p></div>"
    )
    accounts = [
        _account_row(connection, view, status)
        for connection in status.connections
        for view in connection.accounts
    ]
    holdings = [
        _holding_row(view, holding)
        for view in status.accounts
        if view.statement is not None
        for holding in view.statement.holdings
    ]
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
    waiting = (
        '<details class="notice quiet"><summary>Waiting for the account-side '
        "contract</summary><p>The SDK this version is built on does not carry these "
        "yet, so the plugin shows them here and records what today's contract can: "
        f"{e('; '.join(status.waiting))}.</p></details>"
        if status.waiting
        else ""
    )
    return _document(
        '<header class="page-head">'
        f"<div><h1>Brokerage connections</h1><p>{e(mode)}{read}</p></div>"
        '<div class="actions">'
        f"{_button('/admin/read', 'Read now', token)}"
        f"{_button('/admin/connect', 'Connect a brokerage', token, 'primary')}"
        "</div></header>"
        f"{_notices(status, notice, portal)}"
        f"{_tiles(status)}"
        '<section class="panel">'
        '<div class="panel-body"><h2>Connections</h2>'
        '<p class="muted">Each brokerage connected through SnapTrade, and how it is.</p>'
        f"</div>{connections}</section>"
        '<section class="panel">'
        '<div class="panel-body"><h2>Accounts</h2>'
        '<p class="muted">Each account the connections reach, its sync state and what to '
        "do about it, and what the last read recorded.</p></div>"
        + _grid("accounts", "Accounts", "No accounts yet.", "id", _ACCOUNT_COLUMNS, accounts)
        + "</section>"
        '<section class="panel">'
        '<div class="panel-body"><h2>Holdings read</h2>'
        '<p class="muted">What the last read found in each account, as recorded in the '
        "deployment's street store. Quantities are exact, as SnapTrade reported them.</p>"
        "</div>"
        + _grid(
            "holdings",
            "Holdings read",
            "Nothing read yet.",
            "key",
            _HOLDING_COLUMNS,
            holdings,
        )
        + "</section>"
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
        f"{waiting}",
        _GRIDS,
    )


def render_no_page() -> str:
    """What anybody but a deployment administrator gets at `/`."""
    return _document(
        '<section class="panel padded narrow">'
        "<h1>This plugin has no page for you</h1>"
        "<p>It brings brokerage accounts into this deployment through SnapTrade. You "
        "see those accounts, and what they hold, in the reports you have access to.</p>"
        "</section>"
    )


def serve(
    syncer: Syncer,
    loop: asyncio.AbstractEventLoop,
    port: int,
    wake: asyncio.Event,
    tokens: CsrfTokens | None = None,
) -> http.server.ThreadingHTTPServer:
    """Start the pages on 127.0.0.1:`port`, in a thread; the returned server's
    `shutdown()` stops it. SnapTrade is asked on `loop`, where the plugin lives."""
    csrf = tokens if tokens is not None else CsrfTokens()

    def on_loop(work: Coroutine[Any, Any, str]) -> str:
        return asyncio.run_coroutine_threadsafe(work, loop).result(timeout=30)

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
                self._send(403, render_no_page())
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
            path = self.path.split("?", 1)[0]
            if path == "/":
                caller = self._caller()
                if caller is None:
                    return
                if is_administrator(caller):
                    self._redirect("/admin")
                else:
                    self._send(200, render_no_page())
                return
            if path == "/admin":
                caller = self._administrator()
                if caller is not None:
                    self._send(200, render_admin(syncer.status, csrf.token(caller)))
                return
            self._send(404, "No such page.", "text/plain")

        def _presented_token(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MOST_BODY:
                return ""
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            values = parse_qs(body).get(CSRF_FIELD, [])
            return values[0] if len(values) == 1 else ""

        def do_POST(self) -> None:  # noqa: N802
            try:
                presented = self._presented_token()
            except ValueError:
                presented = ""
            caller = self._administrator()
            if caller is None:
                return
            if not csrf.valid(caller, presented):
                # Refused before anything is asked of SnapTrade or the plugin.
                self._send(
                    403,
                    "This form has expired or did not come from this page. "
                    "Reload the page and try again.",
                    "text/plain",
                )
                return
            token = csrf.token(caller)
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            venue = syncer.venue
            known = {c.connection_id for c in syncer.status.connections}
            notice, portal = "", None
            try:
                if parts == ["admin", "read"]:
                    loop.call_soon_threadsafe(wake.set)
                    notice = "Reading SnapTrade now. Reload in a moment."
                elif venue is None:
                    notice = "Nothing to ask SnapTrade until its settings are given."
                elif parts == ["admin", "connect"]:
                    portal = on_loop(venue.connection_portal())
                elif (
                    len(parts) == 4
                    and parts[:2] == ["admin", "connections"]
                    and parts[2] in known
                    and parts[3] in ("refresh", "reconnect")
                ):
                    if parts[3] == "refresh":
                        notice = on_loop(venue.refresh(parts[2]))
                        loop.call_soon_threadsafe(wake.set)
                    else:
                        portal = on_loop(venue.connection_portal(reconnect=parts[2]))
                else:
                    self._send(404, "No such action.", "text/plain")
                    return
            except VenueError as failed:
                notice = str(failed)
            except Exception as failed:
                # Named by its type only: its text is not known to be safe to show.
                notice = f"That failed: {type(failed).__name__}"
            self._send(200, render_admin(syncer.status, token, notice, portal))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
