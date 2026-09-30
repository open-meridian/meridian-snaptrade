"""The pages: Statements at the root, cut to the accounts each reader may read,
every account for a deployment admin, and plainly nothing for somebody with
nothing to read; two admin tabs served to deployment administrators only;
linking on the Account links tab acting for the admin; built on the kit and
usable without it; and never a secret."""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import re
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import meridian
import pytest
from meridian.v1 import sidecar_pb2

from snaptrade import synthetic
from snaptrade.__main__ import follow_links
from snaptrade.contract import Outcome
from snaptrade.linking import DeploymentAccount, Link, Links, LinkView, Offered, link_of
from snaptrade.normalise import (
    AccountView,
    ConnectionView,
    ExternalAccount,
    Freshness,
    Serving,
    SyncState,
)
from snaptrade.page import (
    ACCOUNTS,
    ADMIN_PAGES,
    CONNECTIONS,
    DELAYED,
    KIT,
    LINK,
    REAL_TIME,
    REFRESH_REFUSED,
    STATEMENTS,
    CsrfTokens,
    e,
    render_accounts,
    render_admins_only,
    render_connections,
    render_nothing_here,
    render_statements,
    serve,
    visible,
)
from snaptrade.settings import SYNTHETIC, config_from
from snaptrade.sync import Status, Syncer
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import VenueError

from conftest import Sidecar, caller_header, clock

TOKENS = CsrfTokens(b"a secret for tests only")
TABS = (CONNECTIONS, ACCOUNTS)
ADMIN = caller_header(deployment_admin=True)
# Somebody who may read nothing through this plugin.
PERSON = caller_header("person-2", "Not An Admin")
# Somebody who may read ACC-1 through it, and nothing else.
READER = caller_header("person-3", "A Reader", read=("ACC-1",))
# The synthetic read's accounts, as the last read reached them.
ALPACA = "ALPACA:SYN-ALP-1001"
IBKR = "INTERACTIVE-BROKERS-FLEX:SYN-IB-2002"
NAMES = ("Alpaca Margin", "IBKR Individual", "Schwab Brokerage")
# Links as the deployment holds them before the plugin starts: Alpaca's to
# ACC-1, which READER may read, and IBKR's to ACC-3, which they may not.
HELD = (
    meridian.LinkedExternalAccount(ALPACA, "ACC-1", "Household"),
    meridian.LinkedExternalAccount(IBKR, "ACC-3", "Spare"),
)


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    running = asyncio.new_event_loop()
    thread = threading.Thread(target=running.run_forever, daemon=True)
    thread.start()
    yield running
    running.call_soon_threadsafe(running.stop)
    thread.join(timeout=5)
    running.close()


def read_once(sidecar: Sidecar, loop: asyncio.AbstractEventLoop) -> Syncer:
    syncer = Syncer(sidecar.plugin(), now=clock())
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run_coroutine_threadsafe(syncer.run_once(), loop).result(timeout=10)
    return syncer


@pytest.fixture
def synced(sidecar: Sidecar, loop: asyncio.AbstractEventLoop) -> Syncer:
    return read_once(sidecar, loop)


@pytest.fixture
def woken() -> asyncio.Event:
    return asyncio.Event()


@contextmanager
def serving(
    syncer: Syncer,
    sidecar: Sidecar,
    loop: asyncio.AbstractEventLoop,
    woken: asyncio.Event | None = None,
) -> Iterator[int]:
    """The pages, started as `__main__` starts them: the first delivery of
    the plugin's links held before anything is served, and each one after."""
    links = Links(sidecar.plugin())
    following = asyncio.run_coroutine_threadsafe(
        follow_links(sidecar.plugin(), links), loop
    ).result(timeout=10)
    running = serve(syncer, links, loop, 0, woken or asyncio.Event(), TOKENS)
    try:
        yield running.server_address[1]
    finally:
        running.shutdown()
        loop.call_soon_threadsafe(following.cancel)


@pytest.fixture
def server(
    synced: Syncer, sidecar: Sidecar, loop: asyncio.AbstractEventLoop, woken: asyncio.Event
) -> Iterator[int]:
    with serving(synced, sidecar, loop, woken) as port:
        yield port


def started_with(
    links: Iterable[meridian.LinkedExternalAccount], loop: asyncio.AbstractEventLoop
) -> tuple[Sidecar, Syncer]:
    """A sidecar holding `links` already, as after a restart, and a read."""
    sidecar = Sidecar(links=links)
    return sidecar, read_once(sidecar, loop)


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


def token_of(caller: str) -> str:
    return TOKENS.token(meridian.Caller.from_header(caller))


def form(**fields: str) -> str:
    return "&".join(f"{name}={quote(value)}" for name, value in fields.items())


def assertion(header: str) -> sidecar_pb2.CallerAssertion:
    padded = header + "=" * (-len(header) % 4)
    decoded: sidecar_pb2.CallerAssertion = sidecar_pb2.CallerAssertion.FromString(
        base64.urlsafe_b64decode(padded)
    )
    return decoded


# ── Who is served ────────────────────────────────────────────────────────────


def test_the_admin_pages_are_setup_only_connections_and_account_links() -> None:
    # Holdings moved to Statements, the user side (2026-09-29, step 1).
    assert [(page.path, page.title) for page in ADMIN_PAGES] == [
        ("/admin/connections", "Connections"),
        ("/admin/accounts", "Account links"),
    ]
    declared = meridian.Interface(port=8000, title="SnapTrade", admin_pages=ADMIN_PAGES)
    assert [page.path for page in declared._declared().admin_pages] == list(TABS)


def test_a_request_the_sidecar_did_not_vouch_for_is_refused(server: int) -> None:
    for path in ("/", "/admin", *TABS):
        assert ask(server, "GET", path)[0] == 401


def test_somebody_with_nothing_to_read_is_told_so_plainly(
    server: int, sidecar: Sidecar
) -> None:
    status, _, body = ask(server, "GET", STATEMENTS, PERSON)
    assert status == 200 and "Nothing here for you" in body
    assert "om-grid" not in body and "<table" not in body
    for name in NAMES:
        assert name not in body
    # Nor are the deployment's accounts read for them.
    assert sidecar.sent("ReadAccountsForLinking") == []


def test_the_admin_pages_are_for_administrators_only(server: int, sidecar: Sidecar) -> None:
    for caller in (PERSON, READER):
        for path in ("/admin", *TABS):
            status, _, body = ask(server, "GET", path, caller)
            assert status == 403 and "for the deployment's administrators" in body
            assert "Alpaca" not in body and ALPACA not in body
    assert sidecar.sent("ReadAccountsForLinking") == []


def test_an_administrator_is_sent_to_the_first_admin_tab(server: int) -> None:
    assert ask(server, "GET", "/admin", ADMIN)[:2] == (303, CONNECTIONS)
    # The frame's theme, on the query, goes along.
    status, location, _ = ask(server, "GET", "/admin?om-mode=dark", ADMIN)
    assert (status, location) == (303, f"{CONNECTIONS}?om-mode=dark")


