"""The admin portal: served to administrators only, the root sending them to
it and telling anybody else there is no page for them, and never a secret."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import meridian
import pytest

from snaptrade import page
from snaptrade.contract import Contract
from snaptrade.normalise import ConnectionView, SyncState
from snaptrade.page import CsrfTokens, render_admin, render_no_page, serve, stylesheet
from snaptrade.settings import SYNTHETIC, config_from
from snaptrade.sync import Status, Syncer
from snaptrade.synthetic import SyntheticVenue

from conftest import Sidecar, caller_header, clock

TOKENS = CsrfTokens(b"a secret for tests only")


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    running = asyncio.new_event_loop()
    thread = threading.Thread(target=running.run_forever, daemon=True)
    thread.start()
    yield running
    running.call_soon_threadsafe(running.stop)
    thread.join(timeout=5)
    running.close()


@pytest.fixture
def synced(loop: asyncio.AbstractEventLoop) -> Syncer:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), Contract.of(sidecar), now=clock())
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run_coroutine_threadsafe(syncer.run_once(), loop).result(timeout=10)
    return syncer


@pytest.fixture
def server(synced: Syncer, loop: asyncio.AbstractEventLoop) -> Iterator[int]:
    wake = asyncio.Event()
    running = serve(synced, loop, 0, wake, TOKENS)
    yield running.server_address[1]
    running.shutdown()


def ask(
    port: int, method: str, path: str, caller: str | None = None, form: str | None = None
) -> tuple[int, str, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Meridian-Caller": caller} if caller else {}
    body = None
    if method == "POST":
        body = (form or "").encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request(method, path, body=body, headers=headers)
    answer = connection.getresponse()
    return answer.status, answer.getheader("Location") or "", answer.read().decode()


@pytest.fixture
def administrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page, "is_administrator", lambda caller: True)


def test_a_request_the_sidecar_did_not_vouch_for_is_refused(server: int) -> None:
    assert ask(server, "GET", "/")[0] == 401
    assert ask(server, "GET", "/admin")[0] == 401


def test_anybody_but_an_administrator_is_told_there_is_no_page_for_them(server: int) -> None:
    status, _, body = ask(server, "GET", "/", caller_header())
    assert status == 200 and "This plugin has no page for you" in body
    status, _, body = ask(server, "GET", "/admin", caller_header())
    assert status == 403 and "Alpaca" not in body
    token = TOKENS.token(meridian.Caller.from_header(caller_header()))
    assert ask(server, "POST", "/admin/read", caller_header(), f"csrf={token}")[0] == 403


@pytest.mark.usefixtures("administrator")
def test_an_administrator_is_sent_to_the_admin_portal(server: int) -> None:
    status, location, _ = ask(server, "GET", "/", caller_header())
    assert (status, location) == (303, "/admin")
    status, _, body = ask(server, "GET", "/admin", caller_header())
    assert status == 200
    for shown in (
        "Alpaca",
        "Interactive Brokers",
        "Schwab",
        "Delayed by design",
        "Disabled",
        "ALPACA:SYN-ALP-1001",
        "Connect a brokerage",
        "synthetic-user",
    ):
        assert shown in body
    assert "no stable ID" in body


class Recording(SyntheticVenue):
    """Synthetic SnapTrade, keeping what it was asked."""

    def __init__(self) -> None:
        super().__init__(clock())
        self.asked: list[str] = []

    async def refresh(self, connection_id: str) -> str:
        self.asked.append(f"refresh {connection_id}")
        return await super().refresh(connection_id)

    async def connection_portal(self, reconnect: str | None = None) -> str:
        self.asked.append(f"portal {reconnect}")
        return await super().connection_portal(reconnect)


@pytest.fixture
def recording(synced: Syncer) -> Recording:
    venue = Recording()
    synced.venue = venue
    return venue


def token_on_the_page(port: int, caller: str) -> str:
    _, _, body = ask(port, "GET", "/admin", caller)
    tokens: set[str] = set(re.findall(r'name="csrf" value="([0-9a-f]+)"', body))
    assert len(tokens) == 1, "every form carries the one token"
    return tokens.pop()


@pytest.mark.usefixtures("administrator")
def test_the_admin_actions_answer_with_the_pages_token(
    server: int, synced: Syncer, recording: Recording
) -> None:
    token = token_on_the_page(server, caller_header())
    status, _, body = ask(server, "POST", "/admin/connect", caller_header(), f"csrf={token}")
    assert status == 200 and "Synthetic mode has no Connection Portal" in body
    known = synced.status.connections[0].connection_id
    path = f"/admin/connections/{known}/refresh"
    status, _, body = ask(server, "POST", path, caller_header(), f"csrf={token}")
    assert status == 200 and "nothing was asked of SnapTrade" in body
    assert recording.asked == ["portal None", f"refresh {known}"]
    # The answer's forms carry the token too.
    assert f'value="{token}"' in body
    status, _, _ = ask(
        server,
        "POST",
        "/admin/connections/not-one-of-ours/refresh",
        caller_header(),
        f"csrf={token}",
    )
    assert status == 404


@pytest.mark.usefixtures("administrator")
@pytest.mark.parametrize(
    "form",
    [
        None,
        "",
        "csrf=",
        "csrf=0123456789abcdef",
        # Another person's token.
        f"csrf={TOKENS.token(meridian.Caller.from_header(caller_header('person-2')))}",
        # A token under another secret, as a different process would make.
        f"csrf={CsrfTokens(b'another').token(meridian.Caller.from_header(caller_header()))}",
    ],
)
def test_a_post_without_the_right_token_is_refused_and_does_nothing(
    server: int, synced: Syncer, recording: Recording, form: str | None
) -> None:
    known = synced.status.connections[0].connection_id
    for path in (
        "/admin/connect",
        f"/admin/connections/{known}/refresh",
        f"/admin/connections/{known}/reconnect",
        "/admin/read",
    ):
        status, _, body = ask(server, "POST", path, caller_header(), form)
        assert status == 403 and "expired" in body
    assert recording.asked == []


@pytest.mark.usefixtures("administrator")
def test_two_tokens_in_one_form_are_refused(server: int, recording: Recording) -> None:
    token = token_on_the_page(server, caller_header())
    status, _, _ = ask(
        server, "POST", "/admin/connect", caller_header(), f"csrf={token}&csrf={token}"
    )
    assert status == 403 and recording.asked == []


def test_a_token_is_the_same_for_one_person_and_differs_between_people() -> None:
    first = meridian.Caller.from_header(caller_header("person-1"))
    again = meridian.Caller.from_header(caller_header("person-1", "Renamed"))
    other = meridian.Caller.from_header(caller_header("person-2"))
    assert TOKENS.token(first) == TOKENS.token(again) != TOKENS.token(other)
    assert TOKENS.valid(first, TOKENS.token(again))
    assert not TOKENS.valid(first, "")
    assert "secret" not in repr(TOKENS)


def test_the_stylesheet_is_served_apart_from_the_markup(server: int) -> None:
    status, _, css = ask(server, "GET", page.STYLESHEET)
    assert status == 200 and "--accent" in css


def test_what_snaptrade_says_is_escaped() -> None:
    hostile = ConnectionView(
        "c1", "<script>alert(1)</script>", "Broker & Co", "read", SyncState.CURRENT, "", None
    )
    shown = render_admin(Status(mode="snaptrade", connections=(hostile,)), "t")
    assert "<script>" not in shown and "&lt;script&gt;" in shown
    assert "Broker &amp; Co" in shown


def test_the_portal_link_opens_outside_the_frame() -> None:
    shown = render_admin(
        Status(mode="snaptrade"), "t", portal="https://portal.example/x?a=1&b=2"
    )
    assert 'href="https://portal.example/x?a=1&amp;b=2" target="_blank"' in shown
    assert 'rel="noopener noreferrer"' in shown


def test_waiting_for_settings_names_them_without_values() -> None:
    shown = render_admin(Status(mode="waiting", missing=("snaptrade_consumer_key",)), "t")
    assert "SnapTrade consumer key" in shown


def test_the_no_page_answer_holds_no_account() -> None:
    assert "<table" not in render_no_page()


def test_the_stylesheet_uses_tokens_and_names_no_colour_outside_them() -> None:
    css = stylesheet()
    rules = re.sub(r":root[^{]*\{[^}]*\}", "", css)
    rules = re.sub(r"@media \(prefers-color-scheme: dark\) \{\s*\}", "", rules)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules)


# The design repository's tokens: mounted by `make test`, or beside this
# repository in a workspace. Private, so a public CI run skips this.
DESIGN = Path(
    os.environ.get("MERIDIAN_BRAND_TOKENS")
    or Path(__file__).resolve().parents[2] / "meridian-design" / "brand" / "tokens.json"
)


@pytest.mark.skipif(not DESIGN.exists(), reason="meridian-design is not beside this repo")
def test_the_stylesheets_colours_are_the_design_tokens() -> None:
    tokens = {
        token["name"]: token["value"]
        for token in json.loads(DESIGN.read_text())["color"]["tokens"]
    }
    css = stylesheet()
    light = css.split("@media")[0]
    dark = css.split(':root[data-theme="dark"]')[1]
    for block, theme in ((light, "light"), (dark, "dark")):
        for name, value in re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6})", block):
            assert tokens[name][theme] == value, (theme, name)
