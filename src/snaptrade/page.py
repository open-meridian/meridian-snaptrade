"""The plugin's admin portal, and nothing for anybody else.

Ruled by the product owner on 2026-09-28 (spec/plugin-pages-share-one-kit,
Q5, and the amendment the same day): this plugin has an admin portal and no
user portal. The admin portal covers what only SnapTrade knows: its users
under the key, the brokerage connections, connecting one through SnapTrade's
Connection Portal, each connection's health and each account's sync state with
what to do, and reconnecting or refreshing a connection. The SnapTrade keys
are entered in the dashboard's settings form, never here; linking accounts and
granting access are the dashboard's too. People see accounts and holdings
through a reporting plugin, not through custody.

`/` sends a deployment administrator to `/admin` and tells anybody else there
is no page for them. `/admin` is served to administrators only, decided from
the caller the sidecar verified (contract.is_administrator, which is waiting
for the caller to say so, and until then refuses everybody).

Markup here and style in static/page.css, apart, since both move onto the
shared plugin kit (spec/plugin-pages-share-one-kit) and the class names are
the kit's likely ones: page, panel, badge, notice, button, table.

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
import secrets
import threading
from collections.abc import Coroutine
from datetime import date, datetime
from importlib import resources
from typing import Any
from urllib.parse import parse_qs

import meridian

from .contract import is_administrator
from .normalise import REMEDY, AccountView, ConnectionView, SyncState
from .settings import CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET
from .sync import Status, Syncer
from .venue import VenueError

TITLE = "SnapTrade"
STYLESHEET = "/static/page.css"
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
_STATE_TONE = {
    SyncState.CURRENT: "good",
    SyncState.STALE: "warn",
    SyncState.NEEDS_SIGN_IN: "bad",
    SyncState.DISABLED: "bad",
    SyncState.DELAYED_BY_DESIGN: "info",
}
_SETTING_LABEL = {
    CLIENT_ID: "SnapTrade client ID",
    CONSUMER_KEY: "SnapTrade consumer key",
    USER_ID: "SnapTrade user ID",
    USER_SECRET: "SnapTrade user secret",
}


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _when(moment: datetime | date | None) -> str:
    if moment is None:
        return '<span class="faint">not reported</span>'
    if isinstance(moment, datetime):
        return f'<time datetime="{e(moment.isoformat())}">{moment:%Y-%m-%d %H:%M} UTC</time>'
    return f'<time datetime="{e(moment.isoformat())}">{moment:%Y-%m-%d}</time>'


def _document(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{TITLE}</title><link rel="stylesheet" href="{STYLESHEET}">'
        f'</head><body><main class="page">{body}</main></body></html>'
    )


def _badge(state: SyncState) -> str:
    return f'<span class="badge {_STATE_TONE[state]}">{_STATE_LABEL[state]}</span>'


def _button(action: str, label: str, token: str, primary: bool = False) -> str:
    kind = "button primary" if primary else "button"
    return (
        f'<form method="post" action="{e(action)}" class="inline">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{e(token)}">'
        f'<button class="{kind}">{e(label)}</button></form>'
    )


def _notices(status: Status, notice: str, portal: str | None) -> str:
    parts: list[str] = []
    if portal is not None:
        if portal:
            parts.append(
                '<div class="notice info">SnapTrade\'s Connection Portal is ready. '
                f'<a href="{e(portal)}" target="_blank" rel="noopener noreferrer">'
                "Open it</a> and sign in to the brokerage; the new connection appears "
                "here on the next read.</div>"
            )
        else:
            parts.append(
                '<div class="notice info">Synthetic mode has no Connection Portal. It opens '
                "once the SnapTrade settings are given and synthetic mode is off.</div>"
            )
    if notice:
        parts.append(f'<div class="notice info">{e(notice)}</div>')
    if status.mode == "waiting":
        wanted = ", ".join(_SETTING_LABEL.get(name, name) for name in status.missing)
        parts.append(
            '<div class="notice bad">Not reading SnapTrade: the plugin\'s settings need '
            f"{e(wanted)}. Give them in this plugin's settings in the dashboard, or turn "
            "on synthetic mode to try it without a key.</div>"
        )
    if status.error:
        parts.append(f'<div class="notice bad">The last read failed: {e(status.error)}</div>')
    if status.waiting:
        parts.append(
            '<details class="notice quiet"><summary>Waiting for the account-side '
            "contract</summary><p>The SDK this version is built on does not carry these "
            "yet, so the plugin shows them here and records what today's contract can: "
            f"{e('; '.join(status.waiting))}.</p></details>"
        )
    return "".join(parts)


def _account_row(view: AccountView, status: Status) -> str:
    account, fresh = view.account, view.freshness
    outcome = status.outcomes.get(account.external_account_id)
    if view.statement is None:
        recorded = f'<span class="faint">{e(view.withheld or "nothing")}</span>'
    elif outcome is None:
        recorded = '<span class="faint">not yet</span>'
    elif outcome.stopped:
        recorded = (
            f'<span class="bad-ink">{outcome.recorded} of {outcome.rows} rows; '
            f"stopped: {e(outcome.stopped)}</span>"
        )
    elif outcome.already_recorded:
        recorded = "already recorded"
    else:
        extra = []
        if outcome.placeholders:
            extra.append(f"{outcome.placeholders} awaiting an instrument")
        if outcome.ambiguous:
            extra.append(f"{outcome.ambiguous} ambiguous")
        recorded = f"{outcome.recorded} rows" + (f" ({e(', '.join(extra))})" if extra else "")
    said = [fresh.detail] if fresh.detail else []
    if fresh.state is not SyncState.CURRENT:
        said.append(REMEDY[fresh.state])
    hints = "".join(f'<div class="hint">{e(line)}</div>' for line in said)
    identity = f"<code>{e(account.external_account_id)}</code>"
    if not account.stable:
        identity += (
            '<div class="hint">SnapTrade gives no stable ID for this account: after a '
            "reconnect it appears as a new account, to be linked again.</div>"
        )
    return (
        "<tr>"
        f"<td><strong>{e(account.name)}</strong>"
        f'<div class="hint">{e(account.account_type) or "type not given"}</div></td>'
        f"<td>{identity}</td>"
        f"<td>{_badge(fresh.state)}{hints}</td>"
        f"<td>{_when(fresh.holdings_as_of)}</td>"
        f"<td>{_when(fresh.history_as_of)}</td>"
        f"<td>{recorded}</td>"
        "</tr>"
    )


def _connection(connection: ConnectionView, status: Status, token: str) -> str:
    key = e(connection.connection_id)
    rows = "".join(_account_row(view, status) for view in connection.accounts)
    table = (
        '<div class="table-wrap"><table><thead><tr><th>Account</th><th>ID</th>'
        "<th>Sync state</th><th>Holdings as of</th><th>History as of</th>"
        f"<th>Last statement</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else '<p class="faint">No accounts under this connection.</p>'
    )
    access = {"read": "read-only", "trade": "trading"}.get(connection.access, connection.access)
    remedy = "" if connection.state is SyncState.CURRENT else REMEDY[connection.state]
    return (
        '<article class="connection">'
        '<div class="row">'
        f"<h3>{e(connection.institution or 'Unknown brokerage')}</h3>"
        f"{_badge(connection.state)}"
        f'<span class="faint">{e(connection.name)}{" · " if access else ""}{e(access)}</span>'
        '<span class="spacer"></span>'
        f"{_button(f'/admin/connections/{key}/refresh', 'Refresh', token)}"
        f"{_button(f'/admin/connections/{key}/reconnect', 'Reconnect', token)}"
        "</div>"
        + (
            f'<p class="hint">{e(" ".join(filter(None, (connection.detail, remedy))))}</p>'
            if remedy
            else ""
        )
        + table
        + "</article>"
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
    users = (
        "".join(
            f"<li><code>{e(user)}</code>{' (this instance)' if user == status.user_id else ''}"
            "</li>"
            for user in status.users
        )
        or '<li class="faint">None listed.</li>'
    )
    connections = "".join(_connection(c, status, token) for c in status.connections) or (
        '<p class="faint">No brokerage connections yet. Connect one to begin.</p>'
    )
    return _document(
        '<header class="page-head">'
        f'<div><h1>{TITLE}</h1><p class="faint">{e(mode)}{read}</p></div>'
        f"{_button('/admin/read', 'Read now', token)}"
        "</header>"
        f"{_notices(status, notice, portal)}"
        '<section class="panel">'
        "<h2>SnapTrade user</h2>"
        + (
            f"<p>This instance reads the brokerage connections of "
            f"<code>{e(status.user_id)}</code>.</p>"
            if status.user_id
            else '<p class="faint">No SnapTrade user is set, as with a personal key.</p>'
        )
        + f'<p class="hint">Users registered under this key:</p><ul class="plain">{users}</ul>'
        "</section>"
        '<section class="panel">'
        '<div class="row"><h2>Brokerage connections</h2><span class="spacer"></span>'
        f"{_button('/admin/connect', 'Connect a brokerage', token, primary=True)}</div>"
        f"{connections}"
        "</section>"
    )


def render_no_page(administrator_waiting: bool = True) -> str:
    """What anybody but an administrator gets at `/`."""
    waiting = (
        '<p class="hint">Administrators: the admin page opens once the deployment tells '
        "plugins who its administrators are.</p>"
        if administrator_waiting
        else ""
    )
    return _document(
        '<section class="panel narrow">'
        f"<h1>{TITLE}</h1>"
        "<p>This plugin has no page for you. It brings brokerage accounts into the "
        "deployment through SnapTrade; what they hold is shown elsewhere.</p>"
        f"{waiting}</section>"
    )


def stylesheet() -> str:
    return (resources.files("snaptrade") / "static" / "page.css").read_text(encoding="utf-8")


def serve(
    syncer: Syncer,
    loop: asyncio.AbstractEventLoop,
    port: int,
    wake: asyncio.Event,
    tokens: CsrfTokens | None = None,
) -> http.server.ThreadingHTTPServer:
    """Start the pages on 127.0.0.1:`port`, in a thread; the returned server's
    `shutdown()` stops it. SnapTrade is asked on `loop`, where the plugin lives."""
    css = stylesheet().encode()
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
            if path == STYLESHEET:
                self._send(200, css, "text/css")
                return
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