def test_the_connections_tab(server: int) -> None:
    status, _, body = ask(server, "GET", CONNECTIONS, ADMIN)
    assert status == 200
    for shown in (
        "Alpaca",
        "Interactive Brokers",
        "Schwab",
        "Delayed by design",
        "Disabled",
        "Connect a brokerage",
        "synthetic-user",
    ):
        assert shown in body
    assert "om-grid" not in body
    # When it was last read, as the kit's om-moment, readable without it.
    moment = '<om-moment label="Last read" value="2026-09-28T15:00:00+00:00">'
    assert f"{moment}Last read 2026-09-28 15:00 UTC</om-moment>" in body


def test_the_accounts_tab(server: int, sidecar: Sidecar) -> None:
    status, _, body = ask(server, "GET", ACCOUNTS, ADMIN)
    assert status == 200
    assert "Link each account" in body and ALPACA in body and "no stable ID" in body
    assert 'om-grid id="accounts"' in body
    assert (
        f'<om-account-map action="{LINK}" token-name="csrf" token="{token_of(ADMIN)}"' in body
    )
    # The deployment's accounts are read for the admin viewing the page.
    (read,) = sidecar.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(ADMIN)


def test_holdings_are_no_longer_an_admin_page(server: int) -> None:
    assert ask(server, "GET", "/admin/holdings", ADMIN)[0] == 404


# ── Statements, the user side ────────────────────────────────────────────────


def link_from_the_page(port: int, external_id: str, account_id: str) -> None:
    fields = form(
        csrf=token_of(ADMIN),
        intent="link",
        external_account_id=external_id,
        account_id=account_id,
    )
    assert ask(port, "POST", LINK, ADMIN, fields)[0] == 200


def statement_of(body: str, external_id: str) -> str:
    """One account's section on Statements."""
    start = body.index(f'<section class="panel" data-account="{external_id}">')
    return body[start : body.index("</section>", start)]


def test_a_reader_sees_only_the_accounts_they_may_read(server: int, sidecar: Sidecar) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    link_from_the_page(server, IBKR, "ACC-3")
    status, _, body = ask(server, "GET", STATEMENTS, READER)
    assert status == 200 and "<h1>Statements</h1>" in body
    assert "each account you may read" in body
    # ACC-1's account: its sync state, its last statement and its rows.
    shown = statement_of(body, ALPACA)
    assert "Alpaca Margin" in shown and '<span class="badge good">Current</span>' in shown
    assert "Last statement as of 2026-09-28" in shown
    assert 'om-grid id="rows-0"' in shown and "<code>AAPL</code>" in shown
    # Not ACC-3's, which they may not read, nor one nothing links.
    assert IBKR not in body and "IBKR Individual" not in body
    assert "Schwab Brokerage" not in body
    # Nothing to do here: no form, and nothing asked of the sidecar for them.
    assert "<form" not in body and 'name="csrf"' not in body
    reads = [params.acting_for for params in sidecar.sent("ReadAccountsForLinking")]
    assert assertion(READER) not in reads


def test_an_account_nothing_links_is_not_shown_to_a_reader(server: int) -> None:
    # Its rows were recorded here, which says nothing of a link: only the
    # plugin's account scope does, and it names none.
    status, _, body = ask(server, "GET", STATEMENTS, READER)
    assert status == 200 and "No statements yet" in body
    assert "An account appears once it is linked to yours." in body
    for name in NAMES:
        assert name not in body
    assert "om-grid" not in body


def test_an_account_unlinked_from_the_page_leaves_the_readers_view(server: int) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    assert "Alpaca Margin" in ask(server, "GET", STATEMENTS, READER)[2]
    fields = form(csrf=token_of(ADMIN), intent="unlink", external_account_id=ALPACA)
    assert ask(server, "POST", LINK, ADMIN, fields)[0] == 200
    assert "Alpaca Margin" not in ask(server, "GET", STATEMENTS, READER)[2]


def test_after_a_restart_a_reader_sees_the_accounts_linked_before_it(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        status, _, body = ask(port, "GET", STATEMENTS, READER)
    assert status == 200 and "Alpaca Margin" in statement_of(body, ALPACA)
    assert IBKR not in body and "Schwab Brokerage" not in body
    # Known from the first delivery, not linked again.
    assert links_sent(sidecar) == []


def test_a_deployment_admin_sees_every_account(server: int) -> None:
    status, _, body = ask(server, "GET", STATEMENTS, ADMIN)
    assert status == 200 and "as a deployment administrator you see them all" in body
    for name in NAMES:
        assert name in body
    assert '<span class="badge bad">Disabled</span>' in body
    assert '<span class="badge info">Delayed by design</span>' in body
    # Synthetic data says so, to whoever reads it.
    assert "Synthetic mode: every figure here is invented" in body


def test_the_frames_theme_on_the_query_does_not_change_what_is_served(server: int) -> None:
    status, _, body = ask(server, "GET", f"{STATEMENTS}?om-mode=dark", PERSON)
    assert status == 200 and "Nothing here for you" in body


# ── The connections' actions ─────────────────────────────────────────────────


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


def tokens_on(body: str) -> set[str]:
    return set(re.findall(r'name="csrf" value="([0-9a-f]+)"', body))


def test_every_form_on_every_tab_carries_the_one_token(server: int) -> None:
    for path in TABS:
        _, _, body = ask(server, "GET", path, ADMIN)
        assert tokens_on(body) == {token_of(ADMIN)}


def test_the_connections_actions_answer_with_the_pages_token(
    server: int, synced: Syncer, recording: Recording
) -> None:
    token = token_of(ADMIN)
    status, _, body = ask(server, "POST", "/admin/connect", ADMIN, f"csrf={token}")
    assert status == 200 and "Synthetic mode has no Connection Portal" in body
    # The one SnapTrade serves on a delay: a refresh applies to it.
    known = synthetic.IBKR
    path = f"{CONNECTIONS}/{known}/refresh"
    status, _, body = ask(server, "POST", path, ADMIN, f"csrf={token}")
    assert status == 200 and "nothing was asked of SnapTrade" in body
    assert recording.asked == ["portal None", f"refresh {known}"]
    assert tokens_on(body) == {token}
    unknown = f"{CONNECTIONS}/not-one-of-ours/refresh"
    assert ask(server, "POST", unknown, ADMIN, f"csrf={token}")[0] == 404


def refresh_button(connection_id: str) -> str:
    return f'action="{CONNECTIONS}/{connection_id}/refresh"'


def test_refresh_is_offered_only_where_snaptrade_serves_on_a_delay(server: int) -> None:
    _, _, body = ask(server, "GET", CONNECTIONS, ADMIN)
    # Interactive Brokers is served on a delay: Refresh, and that it may be charged.
    assert refresh_button(synthetic.IBKR) in body
    assert DELAYED in body and "may charge for each refresh" in body
    # Alpaca and Schwab are served in real time: a plain line, and no Refresh.
    assert refresh_button(synthetic.ALPACA) not in body
    assert refresh_button(synthetic.SCHWAB) not in body
    assert body.count(REAL_TIME) == 2
    # Reconnecting is offered to each.
    for key in (synthetic.ALPACA, synthetic.IBKR, synthetic.SCHWAB):
        assert f'action="{CONNECTIONS}/{key}/reconnect"' in body


@pytest.mark.parametrize(
    ("serving", "offered", "said"),
    [
        (Serving.REAL_TIME, False, REAL_TIME),
        (Serving.DELAYED, True, DELAYED),
        # SnapTrade did not say: Refresh stays, and a refusal is said plainly.
        (Serving.UNKNOWN, True, None),
    ],
)
def test_the_refresh_button_follows_how_snaptrade_serves_the_connection(
    serving: Serving, offered: bool, said: str | None
) -> None:
    connection = ConnectionView(
        "c1", "n", "Broker", "read", SyncState.CURRENT, "", None, serving=serving
    )
    shown = render_connections(Status(mode="snaptrade", connections=(connection,)), "t")
    assert (refresh_button("c1") in shown) is offered
    assert 'action="/admin/connections/c1/reconnect"' in shown
    for line in (REAL_TIME, DELAYED):
        assert (line in shown) is (line == said)


def test_a_real_time_connection_is_not_refreshed_by_a_form_from_an_older_page(
    server: int, recording: Recording, woken: asyncio.Event
) -> None:
    path = f"{CONNECTIONS}/{synthetic.ALPACA}/refresh"
    status, _, body = ask(server, "POST", path, ADMIN, f"csrf={token_of(ADMIN)}")
    assert status == 200 and REAL_TIME in body
    assert recording.asked == [] and not woken.is_set()


class Refused(Exception):
    """What SnapTrade's SDK raises, as far as VenueError reads it."""

    def __init__(self, status: int) -> None:
        super().__init__("the SDK's text, which can carry the user secret")
        self.status = status


class Refusing(Recording):
    """SnapTrade answering a refresh with an HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status

    async def refresh(self, connection_id: str) -> str:
        self.asked.append(f"refresh {connection_id}")
        raise VenueError("refreshing a connection", Refused(self.status))


def settle(loop: asyncio.AbstractEventLoop) -> None:
    """Let the loop run what the page's thread handed it."""
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)


def test_snaptrade_refusing_a_refresh_is_said_plainly(
    server: int, synced: Syncer, loop: asyncio.AbstractEventLoop, woken: asyncio.Event
) -> None:
    synced.venue = refusing = Refusing(403)
    path = f"{CONNECTIONS}/{synthetic.IBKR}/refresh"
    status, _, body = ask(server, "POST", path, ADMIN, f"csrf={token_of(ADMIN)}")
    assert status == 200 and refusing.asked == [f"refresh {synthetic.IBKR}"]
    assert f'<div class="notice info" role="status">{e(REFRESH_REFUSED)}</div>' in body
    assert "Refused" not in body and "HTTP 403" not in body and "secret" not in body
    # Nothing was refreshed, so nothing is read again for it.
    settle(loop)
    assert not woken.is_set()


def test_another_refresh_failure_is_shown_as_before(server: int, synced: Syncer) -> None:
    synced.venue = Refusing(500)
    path = f"{CONNECTIONS}/{synthetic.IBKR}/refresh"
    status, _, body = ask(server, "POST", path, ADMIN, f"csrf={token_of(ADMIN)}")
    assert status == 200
    assert "refreshing a connection failed: Refused (HTTP 500)" in body
    assert 'class="notice bad" role="alert"' in body
    assert e(REFRESH_REFUSED) not in body
    assert "secret" not in body


def test_reading_now_answers_with_the_tab_it_was_asked_from(
    server: int, woken: asyncio.Event
) -> None:
    token = token_of(ADMIN)
    _, _, body = ask(server, "POST", "/admin/read", ADMIN, form(csrf=token, back=ACCOUNTS))
    assert "Reading SnapTrade now" in body and "Link each account" in body
    _, _, body = ask(server, "POST", "/admin/read", ADMIN, form(csrf=token, back="/elsewhere"))
    assert "Brokerage connections" in body
    assert woken.is_set()


def refresh_form(body: str) -> tuple[str, dict[str, str]]:
    """The head's Refresh: the one form in the page head's actions whose
    button is marked as a header action, its action and its fields."""
    head = re.search(r'<header class="page-head">(.*?)</header>', body, re.S)
    assert head is not None
    actions = re.search(r'<div class="actions">(.*)</div>', head.group(1), re.S)
    assert actions is not None
    forms = re.findall(
        r'<form method="post" action="([^"]+)" class="inline">(.*?)</form>', actions.group(1)
    )
    marked = [(action, inside) for action, inside in forms if "data-om-action" in inside]
    assert len(marked) == 1, "one header action in the head"
    action, inside = marked[0]
    assert re.search(r'<button data-om-action="refresh">Refresh</button>', inside)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', inside))
    return action, fields


def test_each_admin_tab_marks_refresh_as_a_header_action_for_the_dashboard(
    server: int,
) -> None:
    # The product owner, 2026-09-30: "Read now" becomes Refresh in the
    # dashboard's header. The kit hands it to the dashboard where it frames
    # the page; the form, its token and its POST stay the page's.
    for path in TABS:
        _, _, body = ask(server, "GET", path, ADMIN)
        assert "Read now" not in body
        action, fields = refresh_form(body)
        assert action == "/admin/read"
        assert fields == {"csrf": token_of(ADMIN), "back": path}
    # Only the head's: no other button on a tab is handed to the dashboard.
    _, _, body = ask(server, "GET", CONNECTIONS, ADMIN)
    assert body.count("data-om-action") == 1


def test_the_marked_refresh_still_posts_from_the_page_with_its_token(
    server: int, woken: asyncio.Event
) -> None:
    tabs = ((CONNECTIONS, "Brokerage connections"), (ACCOUNTS, "Link each account"))
    for path, shown in tabs:
        _, _, body = ask(server, "GET", path, ADMIN)
        action, fields = refresh_form(body)
        # Without the page's token, nothing is read.
        forged = form(**{**fields, "csrf": "0" * len(fields["csrf"])})
        status, _, answer = ask(server, "POST", action, ADMIN, forged)
        assert status == 403 and "Reading SnapTrade now" not in answer
        status, _, answer = ask(server, "POST", action, ADMIN, form(**fields))
        assert status == 200 and "Reading SnapTrade now" in answer and shown in answer
    assert woken.is_set()


def test_statements_hands_the_dashboard_no_header_action(server: int) -> None:
    for caller in (ADMIN, PERSON):
        _, _, body = ask(server, "GET", STATEMENTS, caller)
        assert "data-om-action" not in body


# ── Linking, on the Account links tab ───────────────────────────────────────


def links_sent(sidecar: Sidecar) -> list[Any]:
    return sidecar.sent("LinkExternalAccount")


def row_of(body: str, external_id: str) -> str:
    """One account's row in the plain forms shown without the kit."""
    start = body.index(f'<div class="list-row" data-account="{external_id}">')
    ends = [
        found
        for found in (
            body.find('<div class="list-row"', start + 1),
            body.find('<div class="panel-body panel-section">', start),
            body.find("</om-account-map>", start),
        )
        if found != -1
    ]
    return body[start : min(ends)]


def fallback_of(body: str) -> str:
    """The forms under the rows, shown without the kit: one to link any
    account, and one to create an account for any."""
    start = body.index('<div class="panel-body panel-section"><h3>Link an account</h3>')
    return body[start : body.index("</om-account-map>", start)]


def map_of(body: str) -> dict[str, Any]:
    """The JSON declared inside om-account-map, which the kit's map reads."""
    data = re.search(
        r'<om-account-map [^>]*><script type="application/json">(.*?)</script>', body
    )
    assert data is not None
    parsed: dict[str, Any] = json.loads(data.group(1))
    return parsed


def nothing_linked_or_read(sidecar: Sidecar) -> bool:
    return links_sent(sidecar) == [] and sidecar.sent("ReadAccountsForLinking") == []


def post_link(port: int, **fields: str) -> tuple[int, str, str]:
    """The map's one form route, with the admin's token."""
    return ask(port, "POST", LINK, ADMIN, form(csrf=token_of(ADMIN), **fields))


def test_after_a_restart_every_link_is_named_from_the_first_delivery(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, ADMIN)
    assert map_of(body)["links"] == [
        {"external_account_id": ALPACA, "account_id": "ACC-1", "account_name": "Household"},
        {"external_account_id": IBKR, "account_id": "ACC-3", "account_name": "Spare"},
    ]
    assert "Linked to Household (<code>ACC-1</code>)." in row_of(body, ALPACA)
    assert "Linked to Spare (<code>ACC-3</code>)." in row_of(body, IBKR)
    rows = {row["id"]: row for row in grid(body, "accounts")["rows"]}
    assert rows[ALPACA]["link"] == rows[IBKR]["link"] == "Linked"
    assert "2 linked, 1 not linked." in body
    assert links_sent(sidecar) == []


def test_a_link_made_elsewhere_reaches_the_page_with_the_next_delivery(
    server: int, sidecar: Sidecar, loop: asyncio.AbstractEventLoop
) -> None:
    assert "Not linked" in row_of(ask(server, "GET", ACCOUNTS, ADMIN)[2], ALPACA)
    loop.call_soon_threadsafe(sidecar.set_link, ALPACA, "ACC-3", "Spare")
    for _ in range(100):
        row = row_of(ask(server, "GET", ACCOUNTS, ADMIN)[2], ALPACA)
        if "Linked to Spare" in row:
            break
        time.sleep(0.02)
    assert "Linked to Spare" in row and links_sent(sidecar) == []


def test_rows_recorded_do_not_make_an_account_linked(server: int) -> None:
    # The stand-in records every row; only the account scope says a link.
    _, _, body = ask(server, "GET", ACCOUNTS, ADMIN)
    row = row_of(body, ALPACA)
    assert '<span class="badge warn">Not linked</span>' in row and "Unlink" not in row
    assert map_of(body)["links"] == []
    assert "Not known" not in body and "not known" not in body


def test_a_linked_account_names_its_account_and_offers_unlink_and_another(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, ADMIN)
    row = row_of(body, ALPACA)
    assert '<span class="badge good">Linked</span>' in row
    assert 'name="intent" value="unlink"' in row and ">Unlink</button>" in row
    # Linking to another account replaces this link: the one form below the
    # rows links any account, this one too, to another.
    assert "<select" not in row
    forms = fallback_of(body)
    assert "Linking a linked account to another replaces its link." in forms
    assert f'<option value="{ALPACA}">Alpaca Margin (Alpaca · margin)</option>' in forms
    assert '<option value="ACC-3">Spare</option>' in forms


def unlinked(name: str, params: Any) -> Exception | None:
    """The sidecar's refusal of a row for the Alpaca account, which nothing links."""
    if name == "RecordHolding" and params.external_account_id == ALPACA:
        return meridian.NotLinked(
            "RecordHolding", f"external account {ALPACA} is not linked to an account"
        )
    return None


def test_an_unlinked_account_offers_an_existing_account_or_a_new_one(
    loop: asyncio.AbstractEventLoop,
) -> None:
    refusing = Sidecar(refuse=unlinked)
    syncer = read_once(refusing, loop)
    with serving(syncer, refusing, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, ADMIN)
    row = row_of(body, ALPACA)
    assert '<span class="badge warn">Not linked</span>' in row
    assert "Unlink" not in row and "<form" not in row
    # Without the kit: one form links any account the read reached to any of
    # the deployment's open accounts, and nothing closed, each with its
    # custodian and type beside its name where it has them.
    forms = fallback_of(body)
    link, create = re.findall(r"<form .*?</form>", forms)
    assert f'action="{LINK}"' in link and 'name="intent" value="link"' in link
    assert '<option value="ACC-1">Household (Schwab, Brokerage)</option>' in link
    assert '<option value="ACC-3">Spare</option>' in link and "Retired" not in link
    assert f'<option value="{ALPACA}">Alpaca Margin (Alpaca · margin)</option>' in link
    # And one creates a new account for any of them, named, held at and of
    # the type the admin writes (W6.4).
    assert 'name="intent" value="create"' in create
    assert f'<option value="{ALPACA}">' in create
    assert 'name="new_account_name" required maxlength="200"' in create
    assert 'name="new_account_custodian" maxlength="200"' in create
    assert 'name="new_account_type" maxlength="200"' in create
    (read,) = refusing.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(ADMIN)
    # The same for the kit's map, as its JSON.
    data = map_of(body)
    alpaca = next(x for x in data["external_accounts"] if x["external_account_id"] == ALPACA)
    assert alpaca == {
        "external_account_id": ALPACA,
        "name": "Alpaca Margin",
        "detail": "Alpaca · margin",
        "custodian": "Alpaca",
        "account_type": "margin",
        "note": "",
        # What the kit's map matches a suggestion on, and groups by.
        "number": "SYN0001001",
        "connection": "Alpaca · Connection 1",
        "connection_id": "00000000-0000-4000-8000-00000000a001",
    }
    assert data["accounts"][0] == {
        "account_id": "ACC-1",
        "name": "Household",
        "custodian": "Schwab",
        "account_type": "Brokerage",
        "open": True,
    }
    assert {a["account_id"]: a["open"] for a in data["accounts"]}["ACC-2"] is False


def test_linking_to_an_existing_account_is_sent_for_the_admin(
    server: int, sidecar: Sidecar, woken: asyncio.Event
) -> None:
    status, _, body = post_link(
        server, intent="link", external_account_id=ALPACA, account_id="ACC-1"
    )
    assert status == 200 and "Linked Alpaca Margin to Household." in body
    (sent,) = links_sent(sidecar)
    assert (sent.external_account_id, sent.account_id, sent.new_account_name) == (
        ALPACA,
        "ACC-1",
        "",
    )
    assert sent.acting_for == assertion(ADMIN)
    # The page answering shows the link as the account scope now gives it.
    assert "Linked to Household" in row_of(body, ALPACA)
    assert map_of(body)["links"] == [
        {"external_account_id": ALPACA, "account_id": "ACC-1", "account_name": "Household"}
    ]
    # A read follows, so the account's rows are recorded.
    assert woken.is_set()


def test_linking_a_linked_account_to_another_replaces_the_link(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        status, _, body = post_link(
            port, intent="link", external_account_id=ALPACA, account_id="ACC-3"
        )
    assert status == 200 and "Linked Alpaca Margin to Spare." in body
    (sent,) = links_sent(sidecar)
    assert (sent.external_account_id, sent.account_id) == (ALPACA, "ACC-3")
    assert "Linked to Spare" in row_of(body, ALPACA)
    assert [
        link for link in map_of(body)["links"] if link["external_account_id"] == ALPACA
    ] == [{"external_account_id": ALPACA, "account_id": "ACC-3", "account_name": "Spare"}]


def test_creating_a_new_account_names_it_and_links_in_one_step(
    server: int, sidecar: Sidecar
) -> None:
    status, _, body = post_link(
        server,
        intent="create",
        external_account_id=ALPACA,
        new_account_name="  Alpaca margin  ",
    )
    assert status == 200 and "Created Alpaca margin and linked Alpaca Margin to it." in body
    (sent,) = links_sent(sidecar)
    assert (sent.account_id, sent.new_account_name) == ("", "Alpaca margin")
    assert (sent.new_account_custodian, sent.new_account_type) == ("", "")
    assert sent.acting_for == assertion(ADMIN)
    assert "Linked to Alpaca margin" in row_of(body, ALPACA)


def test_a_new_account_is_sent_with_the_custodian_and_type_the_admin_left(
    server: int, sidecar: Sidecar
) -> None:
    status, _, body = post_link(
        server,
        intent="create",
        external_account_id=ALPACA,
        new_account_name="Alpaca margin",
        new_account_custodian=" Alpaca Securities ",
        new_account_type="Margin",
    )
    assert status == 200 and "Created Alpaca margin and linked Alpaca Margin to it." in body
    (sent,) = links_sent(sidecar)
    assert (
        sent.new_account_name,
        sent.new_account_custodian,
        sent.new_account_type,
        sent.new_account_owner,
        sent.new_account_note,
    ) == ("Alpaca margin", "Alpaca Securities", "Margin", "", "")


def test_linking_to_an_existing_account_sends_no_custodian_or_type(
    server: int, sidecar: Sidecar
) -> None:
    # They describe a new account; one that exists is described on the
    # dashboard (W6.3), and the conductor ignores them on a link to it.
    status, _, _ = post_link(
        server,
        intent="link",
        external_account_id=ALPACA,
        account_id="ACC-1",
        new_account_name="Ignored",
        new_account_custodian="Alpaca",
        new_account_type="margin",
    )
    assert status == 200
    (sent,) = links_sent(sidecar)
    assert (
        sent.account_id,
        sent.new_account_name,
        sent.new_account_custodian,
        sent.new_account_type,
    ) == ("ACC-1", "", "", "")


def test_unlinking_sends_neither_account_nor_name(loop: asyncio.AbstractEventLoop) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        status, _, body = post_link(
            port, intent="unlink", external_account_id=ALPACA, account_id="ACC-3"
        )
    assert status == 200 and "Unlinked Alpaca Margin." in body
    (sent,) = links_sent(sidecar)
    assert (sent.external_account_id, sent.account_id, sent.new_account_name) == (
        ALPACA,
        "",
        "",
    )
    assert sent.acting_for == assertion(ADMIN)
    assert '<span class="badge warn">Not linked</span>' in row_of(body, ALPACA)
    assert [link["external_account_id"] for link in map_of(body)["links"]] == [IBKR]


@pytest.mark.parametrize(
    ("intent", "fields", "said"),
    [
        ("link", {"account_id": ""}, "Choose the account to link it to."),
        ("create", {"new_account_name": "   "}, "Name the new account."),
    ],
)
def test_a_form_missing_its_choice_asks_for_it_and_sends_nothing(
    server: int, sidecar: Sidecar, intent: str, fields: dict[str, str], said: str
) -> None:
    status, _, body = post_link(server, intent=intent, external_account_id=ALPACA, **fields)
    assert status == 200 and said in body and links_sent(sidecar) == []


@pytest.mark.parametrize(
    "intent",
    [None, "", "delete", "LINK", " ", "link&intent=unlink"],
)
def test_a_bad_or_missing_intent_is_refused_and_links_nothing(
    server: int, sidecar: Sidecar, intent: str | None
) -> None:
    sent = form(csrf=token_of(ADMIN), external_account_id=ALPACA, account_id="ACC-1")
    if intent is not None:
        sent += f"&intent={intent}"
    status, _, body = ask(server, "POST", LINK, ADMIN, sent)
    assert status == 400 and "does not say what to do" in body
    assert links_sent(sidecar) == []


def test_only_an_account_the_read_reached_is_linked(server: int, sidecar: Sidecar) -> None:
    for external_id in ("not-one-of-ours", ""):
        status, _, _ = post_link(
            server, intent="link", external_account_id=external_id, account_id="ACC-1"
        )
        assert status == 404
    # The one route: 0.3's per-action paths are gone.
    for gone in ("create", "unlink", "delete"):
        fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA, new_account_name="N")
        assert ask(server, "POST", f"{ACCOUNTS}/{gone}", ADMIN, fields)[0] == 404
    assert links_sent(sidecar) == []


# ── Several links in one form ────────────────────────────────────────────────


def post_several(
    port: int, pairs: Iterable[tuple[str, str]], caller: str = ADMIN
) -> tuple[int, str, str]:
    """The map's several-link form: the token, the intent, then a pair of IDs for each link."""
    fields = [f"csrf={quote(token_of(caller))}", "intent=link-several"]
    for external_id, account_id in pairs:
        fields += [
            f"external_account_id={quote(external_id)}",
            f"account_id={quote(account_id)}",
        ]
    return ask(port, "POST", LINK, caller, "&".join(fields))


def test_the_map_offers_several_links_and_groups_by_connection(server: int) -> None:
    _, _, body = ask(server, "GET", ACCOUNTS, ADMIN)
    element = re.search(r"<om-account-map [^>]*>", body)
    assert element is not None
    assert ' group-by="connection" link-several ' in element.group(0)


def test_several_links_are_sent_each_for_the_admin_and_each_reported(
    server: int, sidecar: Sidecar, woken: asyncio.Event
) -> None:
    status, _, body = post_several(server, [(ALPACA, "ACC-1"), (IBKR, "ACC-3")])
    assert status == 200
    assert '<div class="notice good" role="status">Linked 2 accounts.' in body
    assert "<li>Linked Alpaca Margin to Household.</li>" in body
    assert "<li>Linked IBKR Individual to Spare.</li>" in body
    sent = links_sent(sidecar)
    # One link_external_account for each, acting for the admin, naming no new account.
    assert sorted((s.external_account_id, s.account_id, s.new_account_name) for s in sent) == [
        (ALPACA, "ACC-1", ""),
        (IBKR, "ACC-3", ""),
    ]
    assert all(s.acting_for == assertion(ADMIN) for s in sent)
    # The page answering shows both, as the account scope now gives them.
    assert "Linked to Household" in row_of(body, ALPACA)
    assert "Linked to Spare" in row_of(body, IBKR)
    assert woken.is_set()


def test_one_link_refused_leaves_the_others_and_each_is_said(
    synced: Syncer, loop: asyncio.AbstractEventLoop
) -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "LinkExternalAccount" and params.external_account_id == IBKR:
            return meridian.CallFailed("LinkExternalAccount", "refused", "no such account")
        return None

    refusing = Sidecar(refuse=refuse)
    with serving(synced, refusing, loop) as port:
        status, _, body = post_several(
            port, [(ALPACA, "ACC-1"), (IBKR, "ACC-404"), ("not-one-of-ours", "ACC-3")]
        )
    assert status == 200
    assert (
        '<div class="notice warn" role="status">Linked 1 of 3 accounts; 2 not linked:' in body
    )
    shown = body[body.index('<div class="notice warn"') :]
    listed = shown[: shown.index("<details>")]
    assert (
        "<li>IBKR Individual: not linked. The sidecar refused it: no such account</li>"
        in listed
    )
    assert (
        "<li>not-one-of-ours: not linked, as the last read of SnapTrade did not reach it.</li>"
        in listed
    )
    assert "<summary>Each of the 3</summary>" in shown
    assert "<li>Linked Alpaca Margin to Household.</li>" in shown
    assert [s.external_account_id for s in links_sent(refusing)] == [ALPACA]
    assert "Linked to Household" in row_of(body, ALPACA)


def test_every_link_refused_is_said_as_none(
    synced: Syncer, loop: asyncio.AbstractEventLoop
) -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "LinkExternalAccount":
            return meridian.CallFailed("LinkExternalAccount", "refused", "not an admin")
        return None

    refusing = Sidecar(refuse=refuse)
    with serving(synced, refusing, loop) as port:
        status, _, body = post_several(port, [(ALPACA, "ACC-1")])
    assert status == 200
    assert '<div class="notice bad" role="alert">Linked none of the 1 account:' in body


@pytest.mark.parametrize(
    "pairs",
    [
        "",
        # An account without the account it is to be linked to, and the other way.
        f"&external_account_id={quote(ALPACA)}",
        "&account_id=ACC-1",
        f"&external_account_id={quote(ALPACA)}&external_account_id={quote(IBKR)}&account_id=ACC-1",
        # One account twice, or an empty ID.
        f"&external_account_id={quote(ALPACA)}&account_id=ACC-1"
        f"&external_account_id={quote(ALPACA)}&account_id=ACC-3",
        "&external_account_id=&account_id=ACC-1",
        f"&external_account_id={quote(ALPACA)}&account_id=",
    ],
)
def test_several_links_that_do_not_pair_up_are_refused_whole(
    server: int, sidecar: Sidecar, pairs: str
) -> None:
    sent = f"csrf={quote(token_of(ADMIN))}&intent=link-several{pairs}"
    status, _, body = ask(server, "POST", LINK, ADMIN, sent)
    assert status == 400 and "do not pair up" in body
    assert links_sent(sidecar) == []


def test_several_links_take_a_form_of_thousands_of_pairs(server: int, sidecar: Sidecar) -> None:
    # Far over the 8 KB any other form here may be: pairs for accounts the
    # read did not reach, each said, none sent.
    many = [(f"broker:{n:06d}-{'x' * 36}", f"ACC-{n:06d}-{'y' * 30}") for n in range(3000)]
    status, _, body = post_several(server, [(ALPACA, "ACC-1"), *many])
    assert status == 200
    assert "Linked 1 of 3001 accounts; 3000 not linked:" in body
    assert [s.external_account_id for s in links_sent(sidecar)] == [ALPACA]
    # Anywhere else, a body that size is no form of this page's.
    known = "/admin/read"
    padded = f"csrf={quote(token_of(ADMIN))}&pad={'z' * 9000}"
    assert ask(server, "POST", known, ADMIN, padded)[0] == 403


def test_several_links_are_only_for_an_administrator(server: int, sidecar: Sidecar) -> None:
    status, _, body = post_several(server, [(ALPACA, "ACC-1")], PERSON)
    assert status == 403 and "for the deployment's administrators" in body
    assert nothing_linked_or_read(sidecar)


def test_the_page_without_the_kit_stays_one_list_however_many_accounts(
    loop: asyncio.AbstractEventLoop,
) -> None:
    # Each account is a row, and the choices are two lists below them, not a
    # picker of every account on every row: the page grows with the accounts
    # and the deployment's accounts added, not multiplied.
    connection = synthetic_status().connections[0]
    template = connection.accounts[0]

    def page_with(n: int, deployment: int) -> str:
        views = tuple(
            replace(
                template,
                account=replace(
                    template.account, external_account_id=f"broker:{i}", name=f"Account {i}"
                ),
            )
            for i in range(n)
        )
        status = Status(mode="snaptrade", connections=(replace(connection, accounts=views),))
        offered = Offered(
            tuple(DeploymentAccount(f"ACC-{i}", f"Book {i}") for i in range(deployment))
        )
        return render_accounts(status, "t", links_for(status, meridian.AccountScope()), offered)

    small, large = page_with(10, 10), page_with(1000, 1000)
    for shown in (small, large):
        assert fallback_of(shown).count("<select") == 3
    assert large.count("<option") == 3 * 1000 + 3
    assert len(large) < 100 * len(small)


def test_the_sidecars_refusal_is_shown_plainly(
    synced: Syncer, loop: asyncio.AbstractEventLoop
) -> None:
    said = "LinkExternalAccount is admitted only acting for a deployment admin"

    def refuse(name: str, params: Any) -> Exception | None:
        if name == "LinkExternalAccount":
            return meridian.CallFailed("LinkExternalAccount", "refused", said)
        return None

    refusing = Sidecar(refuse=refuse)
    with serving(synced, refusing, loop) as port:
        status, _, body = post_link(
            port, intent="link", external_account_id=ALPACA, account_id="ACC-1"
        )
    assert status == 200
    assert (
        f'<div class="notice bad" role="alert">The sidecar refused this: {said}</div>' in body
    )
    # Still as the account scope gives it.
    assert '<span class="badge warn">Not linked</span>' in row_of(body, ALPACA)


def test_when_the_deployments_accounts_cannot_be_read_only_a_new_one_is_offered(
    loop: asyncio.AbstractEventLoop,
) -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "ReadAccountsForLinking":
            return meridian.CallFailed("ReadAccountsForLinking", "refused", "not an admin")
        return unlinked(name, params)

    refusing = Sidecar(refuse=refuse)
    syncer = read_once(refusing, loop)
    with serving(syncer, refusing, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, ADMIN)
    assert "The deployment's accounts could not be read: not an admin" in body
    forms = fallback_of(body)
    assert 'name="account_id"' not in forms and 'name="new_account_name"' in forms
    assert "none is offered here" in forms
    # The kit's map is told they could not be read.
    assert map_of(body)["accounts"] is None


def test_linking_is_only_for_an_administrator(server: int, sidecar: Sidecar) -> None:
    # Even with a token that is theirs, somebody else is refused before
    # anything is sent.
    for intent in ("link", "create", "unlink"):
        fields = form(
            csrf=token_of(PERSON),
            intent=intent,
            external_account_id=ALPACA,
            account_id="ACC-1",
            new_account_name="Mine",
        )
        status, _, body = ask(server, "POST", LINK, PERSON, fields)
        assert status == 403 and "for the deployment's administrators" in body
    assert nothing_linked_or_read(sidecar)


# ── The CSRF token ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sent",
    [
        None,
        "",
        "csrf=",
        "csrf=0123456789abcdef",
        # Another person's token.
        f"csrf={token_of(caller_header('person-2'))}",
        # A token under another secret, as a different process would make.
        f"csrf={CsrfTokens(b'another').token(meridian.Caller.from_header(ADMIN))}",
        # The right token twice.
        f"csrf={token_of(ADMIN)}&csrf={token_of(ADMIN)}",
    ],
)
def test_a_post_without_the_right_token_is_refused_and_does_nothing(
    server: int, synced: Syncer, sidecar: Sidecar, recording: Recording, sent: str | None
) -> None:
    known = synced.status.connections[0].connection_id
    rest = f"&external_account_id={quote(ALPACA)}&account_id=ACC-1&new_account_name=N"
    for path, intent in (
        ("/admin/connect", ""),
        (f"{CONNECTIONS}/{known}/refresh", ""),
        (f"{CONNECTIONS}/{known}/reconnect", ""),
        ("/admin/read", ""),
        # The map's one route, whatever the form says it means.
        (LINK, "&intent=link"),
        (LINK, "&intent=create"),
        (LINK, "&intent=unlink"),
        (LINK, "&intent=link-several"),
    ):
        status, _, body = ask(server, "POST", path, ADMIN, (sent or "") + rest + intent)
        assert status == 403 and "expired" in body
    assert recording.asked == [] and nothing_linked_or_read(sidecar)


def test_a_token_is_the_same_for_one_person_and_differs_between_people() -> None:
    first = meridian.Caller.from_header(caller_header("person-1"))
    again = meridian.Caller.from_header(caller_header("person-1", "Renamed"))
    other = meridian.Caller.from_header(caller_header("person-2"))
    assert TOKENS.token(first) == TOKENS.token(again) != TOKENS.token(other)
    assert TOKENS.valid(first, TOKENS.token(again))
    assert not TOKENS.valid(first, "")
    assert "secret" not in repr(TOKENS)


# ── What is drawn ────────────────────────────────────────────────────────────


def test_what_snaptrade_says_is_escaped() -> None:
    hostile = ConnectionView(
        "c1", "<script>alert(1)</script>", "Broker & Co", "read", SyncState.CURRENT, "", None
    )
    shown = render_connections(Status(mode="snaptrade", connections=(hostile,)), "t")
    assert "<script>" not in shown and "&lt;script&gt;" in shown
    assert "Broker &amp; Co" in shown


def test_the_portal_link_opens_outside_the_frame() -> None:
    shown = render_connections(
        Status(mode="snaptrade"), "t", portal="https://portal.example/x?a=1&b=2"
    )
    assert 'href="https://portal.example/x?a=1&amp;b=2" target="_blank"' in shown
    assert 'rel="noopener noreferrer"' in shown


def test_waiting_for_settings_names_them_by_label_without_values() -> None:
    status = Status(mode="waiting", missing=("snaptrade_consumer_key",))
    shown = render_connections(status, "t")
    assert "Consumer key" in shown
    # Statements does not name settings; it says SnapTrade is not read yet.
    shown = render_statements(status, [], everyone=False)
    assert "Consumer key" not in shown and "SnapTrade is not being read yet." in shown


def test_the_refusals_hold_no_account() -> None:
    for shown in (render_nothing_here(), render_admins_only()):
        assert "<table" not in shown and "om-grid" not in shown
        assert f'href="{KIT}meridian.css"' in shown


def synthetic_status() -> Status:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock())
    syncer.configure(config_from({SYNTHETIC: True}))
    return asyncio.run(syncer.run_once())


def links_for(
    status: Status, scope: meridian.AccountScope | None = None
) -> dict[str, LinkView]:
    """Each account's link as `scope` gives it: by default, Alpaca's to ACC-1."""
    held = scope if scope is not None else meridian.AccountScope(links=HELD[:1])
    return {
        view.account.external_account_id: link_of(held, view.account.external_account_id)
        for view in status.accounts
    }


def synthetic_pages() -> dict[str, str]:
    status = synthetic_status()
    offered = Offered()
    everyone = visible(status, links_for(status), meridian.Caller.from_header(ADMIN))
    return {
        CONNECTIONS: render_connections(status, "t"),
        ACCOUNTS: render_accounts(status, "t", links_for(status), offered),
        STATEMENTS: render_statements(status, everyone, everyone=True),
    }


def test_each_page_is_built_on_the_kit_with_no_style_or_chrome_of_its_own() -> None:
    for shown in (*synthetic_pages().values(), render_nothing_here(), render_admins_only()):
        assert f'<link rel="stylesheet" href="{KIT}meridian.css">' in shown
        assert f'<script src="{KIT}meridian.js"></script>' in shown
        # One stylesheet, the kit's; no style, colour or chrome of the page's.
        assert shown.count('rel="stylesheet"') == 1
        assert "<style" not in shown and "style=" not in shown
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", shown)
        # No script of its own: the kit's, and data declared for its components.
        scripts = re.findall(r"<script[^>]*>", shown)
        assert set(scripts) <= {
            f'<script src="{KIT}meridian.js">',
            '<script type="application/json">',
        }
        assert "customElements" not in shown
        # The dashboard draws the tabs, and the frame the rest.
        for chrome in (
            "<nav",
            "<img",
            "<svg",
            "<footer",
            'class="tabs"',
            "data-om-mode",
            "theme",
        ):
            assert chrome not in shown
        assert '<main class="page">' in shown


def test_the_kit_is_the_one_whose_account_map_scales() -> None:
    # 0.5.0: the map searched, filtered, grouped and paged, with suggestions
    # and several links in one form.
    assert KIT == "/.meridian/ui/0.5.0/"


def grid_element(shown: str, grid_id: str) -> str:
    element = re.search(rf'<om-grid id="{grid_id}" [^>]*>(.*?)</om-grid>', shown)
    assert element is not None
    return element.group(0)


def grid(shown: str, grid_id: str) -> dict[str, list[dict[str, Any]]]:
    """The columns and rows declared inside a grid, which the kit's grid reads."""
    data = re.search(
        r'^<om-grid [^>]*><script type="application/json">(.*?)</script>',
        grid_element(shown, grid_id),
    )
    assert data is not None
    parsed: dict[str, list[dict[str, Any]]] = json.loads(data.group(1))
    return parsed


def test_each_table_is_in_the_html_until_the_kits_grid_replaces_it() -> None:
    pages = synthetic_pages()
    for path, grid_id, row_key in (
        (ACCOUNTS, "accounts", "id"),
        (STATEMENTS, "rows-0", "key"),
        (STATEMENTS, "rows-1", "key"),
    ):
        shown = pages[path]
        element = grid_element(shown, grid_id)
        # Cards where it is narrow.
        assert element.startswith(f'<om-grid id="{grid_id}" row-key="{row_key}" narrow="cards"')
        assert "<table>" in element
        data = grid(shown, grid_id)
        # As many rows in the table a browser shows as the grid is given.
        assert element.count("<tr>") == len(data["rows"]) + 1
        assert len({row[row_key] for row in data["rows"]}) == len(data["rows"])


def test_the_grids_columns_are_json_the_kit_draws_with_no_script() -> None:
    columns = {c["key"]: c for c in grid(synthetic_pages()[ACCOUNTS], "accounts")["columns"]}
    assert columns["account"] == {
        "key": "account",
        "label": "Account",
        "type": "text",
        "hint": "where",
        "strong": True,
    }
    assert columns["link"]["type"] == "badge" and columns["link"]["tone"] == {
        "field": "link_tone"
    }
    assert columns["state"]["tone"] == {"field": "tone"} and columns["state"]["hint"] == "todo"
    assert columns["holdings_as_of"]["blank"] == "not reported"
    assert columns["recorded"]["tone"] == {"field": "recorded_tone"}
    assert columns["id"]["type"] == "code" and columns["id"]["hint"] == "id_note"
    # Nothing of 0.3's own grid code: no text class, and no format.
    for column in columns.values():
        assert not {"ink", "format"} & set(column)


def test_the_accounts_grid_says_each_link() -> None:
    shown = synthetic_pages()[ACCOUNTS]
    rows = {row["id"]: row for row in grid(shown, "accounts")["rows"]}
    assert (rows[ALPACA]["link"], rows[ALPACA]["link_tone"]) == ("Linked", "good")
    assert (rows[IBKR]["link"], rows[IBKR]["link_tone"]) == ("Not linked", "warn")
    # And the table without the kit draws the same badges.
    table = grid_element(shown, "accounts")
    assert '<span class="badge good">Linked</span>' in table
    assert '<span class="badge warn">Not linked</span>' in table


def test_a_stopped_statement_is_toned_bad_in_the_grid_and_the_table() -> None:
    status = synthetic_status()
    stopped = Outcome(rows=3, recorded=1, stopped="RecordHolding: refused: no")
    status = Status(
        mode=status.mode,
        read_at=status.read_at,
        connections=status.connections,
        outcomes={**status.outcomes, ALPACA: stopped},
    )
    shown = render_accounts(status, "t", links_for(status), Offered())
    row = next(r for r in grid(shown, "accounts")["rows"] if r["id"] == ALPACA)
    assert (row["recorded"], row["recorded_tone"]) == ("Stopped at 1 of 3 rows", "bad")
    assert '<span class="bad-ink">Stopped at 1 of 3 rows</span>' in grid_element(
        shown, "accounts"
    )


def test_quantities_are_exact_decimal_strings_as_read() -> None:
    shown = synthetic_pages()[STATEMENTS]
    rows = [row for index in range(2) for row in grid(shown, f"rows-{index}")["rows"]]
    quantities = {row["instrument"]: row["quantity"] for row in rows}
    assert all(isinstance(quantity, str) for quantity in quantities.values())
    # Crypto to nine decimals, a short, and cash as SnapTrade gave it.
    assert "0.012345678" in quantities.values() and "-40" in quantities.values()
    assert "200.00" in quantities.values()
    assert '<td class="num">0.012345678</td>' in shown


def test_the_declared_json_cannot_close_its_script() -> None:
    hostile = "</script><script>alert(1)</script>"
    account = ExternalAccount("broker:1", True, hostile, "", "Broker", "c1", "s1")
    fresh = Freshness(SyncState.CURRENT, None, None, "")
    view = AccountView(account, fresh, None, "withheld")
    connection = ConnectionView(
        "c1", "n", "Broker", "read", SyncState.CURRENT, "", None, (view,)
    )
    status = Status(mode="snaptrade", connections=(connection,))
    shown = render_accounts(status, "t", links_for(status), Offered())
    assert "<script>alert" not in shown
    assert grid(shown, "accounts")["rows"][0]["account"] == hostile
    assert map_of(shown)["external_accounts"][0]["name"] == hostile


def test_a_closed_account_is_not_offered() -> None:
    status = synthetic_status()
    offered = Links(Sidecar().plugin())
    read = asyncio.run(offered.offered(ADMIN))
    assert [(a.account_id, a.open) for a in read.accounts] == [
        ("ACC-1", True),
        ("ACC-3", True),
        ("ACC-2", False),
    ]
    # Read with where each is held and what it is (W6.4).
    household = read.accounts[0]
    assert (household.custodian, household.account_type) == ("Schwab", "Brokerage")
    shown = render_accounts(status, "t", links_for(status, meridian.AccountScope()), read)
    assert "Household" in shown and '<option value="ACC-2">' not in shown
    # The kit's map is told it is closed, and offers only open ones.
    assert {a["account_id"]: a["open"] for a in map_of(shown)["accounts"]}["ACC-2"] is False


def test_the_links_are_the_account_scopes_and_nothing_else() -> None:
    # Linked, naming the account, or not linked: there is no third state.
    scope = meridian.AccountScope(links=HELD)
    assert link_of(scope, ALPACA) == LinkView(Link.LINKED, "ACC-1", "Household")
    assert link_of(scope, "broker:other") == LinkView(Link.UNLINKED)
    assert [state.value for state in Link] == ["linked", "unlinked"]
