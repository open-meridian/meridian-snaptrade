"""The pages, on the SDK's `meridian.Pages`: Connections and Account links at
`admin` (Manage), showing no account's data; Statements at `write` and `read`
(Open and View), cut to the accounts each person may read, with Refresh under
Open alone; each refused at any other level; linking acting for the plugin
admin, a new account only for a deployment admin; every action behind the
SDK's CSRF token; built on the kit's base template and usable without it; and
never a secret."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import re
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import meridian
import pytest
from meridian.pages import TOO_LARGE
from meridian.testing import PageClient, caller_header
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
    CONNECT,
    CONNECTIONS,
    DELAYED,
    KIT,
    LINK,
    ONLY_DEPLOYMENT_ADMINS,
    RAW,
    READ,
    REAL_TIME,
    RECONNECT,
    REFRESH,
    REFRESH_REFUSED,
    STATEMENTS,
    TITLE,
    hold,
    pages,
)
from snaptrade.raw import RawStore
from snaptrade.settings import SYNTHETIC, config_from
from snaptrade.sync import Status, Syncer
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import VenueError

from conftest import Sidecar, clock

ADMIN_LEVEL = sidecar_pb2.ACCESS_LEVEL_ADMIN
WRITE_LEVEL = sidecar_pb2.ACCESS_LEVEL_WRITE
READ_LEVEL = sidecar_pb2.ACCESS_LEVEL_READ
ADMIN_TABS = (CONNECTIONS, ACCOUNTS)
# A plugin admin under Manage, who is no deployment admin: they configure
# SnapTrade, link to any existing account, and name no new one.
MANAGER = caller_header("admin", subject="local|manager", display_name="A Manager")
# A deployment admin under Manage, who may also name a new account (W6.4): the
# header for the pages served over HTTP. In-process, PageClient asks as one
# with deployment_admin=True.
ROOT = caller_header("admin", subject="local|root", display_name="Root", deployment_admin=True)
# Under View, somebody who may read ACC-1 through the plugin, and nothing else.
READER = caller_header("read", read=("ACC-1",), subject="person-3", display_name="A Reader")
# Under Open, somebody who may write ACC-1, and so read it.
WRITER = caller_header("write", write=("ACC-1",), subject="person-4", display_name="A Writer")
# Under View, somebody who may read nothing through this plugin.
NOBODY = caller_header("read", subject="person-2", display_name="Not A Reader")
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
    the plugin's links held before anything is served, and each one after;
    each view run on the plugin's loop, by the SDK's server."""
    links = Links(sidecar.plugin())
    following = asyncio.run_coroutine_threadsafe(
        follow_links(sidecar.plugin(), links), loop
    ).result(timeout=10)
    hold(syncer, links, woken or asyncio.Event())
    running = pages.serve(sidecar.plugin(), 0, loop=loop)
    try:
        yield running.server_address[1]
    finally:
        running.shutdown()
        running.server_close()

        async def stop() -> None:
            following.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await following

        asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=5)


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
    return pages.csrf_token(meridian.Caller.from_header(caller))


def form(**fields: str) -> str:
    return "&".join(f"{name}={quote(value)}" for name, value in fields.items())


def assertion(header: str) -> sidecar_pb2.CallerAssertion:
    padded = header + "=" * (-len(header) % 4)
    decoded: sidecar_pb2.CallerAssertion = sidecar_pb2.CallerAssertion.FromString(
        base64.urlsafe_b64decode(padded)
    )
    return decoded


class Offering(Links):
    """The plugin's links, offering `offered` as the deployment's accounts, or
    what the sidecar stand-in answers when it is None."""

    def __init__(self, offered: Offered | None = None) -> None:
        super().__init__(Sidecar().plugin())
        self._offering = offered

    async def offered(self, acting_for: str) -> Offered:
        if self._offering is not None:
            return self._offering
        return await super().offered(acting_for)


def holding(
    status: Status,
    scope: meridian.AccountScope | None,
    offered: Offered | None,
    raw: RawStore | None = None,
) -> None:
    """The pages holding `status` as the last read, the links `scope` holds,
    `offered` as the deployment's accounts, and `raw`'s raw responses."""
    syncer = Syncer(Sidecar().plugin(), raw=raw)
    syncer.status = status
    links = Offering(offered)
    asyncio.run(links.hold(scope or meridian.AccountScope()))
    hold(syncer, links, asyncio.Event())


def page_for(
    status: Status,
    path: str = CONNECTIONS,
    caller: str = MANAGER,
    scope: meridian.AccountScope | None = None,
    offered: Offered | None = None,
    raw: RawStore | None = None,
) -> str:
    """The page at `path` as its view serves `caller`, from `status` and the
    links `scope` holds, with no server."""
    holding(status, scope, offered, raw)
    request = meridian.Request(
        "GET", path, meridian.Caller.from_header(caller), Sidecar().plugin()
    )
    answer = asyncio.run(pages.dispatch(request))
    assert answer.status == 200, answer.text
    return answer.text


def deployment_admins_page(
    status: Status,
    path: str,
    scope: meridian.AccountScope | None = None,
    offered: Offered | None = None,
) -> str:
    """The page at `path` under Manage for a deployment admin, as ROOT and as
    the SDK's PageClient asks it, with no server."""
    holding(status, scope, offered)
    client = PageClient(
        pages,
        Sidecar().plugin(),
        subject="local|root",
        display_name="Root",
        deployment_admin=True,
    )
    answer = client.get(path, "admin")
    assert answer.status == 200, answer.text
    return answer.text


def synthetic_status() -> Status:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock())
    syncer.configure(config_from({SYNTHETIC: True}))
    return asyncio.run(syncer.run_once())


def flat(body: str) -> str:
    """The page without the line breaks between its tags."""
    return re.sub(r">\s*\n\s*<", "><", body)


# ── Declared, and served at its levels ──────────────────────────────────────


def test_each_page_is_declared_once_with_the_levels_it_serves() -> None:
    # Connections and Account links at admin (Manage); Statements and Raw
    # responses at write and read (Open and View), each adapting by the
    # session's level.
    assert [(page.path, page.title, tuple(page.levels)) for page in pages.declared] == [
        (CONNECTIONS, "Connections", (ADMIN_LEVEL,)),
        (ACCOUNTS, "Account links", (ADMIN_LEVEL,)),
        (STATEMENTS, "Statements", (WRITE_LEVEL, READ_LEVEL)),
        (RAW, "Raw responses", (WRITE_LEVEL, READ_LEVEL)),
    ]
    # What registration sends: one list, the actions not among its tabs.
    declared = meridian.Interface(port=8000, title=TITLE, pages=pages)._declared()
    assert [(page.path, list(page.levels)) for page in declared.pages] == [
        (CONNECTIONS, [ADMIN_LEVEL]),
        (ACCOUNTS, [ADMIN_LEVEL]),
        (STATEMENTS, [WRITE_LEVEL, READ_LEVEL]),
        (RAW, [WRITE_LEVEL, READ_LEVEL]),
    ]


def test_each_page_is_served_at_its_levels_and_refused_at_the_others(
    synced: Syncer, sidecar: Sidecar
) -> None:
    hold(synced, Links(sidecar.plugin()), asyncio.Event())
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1"}, write={"ACC-1"})
    served = {(r.page.path, r.level): r.response.status for r in client.every_page()}
    assert served == {
        (CONNECTIONS, "admin"): 200,
        (CONNECTIONS, "write"): 403,
        (CONNECTIONS, "read"): 403,
        (ACCOUNTS, "admin"): 200,
        (ACCOUNTS, "write"): 403,
        (ACCOUNTS, "read"): 403,
        (STATEMENTS, "admin"): 403,
        (STATEMENTS, "write"): 200,
        (STATEMENTS, "read"): 200,
        (RAW, "admin"): 403,
        (RAW, "write"): 200,
        (RAW, "read"): 200,
    }


def test_a_request_the_sidecar_did_not_vouch_for_is_refused(server: int) -> None:
    for path in (STATEMENTS, "/admin", *ADMIN_TABS):
        assert ask(server, "GET", path)[0] == 401


def test_a_session_at_no_level_is_served_nothing(server: int) -> None:
    nothing = caller_header("", read=("ACC-1",), subject="person-9")
    for path in (STATEMENTS, "/admin", *ADMIN_TABS):
        assert ask(server, "GET", path, nothing)[0] == 403


def test_the_pages_at_admin_are_refused_under_open_and_view(
    server: int, sidecar: Sidecar
) -> None:
    for caller in (READER, WRITER, NOBODY):
        for path in ("/admin", *ADMIN_TABS):
            status, _, body = ask(server, "GET", path, caller)
            assert status == 403 and "is not served under" in body
            assert "Alpaca" not in body and ALPACA not in body
    assert sidecar.sent("ReadAccountsForLinking") == []


def test_manage_is_sent_to_its_first_tab(server: int) -> None:
    assert ask(server, "GET", "/admin", MANAGER)[:2] == (303, CONNECTIONS)
    # The frame's theme, on the query, goes along.
    status, location, _ = ask(server, "GET", "/admin?om-mode=dark", MANAGER)
    assert (status, location) == (303, f"{CONNECTIONS}?om-mode=dark")


def test_holdings_are_no_admin_page(server: int) -> None:
    assert ask(server, "GET", "/admin/holdings", MANAGER)[0] == 404


# ── Under Manage: configuration, and no account's data ──────────────────────


def test_the_connections_tab(server: int) -> None:
    status, _, body = ask(server, "GET", CONNECTIONS, MANAGER)
    assert status == 200
    for shown in (
        "Alpaca",
        "Interactive Brokers",
        "Schwab",
        "Delayed by design",
        "Needs sign-in",
        ">+ Add</button>",
        "synthetic-user",
    ):
        assert shown in body
    assert "om-grid" not in body
    # How it is reading, as the kit's status dot, readable without it.
    assert head_of(body).count("<om-status ") == 1
    assert (
        "<om-status data-om-header "
        'state="ok" label="Synthetic mode: built-in responses, not SnapTrade" '
        'at="2026-09-28T15:00:00+00:00" at-label="Last read">' in body
    )


def test_the_connections_tab_has_no_tiles_but_its_card_rows_and_actions(
    server: int, sidecar: Sidecar
) -> None:
    """Connections, Accounts reached and Last read are the plugin's figures on
    the Summary core draws (sync.figures), not tiles here; the card, its
    rows and their actions stay."""
    _, _, body = ask(server, "GET", CONNECTIONS, MANAGER)
    assert 'class="tiles"' not in body and "tile-label" not in body
    assert "Accounts reached" not in body and "needs attention" not in body
    # The card: its heading, Refresh beside + Add, and a row per connection,
    # Schwab's, needing sign-in, led to Reconnect.
    assert "<h2>Connections</h2>" in body
    assert ">Refresh</button>" in body and ">+ Add</button>" in body
    assert body.count('<div class="list-row">') == 3
    assert '<button class="primary">Reconnect</button>' in body
    assert "Holdings read" not in body and "statement" not in body
    # What the page no longer shows went on the heartbeat with the read.
    (beat,) = sidecar.heartbeats
    assert [figure.label for figure in beat.figures] == [
        "Connections",
        "Accounts reached",
        "Last read",
    ]


def test_the_accounts_tab(server: int, sidecar: Sidecar) -> None:
    status, _, body = ask(server, "GET", ACCOUNTS, MANAGER)
    assert status == 200
    assert "Link each account" in body and ALPACA in body and "no stable ID" in body
    assert "om-grid" not in body
    assert (
        f'<om-account-map action="{LINK}" token-name="csrf" token="{token_of(MANAGER)}"' in body
    )
    # The deployment's accounts are read for the admin viewing the page.
    (read,) = sidecar.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(MANAGER)


def test_account_links_shows_identities_and_links_only(
    loop: asyncio.AbstractEventLoop,
) -> None:
    # The product owner, 2026-09-30: a plugin admin is account agnostic. The
    # map is given who each account is and its link, and nothing SnapTrade
    # read for it: no sync state, statement or rows, so it draws no Status
    # column; and the plain rows say the same.
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, MANAGER)
    assert "status-heading" not in body
    for external in map_of(body)["external_accounts"]:
        assert set(external) == {
            "external_account_id",
            "name",
            "detail",
            "custodian",
            "account_type",
            "note",
            "number",
            "connection",
            "connection_id",
        }
    for said in (
        "Holdings as of",
        "History as of",
        "Last statement",
        "last statement",
        "Current",
        "Delayed by design",
        "Disabled",
        "Stale",
        "AAPL",
    ):
        assert said not in body, said


def test_manage_shows_no_accounts_data(synced: Syncer, sidecar: Sidecar) -> None:
    # Every page at admin, under Manage, with two accounts linked, shows none
    # of what the last read found in an account.
    links = Links(sidecar.plugin())
    asyncio.run(links.hold(meridian.AccountScope(links=HELD)))
    hold(synced, links, asyncio.Event())
    held: set[str] = {"Holdings as of", "History as of", "Last statement"}
    for view in synced.status.accounts:
        for holding in view.statement.holdings if view.statement else ():
            held.add(holding.description)
            held.update(i.value for i in holding.identifiers if i.scheme == "symbol")
            if "." in (quantity := format(holding.quantity, "f")):
                held.add(quantity)
    assert {"AAPL", "Bitcoin", "0.012345678"} <= held
    PageClient(pages, sidecar.plugin()).assert_no_account_data(*sorted(held))
    # And the same for a deployment admin, who is offered a new account too.
    PageClient(pages, sidecar.plugin(), deployment_admin=True).assert_no_account_data(
        *sorted(held)
    )


# ── Statements, under Open and View ─────────────────────────────────────────


def link_from_the_page(port: int, external_id: str, account_id: str) -> None:
    fields = form(
        csrf=token_of(MANAGER),
        intent="link",
        external_account_id=external_id,
        account_id=account_id,
    )
    assert ask(port, "POST", LINK, MANAGER, fields)[0] == 200


def statement_of(body: str, external_id: str) -> str:
    """One account's section on Statements."""
    start = body.index(f'<section class="panel" data-account="{external_id}">')
    return body[start : body.index("</section>", start)]


def test_a_reader_sees_only_the_accounts_they_may_read(server: int, sidecar: Sidecar) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    link_from_the_page(server, IBKR, "ACC-3")
    status, _, body = ask(server, "GET", STATEMENTS, READER)
    assert status == 200 and "<title>Statements · SnapTrade</title>" in body
    # The product owner, 2026-09-30: no intro line above the statements.
    assert "each account you may read" not in body
    # ACC-1's account: its sync state, its last statement and its rows.
    shown = statement_of(body, ALPACA)
    assert "Alpaca Margin" in shown and '<span class="badge good">Current</span>' in shown
    assert "Last statement as of 2026-09-28" in shown
    assert 'om-grid id="rows-0"' in shown and "<code>AAPL</code>" in shown
    # Not ACC-3's, which they may not read, nor one nothing links.
    assert IBKR not in body and "IBKR Individual" not in body
    assert "Schwab Brokerage" not in body
    # View acts on nothing: no form, and nothing asked of the sidecar for them.
    assert "<form" not in body and 'name="csrf"' not in body
    assert "data-om-action" not in body
    reads = [params.acting_for for params in sidecar.sent("ReadAccountsForLinking")]
    assert assertion(READER) not in reads


def test_open_shows_the_same_accounts_and_refresh(server: int) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    link_from_the_page(server, IBKR, "ACC-3")
    status, _, body = ask(server, "GET", STATEMENTS, WRITER)
    assert status == 200 and "Alpaca Margin" in statement_of(body, ALPACA)
    assert IBKR not in body and "Schwab Brokerage" not in body
    # Refresh, its one action, handed to the dashboard's header.
    action, fields = refresh_form(body)
    assert (action, fields) == (READ, {"csrf": token_of(WRITER), "back": STATEMENTS})
    assert body.count("data-om-action") == 1


def test_refresh_under_open_reads_now_and_answers_with_statements(
    server: int, woken: asyncio.Event
) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    woken.clear()
    status, _, body = ask(server, "POST", READ, WRITER, form(csrf=token_of(WRITER)))
    assert status == 200 and "Reading SnapTrade now. Reload in a moment." in body
    assert "Alpaca Margin" in statement_of(body, ALPACA)
    assert woken.is_set()


def test_view_reads_nothing_now(server: int, woken: asyncio.Event) -> None:
    status, _, body = ask(server, "POST", READ, READER, form(csrf=token_of(READER)))
    assert status == 403 and "not served under View" in body
    assert not woken.is_set()


def test_somebody_with_nothing_to_read_is_told_so_plainly(
    server: int, sidecar: Sidecar
) -> None:
    nobody_writes = caller_header("write", subject="person-5")
    for caller in (NOBODY, nobody_writes):
        status, _, body = ask(server, "GET", STATEMENTS, caller)
        assert status == 200 and "Nothing here for you" in body
        assert "om-grid" not in body and "<table" not in body
        assert "data-om-action" not in body
        for name in NAMES:
            assert name not in body
    # Nor are the deployment's accounts read for them.
    assert sidecar.sent("ReadAccountsForLinking") == []


def test_a_deployment_admin_reads_no_account_by_being_one(
    synced: Syncer, sidecar: Sidecar
) -> None:
    # The product owner, 2026-09-30: nobody has implicit data access. Under
    # Manage there is no Statements; under View, they read what their grants give.
    hold(synced, Links(sidecar.plugin()), asyncio.Event())
    client = PageClient(pages, sidecar.plugin(), deployment_admin=True)
    assert client.get(STATEMENTS, "admin").status == 403
    viewing = client.get(STATEMENTS, "read")
    assert viewing.status == 200 and "Nothing here for you" in viewing.text
    for name in NAMES:
        assert name not in viewing.text


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
    fields = form(csrf=token_of(MANAGER), intent="unlink", external_account_id=ALPACA)
    assert ask(server, "POST", LINK, MANAGER, fields)[0] == 200
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


def test_synthetic_data_says_so_to_whoever_reads_it(loop: asyncio.AbstractEventLoop) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", STATEMENTS, READER)
    assert "Synthetic mode: every figure here is invented" in body


def test_the_frames_theme_on_the_query_does_not_change_what_is_served(server: int) -> None:
    status, _, body = ask(server, "GET", f"{STATEMENTS}?om-mode=dark", NOBODY)
    assert status == 200 and "Nothing here for you" in body


# ── The connections' actions ─────────────────────────────────────────────────


class Recording(SyntheticVenue):
    """Synthetic SnapTrade, keeping what it was asked."""

    def __init__(self, portal: str | None = None) -> None:
        super().__init__(clock())
        self.asked: list[str] = []
        self._portal = portal

    async def refresh(self, connection_id: str) -> str:
        self.asked.append(f"refresh {connection_id}")
        return await super().refresh(connection_id)

    async def connection_portal(self, reconnect: str | None = None) -> str:
        self.asked.append(f"portal {reconnect}")
        if self._portal is not None:
            return self._portal
        return await super().connection_portal(reconnect)


@pytest.fixture
def recording(synced: Syncer) -> Recording:
    venue = Recording()
    synced.venue = venue
    return venue


def tokens_on(body: str) -> set[str]:
    return set(re.findall(r'name="csrf" value="([0-9a-f]+)"', body))


def test_every_form_on_every_tab_carries_the_one_token(server: int) -> None:
    for path in ADMIN_TABS:
        _, _, body = ask(server, "GET", path, MANAGER)
        assert tokens_on(body) == {token_of(MANAGER)}


def test_the_connections_actions_answer_with_the_pages_token(
    server: int, synced: Syncer, recording: Recording
) -> None:
    token = token_of(MANAGER)
    status, _, body = ask(server, "POST", CONNECT, MANAGER, form(csrf=token))
    assert status == 200 and "Synthetic mode has no Connection Portal" in body
    # The one SnapTrade serves on a delay: a refresh applies to it.
    known = synthetic.IBKR
    status, _, body = ask(
        server, "POST", REFRESH, MANAGER, form(csrf=token, connection_id=known)
    )
    assert status == 200 and "nothing was asked of SnapTrade" in body
    status, _, body = ask(
        server, "POST", RECONNECT, MANAGER, form(csrf=token, connection_id=known)
    )
    assert status == 200 and "Synthetic mode has no Connection Portal" in body
    assert recording.asked == ["portal None", f"refresh {known}", f"portal {known}"]
    assert tokens_on(body) == {token}
    for path in (REFRESH, RECONNECT):
        for unknown in ("not-one-of-ours", ""):
            fields = form(csrf=token, connection_id=unknown)
            assert ask(server, "POST", path, MANAGER, fields)[0] == 404
    assert len(recording.asked) == 3


def refresh_button(connection_id: str) -> str:
    return (
        f'<input type="hidden" name="connection_id" value="{connection_id}">'
        "<button>Refresh</button>"
    )


def reconnect_button(connection_id: str) -> str:
    return f'<input type="hidden" name="connection_id" value="{connection_id}"><button'


def test_refresh_is_offered_only_where_snaptrade_serves_on_a_delay(server: int) -> None:
    _, _, body = ask(server, "GET", CONNECTIONS, MANAGER)
    # Interactive Brokers is served on a delay: Refresh, and that it may be charged.
    assert refresh_button(synthetic.IBKR) in body
    assert DELAYED in body and "may charge for each refresh" in body
    # Alpaca and Schwab are served in real time: a plain line, and no Refresh.
    assert refresh_button(synthetic.ALPACA) not in body
    assert refresh_button(synthetic.SCHWAB) not in body
    assert body.count(REAL_TIME) == 2
    # Reconnecting is offered to each; it leads where the connection is disabled.
    for key in (synthetic.ALPACA, synthetic.IBKR):
        assert f"{reconnect_button(key)}>Reconnect</button>" in body
    assert f'{reconnect_button(synthetic.SCHWAB)} class="primary">Reconnect</button>' in body


@pytest.mark.parametrize(
    ("serving_as", "offered", "said"),
    [
        (Serving.REAL_TIME, False, REAL_TIME),
        (Serving.DELAYED, True, DELAYED),
        # SnapTrade did not say: Refresh stays, and a refusal is said plainly.
        (Serving.UNKNOWN, True, None),
    ],
)
def test_the_refresh_button_follows_how_snaptrade_serves_the_connection(
    serving_as: Serving, offered: bool, said: str | None
) -> None:
    connection = ConnectionView(
        "c1", "n", "Broker", "read", SyncState.CURRENT, "", None, serving=serving_as
    )
    shown = page_for(Status(mode="snaptrade", connections=(connection,)))
    assert (refresh_button("c1") in shown) is offered
    assert f"{reconnect_button('c1')}>Reconnect</button>" in shown
    for line in (REAL_TIME, DELAYED):
        assert (line in shown) is (line == said)


def test_a_real_time_connection_is_not_refreshed_by_a_form_from_an_older_page(
    server: int, recording: Recording, woken: asyncio.Event
) -> None:
    fields = form(csrf=token_of(MANAGER), connection_id=synthetic.ALPACA)
    status, _, body = ask(server, "POST", REFRESH, MANAGER, fields)
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
    """Let the loop run what it was handed."""
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)


def escaped(text: str) -> str:
    """As the templates write text: an apostrophe as &#39;."""
    return text.replace("'", "&#39;")


def test_snaptrade_refusing_a_refresh_is_said_plainly(
    server: int, synced: Syncer, loop: asyncio.AbstractEventLoop, woken: asyncio.Event
) -> None:
    synced.venue = refusing = Refusing(403)
    fields = form(csrf=token_of(MANAGER), connection_id=synthetic.IBKR)
    status, _, body = ask(server, "POST", REFRESH, MANAGER, fields)
    assert status == 200 and refusing.asked == [f"refresh {synthetic.IBKR}"]
    assert f'<div class="notice info" role="status">{escaped(REFRESH_REFUSED)}</div>' in body
    assert "Refused" not in body and "HTTP 403" not in body and "secret" not in body
    # Nothing was refreshed, so nothing is read again for it.
    settle(loop)
    assert not woken.is_set()


def test_another_refresh_failure_is_shown_as_before(server: int, synced: Syncer) -> None:
    synced.venue = Refusing(500)
    fields = form(csrf=token_of(MANAGER), connection_id=synthetic.IBKR)
    status, _, body = ask(server, "POST", REFRESH, MANAGER, fields)
    assert status == 200
    assert "refreshing a connection failed: Refused (HTTP 500)" in body
    assert 'class="notice bad" role="alert"' in body
    assert escaped(REFRESH_REFUSED) not in body
    assert "secret" not in body


def test_reading_now_answers_with_the_tab_it_was_asked_from(
    server: int, woken: asyncio.Event
) -> None:
    token = token_of(MANAGER)
    _, _, body = ask(server, "POST", READ, MANAGER, form(csrf=token, back=ACCOUNTS))
    assert "Reading SnapTrade now" in body and "Link each account" in body
    _, _, body = ask(server, "POST", READ, MANAGER, form(csrf=token, back="/elsewhere"))
    assert "Reading SnapTrade now" in body and ADD in body
    # Under Manage, never Statements, whatever the form says.
    _, _, body = ask(server, "POST", READ, MANAGER, form(csrf=token, back=STATEMENTS))
    assert ADD in body and "No statements yet" not in body
    assert woken.is_set()


# The Connections card's + Add, by its accessible name: shown only on Connections.
ADD = 'aria-label="Add a brokerage connection">+ Add</button>'


def head_of(body: str) -> str:
    head = re.search(r'<header class="page-head">(.*?)</header>', body, re.S)
    assert head is not None
    return head.group(1)


# The head's Refresh: a header action, drawn as the kit's refresh icon (kit
# 0.8.0), its words its name and its tooltip.
HEADER_REFRESH = (
    r'<button data-om-action="refresh" data-om-icon="refresh" title="Refresh">'
    r"Refresh</button>"
)


def refresh_form(body: str) -> tuple[str, dict[str, str]]:
    """The head's Refresh: the one form in the page head's actions whose
    button is marked as a header action, its action and its fields."""
    actions = re.search(r'<div class="actions">(.*)</div>', head_of(body), re.S)
    assert actions is not None
    forms = re.findall(
        r'<form method="post" action="([^"]+)" class="inline">(.*?)</form>', actions.group(1)
    )
    marked = [(action, inside) for action, inside in forms if "data-om-action" in inside]
    assert len(marked) == 1, "one header action in the head"
    action, inside = marked[0]
    assert re.search(HEADER_REFRESH, inside)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', inside))
    return action, fields


def connections_card(body: str) -> str:
    """What the Connections card's header holds beside its heading."""
    card = re.search(
        r'<section class="panel"><div class="panel-body"><div class="row"><h2>Connections</h2>'
        r'<span class="spacer"></span>(.*?)</div>',
        flat(body),
    )
    assert card is not None
    return card.group(1)


def card_refresh_form(body: str) -> tuple[str, dict[str, str]]:
    """The Connections card's Refresh, beside + Add: its action and fields."""
    forms = re.findall(
        r'<form method="post" action="([^"]+)" class="inline">(.*?)</form>',
        connections_card(body),
    )
    refreshes = [(action, inside) for action, inside in forms if ">Refresh</button>" in inside]
    assert len(refreshes) == 1, "one Refresh in the Connections card"
    action, inside = refreshes[0]
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', inside))
    return action, fields


def test_account_links_offers_no_refresh(server: int) -> None:
    # The product owner, 2026-09-30: "remove Refresh from Account links";
    # reading SnapTrade now is the Connections card's, beside + Add.
    _, _, body = ask(server, "GET", ACCOUNTS, MANAGER)
    assert f'action="{READ}"' not in body
    assert "data-om-action" not in body


def test_connections_hands_the_dashboard_no_header_action(server: int) -> None:
    # The product owner, 2026-09-30: Refresh beside + Add in the Connections
    # card, once on the page, and not in the head or the dashboard's header.
    _, _, body = ask(server, "GET", CONNECTIONS, MANAGER)
    assert "data-om-action" not in body
    assert '<div class="actions">' not in head_of(body)
    action, fields = card_refresh_form(body)
    assert action == READ
    assert fields == {"csrf": token_of(MANAGER), "back": CONNECTIONS}
    assert body.count(f'action="{READ}"') == 1


def test_each_admin_tabs_refresh_posts_from_the_page_with_its_token(
    server: int, woken: asyncio.Event
) -> None:
    tabs = ((CONNECTIONS, card_refresh_form, ADD),)
    for path, found, shown in tabs:
        woken.clear()
        _, _, body = ask(server, "GET", path, MANAGER)
        action, fields = found(body)
        # Without the page's token, nothing is read.
        forged = form(**{**fields, "csrf": "0" * len(fields["csrf"])})
        status, _, answer = ask(server, "POST", action, MANAGER, forged)
        assert status == 403 and "Reading SnapTrade now" not in answer
        assert not woken.is_set()
        status, _, answer = ask(server, "POST", action, MANAGER, form(**fields))
        assert status == 200 and "Reading SnapTrade now" in answer and shown in answer
        assert woken.is_set()


# ── Linking, on the Account links tab ───────────────────────────────────────


def links_sent(sidecar: Sidecar) -> list[Any]:
    return sidecar.sent("LinkExternalAccount")


def row_of(body: str, external_id: str) -> str:
    """One account's row in the plain forms shown without the kit."""
    body = flat(body)
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
    account, and, for a deployment admin, one to create an account for any."""
    body = flat(body)
    start = body.index('<div class="panel-body panel-section"><h3>Link an account</h3>')
    return body[start : body.index("</om-account-map>", start)]


def map_tag(body: str) -> str:
    """The om-account-map element's opening tag, with its attributes."""
    tag = re.search(r"<om-account-map [^>]*>", body)
    assert tag is not None
    return tag.group(0)


def map_of(body: str) -> dict[str, Any]:
    """The JSON declared inside om-account-map, which the kit's map reads."""
    data = re.search(
        r'<om-account-map [^>]*>\s*<script type="application/json">(.*?)</script>', body
    )
    assert data is not None
    parsed: dict[str, Any] = json.loads(data.group(1))
    return parsed


def nothing_linked_or_read(sidecar: Sidecar) -> bool:
    return links_sent(sidecar) == [] and sidecar.sent("ReadAccountsForLinking") == []


def post_link(port: int, caller: str = MANAGER, **fields: str) -> tuple[int, str, str]:
    """The map's one form route, with the caller's token."""
    return ask(port, "POST", LINK, caller, form(csrf=token_of(caller), **fields))


def test_after_a_restart_every_link_is_named_from_the_first_delivery(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, MANAGER)
    assert map_of(body)["links"] == [
        {"external_account_id": ALPACA, "account_id": "ACC-1", "account_name": "Household"},
        {"external_account_id": IBKR, "account_id": "ACC-3", "account_name": "Spare"},
    ]
    assert "Linked to Household (<code>ACC-1</code>)." in row_of(body, ALPACA)
    assert "Linked to Spare (<code>ACC-3</code>)." in row_of(body, IBKR)
    assert "2 linked, 1 not linked." in body
    assert links_sent(sidecar) == []


def test_a_link_made_elsewhere_reaches_the_page_with_the_next_delivery(
    server: int, sidecar: Sidecar, loop: asyncio.AbstractEventLoop
) -> None:
    assert "Not linked" in row_of(ask(server, "GET", ACCOUNTS, MANAGER)[2], ALPACA)
    loop.call_soon_threadsafe(sidecar.set_link, ALPACA, "ACC-3", "Spare")
    for _ in range(100):
        row = row_of(ask(server, "GET", ACCOUNTS, MANAGER)[2], ALPACA)
        if "Linked to Spare" in row:
            break
        time.sleep(0.02)
    assert "Linked to Spare" in row and links_sent(sidecar) == []


def test_rows_recorded_do_not_make_an_account_linked(server: int) -> None:
    # The stand-in records every row; only the account scope says a link.
    _, _, body = ask(server, "GET", ACCOUNTS, MANAGER)
    row = row_of(body, ALPACA)
    assert '<span class="badge warn">Not linked</span>' in row and "Unlink" not in row
    assert map_of(body)["links"] == []
    assert "Not known" not in body and "not known" not in body


def test_a_linked_account_names_its_account_and_offers_unlink_and_another(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD[:1], loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, MANAGER)
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


def test_an_account_already_linked_is_offered_for_no_other(
    loop: asyncio.AbstractEventLoop,
) -> None:
    # One external account per account of the deployment's (v7): Household
    # holds Alpaca's, so it is offered to none, in the map or without it.
    sidecar, syncer = started_with(HELD[:1], loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, MANAGER)
    forms = fallback_of(body)
    assert '<option value="ACC-1">' not in forms
    assert '<option value="ACC-3">Spare</option>' in forms
    assert "An account already linked is not offered." in forms
    offered = {a["account_id"]: a["open"] for a in map_of(body)["accounts"]}
    # Still named, for the row linked to it; only not offered.
    assert offered == {"ACC-1": False, "ACC-3": True, "ACC-2": False}
    assert "Each of the deployment's accounts takes one at most" in body


def test_with_every_open_account_linked_none_is_offered(
    loop: asyncio.AbstractEventLoop,
) -> None:
    sidecar, syncer = started_with(HELD, loop)
    with serving(syncer, sidecar, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, MANAGER)
    forms = fallback_of(body)
    assert 'name="account_id"' not in forms
    assert "No open account of the deployment's is free to link." in forms


def test_a_second_link_to_an_account_is_refused_and_the_reason_shown(
    loop: asyncio.AbstractEventLoop,
) -> None:
    # The conductor's refusal, for an account the page did not offer (one
    # another plugin links, say, or one linked since the page was drawn).
    sidecar, syncer = started_with(HELD[1:], loop)
    with serving(syncer, sidecar, loop) as port:
        status, _, body = post_link(
            port, intent="link", external_account_id=ALPACA, account_id="ACC-3"
        )
    assert status == 200
    assert '<div class="notice bad"' in body
    assert (
        escaped(
            f"The sidecar refused this: ACC-3 already has external account {IBKR} linked "
            f"(snaptrade); an account has one external account: link {ALPACA} to another "
            "account, or a new one"
        )
        in body
    )
    assert links_sent(sidecar) == []
    assert '<span class="badge warn">Not linked</span>' in row_of(body, ALPACA)


def unlinked(name: str, params: Any) -> Exception | None:
    """The sidecar's refusal of a statement for the Alpaca account, which
    nothing links."""
    if name == "RecordHoldingsStatement" and params.external_account_id == ALPACA:
        return meridian.NotLinked(
            "RecordHoldingsStatement", f"external account {ALPACA} is not linked to an account"
        )
    return None


def test_an_unlinked_account_offers_an_existing_account_and_a_deployment_admin_a_new_one(
    loop: asyncio.AbstractEventLoop,
) -> None:
    refusing = Sidecar(refuse=unlinked)
    syncer = read_once(refusing, loop)
    with serving(syncer, refusing, loop) as port:
        _, _, body = ask(port, "GET", ACCOUNTS, ROOT)
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
    # And, for a deployment admin, one creates a new account for any of them,
    # named, held at and of the type the admin writes (W6.4).
    assert 'name="intent" value="create"' in create
    assert f'<option value="{ALPACA}">' in create
    assert 'name="new_account_name" required maxlength="200"' in create
    assert 'name="new_account_custodian" maxlength="200"' in create
    assert 'name="new_account_type" maxlength="200"' in create
    assert "Link it to an existing account, or create one for it." in body
    # The kit's map offers a deployment admin a new account too.
    assert "no-new-account" not in map_tag(body)
    (read,) = refusing.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(ROOT)
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


def test_a_plugin_admin_is_offered_no_new_account_and_sends_none(
    server: int, sidecar: Sidecar
) -> None:
    # W6.4: a plugin admin links to any existing account; only a deployment
    # admin names a new one.
    _, _, body = ask(server, "GET", ACCOUNTS, MANAGER)
    forms = fallback_of(body)
    assert 'name="intent" value="create"' not in forms and "new_account_name" not in forms
    assert "a deployment admin may also create one for it" in body
    # Nor does the kit's map (kit 0.7.1): no new account anywhere in it.
    assert " no-new-account " in map_tag(body)
    status, _, body = post_link(
        server, intent="create", external_account_id=ALPACA, new_account_name="Mine"
    )
    assert status == 200 and escaped(ONLY_DEPLOYMENT_ADMINS) in body
    assert links_sent(sidecar) == []


def test_linking_to_an_existing_account_is_sent_for_the_plugin_admin(
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
    assert sent.acting_for == assertion(MANAGER)
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
    sidecar, syncer = started_with(HELD[:1], loop)
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


def test_a_deployment_admin_creates_a_new_account_and_links_it_in_one_step(
    server: int, sidecar: Sidecar
) -> None:
    status, _, body = post_link(
        server,
        ROOT,
        intent="create",
        external_account_id=ALPACA,
        new_account_name="  Alpaca margin  ",
    )
    assert status == 200 and "Created Alpaca margin and linked Alpaca Margin to it." in body
    (sent,) = links_sent(sidecar)
    assert (sent.account_id, sent.new_account_name) == ("", "Alpaca margin")
    assert (sent.new_account_custodian, sent.new_account_type) == ("", "")
    assert sent.acting_for == assertion(ROOT)
    assert "Linked to Alpaca margin" in row_of(body, ALPACA)


def test_a_new_account_is_sent_with_the_custodian_and_type_the_admin_left(
    server: int, sidecar: Sidecar
) -> None:
    status, _, body = post_link(
        server,
        ROOT,
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
        ROOT,
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
    assert sent.acting_for == assertion(MANAGER)
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
    status, _, body = post_link(
        server, ROOT, intent=intent, external_account_id=ALPACA, **fields
    )
    assert status == 200 and said in body and links_sent(sidecar) == []


@pytest.mark.parametrize(
    "intent",
    [None, "", "delete", "LINK", " ", "link&intent=unlink"],
)
def test_a_bad_or_missing_intent_is_refused_and_links_nothing(
    server: int, sidecar: Sidecar, intent: str | None
) -> None:
    sent = form(csrf=token_of(MANAGER), external_account_id=ALPACA, account_id="ACC-1")
    if intent is not None:
        sent += f"&intent={intent}"
    status, _, body = ask(server, "POST", LINK, MANAGER, sent)
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
        fields = form(csrf=token_of(MANAGER), external_account_id=ALPACA, new_account_name="N")
        assert ask(server, "POST", f"{ACCOUNTS}/{gone}", MANAGER, fields)[0] == 404
    assert links_sent(sidecar) == []


def test_linking_is_only_under_manage(server: int, sidecar: Sidecar) -> None:
    # Even with a token that is theirs, a session under Open or View is
    # refused before anything is sent.
    for caller in (READER, WRITER):
        for intent in ("link", "create", "unlink", "link-several"):
            fields = form(
                csrf=token_of(caller),
                intent=intent,
                external_account_id=ALPACA,
                account_id="ACC-1",
                new_account_name="Mine",
            )
            status, _, body = ask(server, "POST", LINK, caller, fields)
            assert status == 403 and "is not served under" in body
    assert nothing_linked_or_read(sidecar)


# ── Several links in one form ────────────────────────────────────────────────


def post_several(
    port: int, pairs: Iterable[tuple[str, str]], caller: str = MANAGER
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
    _, _, body = ask(server, "GET", ACCOUNTS, MANAGER)
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
    assert all(s.acting_for == assertion(MANAGER) for s in sent)
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
    sent = f"csrf={quote(token_of(MANAGER))}&intent=link-several{pairs}"
    status, _, body = ask(server, "POST", LINK, MANAGER, sent)
    assert status == 400 and "do not pair up" in body
    assert links_sent(sidecar) == []


def test_several_links_take_a_form_of_thousands_of_pairs(
    server: int, sidecar: Sidecar, woken: asyncio.Event
) -> None:
    # Far over the 8 KB any other form here may be: pairs for accounts the
    # read did not reach, each said, none sent.
    many = [(f"broker:{n:06d}-{'x' * 36}", f"ACC-{n:06d}-{'y' * 30}") for n in range(3000)]
    status, _, body = post_several(server, [(ALPACA, "ACC-1"), *many])
    assert status == 200
    assert "Linked 1 of 3001 accounts; 3000 not linked:" in body
    assert [s.external_account_id for s in links_sent(sidecar)] == [ALPACA]
    # Anywhere else, a body that size is no form of this page's, and nothing
    # is done for it.
    woken.clear()
    for path in (READ, CONNECT, REFRESH, RECONNECT, LINK):
        padded = f"csrf={quote(token_of(MANAGER))}&intent=link&pad={'z' * 9000}"
        assert ask(server, "POST", path, MANAGER, padded)[0] == 413
    assert not woken.is_set() and len(links_sent(sidecar)) == 1


def test_a_body_over_the_pages_ceiling_is_refused_before_any_view(
    server: int, sidecar: Sidecar, woken: asyncio.Event
) -> None:
    # The several-link form's ceiling is the pages' own, Pages(max_body=...):
    # a larger body is answered 413 by the SDK, unread, and nothing is done.
    woken.clear()
    padded = f"csrf={quote(token_of(MANAGER))}&intent=link-several&pad={'z' * (1 << 20)}"
    status, _, body = ask(server, "POST", LINK, MANAGER, padded)
    assert (status, body) == (413, TOO_LARGE)
    assert links_sent(sidecar) == [] and not woken.is_set()


def test_the_page_without_the_kit_stays_one_list_however_many_accounts() -> None:
    # Each account is a row, and the choices are lists below them, not a
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
        return deployment_admins_page(status, ACCOUNTS, offered=offered)

    small, large = page_with(10, 10), page_with(1000, 1000)
    for shown in (small, large):
        assert fallback_of(shown).count("<select") == 3
    assert large.count("<option") == 3 * 1000 + 3
    assert len(large) < 100 * len(small)


def test_the_sidecars_refusal_is_shown_plainly(
    synced: Syncer, loop: asyncio.AbstractEventLoop
) -> None:
    said = "LinkExternalAccount is admitted only in a session at admin"

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
        _, _, body = ask(port, "GET", ACCOUNTS, ROOT)
    assert "The deployment's accounts could not be read: not an admin" in body
    forms = fallback_of(body)
    assert 'name="account_id"' not in forms and 'name="new_account_name"' in forms
    assert "none is offered here" in forms
    # The kit's map is told they could not be read.
    assert map_of(body)["accounts"] is None


# ── The CSRF token, the SDK's ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "sent",
    [
        None,
        "",
        "csrf=",
        "csrf=0123456789abcdef",
        # Another person's token.
        f"csrf={token_of(caller_header('admin', subject='person-2'))}",
        # Their own, from a session at another level.
        f"csrf={token_of(caller_header('read', subject='local|manager'))}",
        # A token under another secret, as a different process would make.
        f"csrf={meridian.Pages().csrf_token(meridian.Caller.from_header(MANAGER))}",
    ],
)
def test_a_post_without_the_right_token_is_refused_and_does_nothing(
    server: int,
    synced: Syncer,
    sidecar: Sidecar,
    recording: Recording,
    woken: asyncio.Event,
    sent: str | None,
) -> None:
    known = synced.status.connections[0].connection_id
    rest = (
        f"&external_account_id={quote(ALPACA)}&account_id=ACC-1&new_account_name=N"
        f"&connection_id={quote(known)}"
    )
    for path, intent in (
        (CONNECT, ""),
        (REFRESH, ""),
        (RECONNECT, ""),
        (READ, ""),
        # The map's one route, whatever the form says it means.
        (LINK, "&intent=link"),
        (LINK, "&intent=create"),
        (LINK, "&intent=unlink"),
        (LINK, "&intent=link-several"),
    ):
        status, _, body = ask(server, "POST", path, MANAGER, (sent or "") + rest + intent)
        assert status == 403 and "expired" in body
    assert recording.asked == [] and nothing_linked_or_read(sidecar)
    assert not woken.is_set()


def test_a_token_is_one_persons_in_one_sessions_level() -> None:
    first = meridian.Caller.from_header(caller_header("admin", subject="person-1"))
    again = meridian.Caller.from_header(
        caller_header("admin", subject="person-1", display_name="Renamed")
    )
    other = meridian.Caller.from_header(caller_header("admin", subject="person-2"))
    viewing = meridian.Caller.from_header(caller_header("read", subject="person-1"))
    assert pages.csrf_token(first) == pages.csrf_token(again)
    assert len({pages.csrf_token(c) for c in (first, other, viewing)}) == 3


# ── What is drawn ────────────────────────────────────────────────────────────


def test_what_snaptrade_says_is_escaped() -> None:
    hostile = ConnectionView(
        "c1", "<script>alert(1)</script>", "Broker & Co", "read", SyncState.CURRENT, "", None
    )
    shown = page_for(Status(mode="snaptrade", connections=(hostile,)))
    assert "<script>alert" not in shown and "&lt;script&gt;" in shown
    assert "Broker &amp; Co" in shown


def test_the_portal_link_opens_outside_the_frame(server: int, synced: Syncer) -> None:
    synced.venue = Recording(portal="https://portal.example/x?a=1&b=2")
    status, _, body = ask(server, "POST", CONNECT, MANAGER, form(csrf=token_of(MANAGER)))
    assert status == 200
    assert 'href="https://portal.example/x?a=1&amp;b=2" target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_waiting_for_settings_names_them_by_label_without_values() -> None:
    status = Status(mode="waiting", missing=("snaptrade_consumer_key",))
    assert "Consumer key" in page_for(status)
    # Statements does not name settings; it says SnapTrade is not read yet.
    shown = page_for(status, STATEMENTS, READER, meridian.AccountScope(links=HELD))
    assert "Consumer key" not in shown and "SnapTrade is not being read yet." in shown


def all_pages() -> dict[str, str]:
    """Each page, as each of its levels is served it, on the synthetic read."""
    status = synthetic_status()
    scope = meridian.AccountScope(links=HELD)
    return {
        "connections": deployment_admins_page(status, CONNECTIONS, scope),
        "accounts": deployment_admins_page(status, ACCOUNTS, scope),
        "statements-open": page_for(status, STATEMENTS, WRITER, scope),
        "statements-view": page_for(status, STATEMENTS, READER, scope),
        "nothing": page_for(status, STATEMENTS, NOBODY, scope),
        "raw-open": page_for(status, RAW, WRITER, scope, raw=kept_raw()),
        "raw-view": page_for(status, RAW, READER, scope, raw=kept_raw()),
        "raw-nothing": page_for(status, RAW, NOBODY, scope, raw=kept_raw()),
    }


def kept_raw() -> RawStore:
    """A store holding the synthetic read's raw responses."""
    store = RawStore(Path(tempfile.mkdtemp()) / "raw")
    syncer = Syncer(Sidecar().plugin(), now=clock(), raw=store)
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    return store


def test_each_page_is_built_on_the_kits_base_template_with_no_style_of_its_own() -> None:
    for name, shown in all_pages().items():
        assert f'<link rel="stylesheet" href="/.meridian/ui/{KIT}/meridian.css">' in shown
        assert f'<script src="/.meridian/ui/{KIT}/meridian.js"></script>' in shown
        # One stylesheet, the kit's; no style, colour or chrome of the page's.
        assert shown.count('rel="stylesheet"') == 1, name
        assert "<style" not in shown and "style=" not in shown, name
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", shown), name
        # No script of its own: the kit's, and data declared for its components.
        scripts = re.findall(r"<script[^>]*>", shown)
        assert set(scripts) <= {
            f'<script src="/.meridian/ui/{KIT}/meridian.js">',
            '<script type="application/json">',
        }, name
        assert "customElements" not in shown
        # The frame draws the rest; the kit drops the base's heading and tabs
        # when the page is framed.
        for chrome in ("<img", "<svg", "<footer", "data-om-mode", "theme"):
            assert chrome not in shown, (name, chrome)
        assert '<main class="page">' in shown and "<h1>SnapTrade</h1>" in shown


def test_the_tab_row_on_its_own_is_the_sessions_levels() -> None:
    shown = all_pages()
    tabs = {
        name: re.findall(r'<a class="tab[^"]*" href="([^"]+)"', page)
        for name, page in shown.items()
    }
    # Under Manage, its two tabs; under Open and View, its two others.
    assert tabs["connections"] == tabs["accounts"] == [CONNECTIONS, ACCOUNTS]
    assert tabs["statements-open"] == tabs["statements-view"] == [STATEMENTS, RAW]
    assert tabs["raw-open"] == tabs["raw-view"] == [STATEMENTS, RAW]
    assert 'class="tab on" href="/admin/accounts" aria-current="page"' in shown["accounts"]


def test_the_kit_is_the_one_that_draws_refresh_as_an_icon() -> None:
    # 0.8.0: a header action marked data-om-icon="refresh" drawn as a circular
    # arrow, by the dashboard beside the status dot (0.7.0's header status,
    # 0.6.0's om-status and 0.5.0's map are in it too: a 0.x release only adds).
    assert KIT == "0.8.0" and pages.kit == "/.meridian/ui/0.8.0/"


def grid_element(shown: str, grid_id: str) -> str:
    element = re.search(rf'<om-grid id="{grid_id}" [^>]*>(.*?)</om-grid>', shown, re.S)
    assert element is not None
    return element.group(0)


def grid(shown: str, grid_id: str) -> dict[str, list[dict[str, Any]]]:
    """The columns and rows declared inside a grid, which the kit's grid reads."""
    data = re.search(
        r'^<om-grid [^>]*>\s*<script type="application/json">(.*?)</script>',
        grid_element(shown, grid_id),
    )
    assert data is not None
    parsed: dict[str, list[dict[str, Any]]] = json.loads(data.group(1))
    return parsed


def statements_of_both() -> str:
    """Statements for a reader of both accounts the synthetic read records."""
    status = synthetic_status()
    both = caller_header("read", read=("ACC-1", "ACC-3"), subject="person-6")
    return page_for(status, STATEMENTS, both, meridian.AccountScope(links=HELD))


def test_each_table_is_in_the_html_until_the_kits_grid_replaces_it() -> None:
    shown = statements_of_both()
    for grid_id in ("rows-0", "rows-1"):
        element = grid_element(shown, grid_id)
        # Cards where it is narrow.
        assert element.startswith(f'<om-grid id="{grid_id}" row-key="key" narrow="cards"')
        assert "<table>" in element
        data = grid(shown, grid_id)
        # As many rows in the table a browser shows as the grid is given.
        assert element.count("<tr>") == len(data["rows"]) + 1
        assert len({row["key"] for row in data["rows"]}) == len(data["rows"])


def test_a_kind_snaptrade_gave_none_is_said_by_its_column_with_no_script() -> None:
    # The kit's rich cells (kit 0.3.0): the column declares what an empty
    # value says, as plain JSON, and the row carries no invented word.
    status = synthetic_status()
    connections = tuple(
        replace(
            connection,
            accounts=tuple(
                replace(
                    view,
                    statement=replace(
                        view.statement,
                        holdings=(
                            replace(view.statement.holdings[0], kind=""),
                            *view.statement.holdings[1:],
                        ),
                    ),
                )
                if view.account.external_account_id == ALPACA and view.statement is not None
                else view
                for view in connection.accounts
            ),
        )
        for connection in status.connections
    )
    shown = page_for(
        replace(status, connections=connections),
        STATEMENTS,
        READER,
        meridian.AccountScope(links=HELD),
    )
    data = grid(shown, "rows-0")
    kind = next(column for column in data["columns"] if column["key"] == "kind")
    assert kind == {"key": "kind", "label": "Kind", "type": "text", "blank": "not given"}
    assert data["rows"][0]["kind"] == ""
    assert all(row["kind"] for row in data["rows"][1:])
    # Without the kit, the same words, faint.
    faint = '<td><span class="faint">not given</span></td>'
    assert grid_element(shown, "rows-0").count(faint) == 1


@pytest.mark.parametrize(
    ("state", "badge"),
    [
        (SyncState.CURRENT, '<span class="badge good">Current</span>'),
        (SyncState.DELAYED_BY_DESIGN, '<span class="badge info">Delayed by design</span>'),
        (SyncState.STALE, '<span class="badge warn">Stale</span>'),
        (SyncState.NEEDS_SIGN_IN, '<span class="badge bad">Needs sign-in</span>'),
        (SyncState.DISABLED, '<span class="badge bad">Disabled</span>'),
        (
            SyncState.HOLDINGS_UNAVAILABLE,
            '<span class="badge bad">Holdings unavailable</span>',
        ),
    ],
)
def test_each_accounts_sync_state_is_on_statements(state: SyncState, badge: str) -> None:
    account = ExternalAccount("broker:1", True, "Roth IRA", "", "Broker", "c1", "s1")
    view = AccountView(account, Freshness(state, None, None, "Said by SnapTrade."), None, "")
    connection = ConnectionView("c1", "n", "Broker", "read", state, "", None, (view,))
    status = Status(mode="snaptrade", connections=(connection,))
    scope = meridian.AccountScope(
        links=(meridian.LinkedExternalAccount("broker:1", "ACC-1", "Household"),)
    )
    shown = statement_of(page_for(status, STATEMENTS, READER, scope), "broker:1")
    assert badge in shown and '<span class="hint">Said by SnapTrade.</span>' in shown
    # No moment reported: said so, never guessed.
    assert shown.count('<span class="faint">not reported</span>') == 2
    # Nothing recorded on the last read.
    assert "No statement on the last read: nothing recorded" in shown


def test_a_connection_whose_holdings_are_unavailable_says_to_connect_another_way() -> None:
    """The ruled remedy (the product owner, 2026-09-28), not "wait": holdings
    will not arrive through this connection, however long it waits."""
    connection = ConnectionView(
        "c1",
        "n",
        "Broker",
        "read",
        SyncState.HOLDINGS_UNAVAILABLE,
        "The brokerage does not show this account's holdings to SnapTrade.",
        None,
    )
    shown = page_for(Status(mode="snaptrade", connections=(connection,)))
    assert '<span class="badge bad">Holdings unavailable</span>' in shown
    assert "Connect the account another way, or through another venue" in shown
    assert "Usually SnapTrade's to recover" not in shown


def test_a_stopped_statement_is_toned_bad_on_statements() -> None:
    status = synthetic_status()
    stopped = Outcome(rows=3, recorded=1, stopped="RecordHolding: refused: no")
    status = replace(status, outcomes={**status.outcomes, ALPACA: stopped})
    shown = page_for(status, STATEMENTS, READER, meridian.AccountScope(links=HELD))
    alpaca = statement_of(shown, ALPACA)
    assert '<span class="bad-ink">Stopped at 1 of 3 rows</span>' in alpaca
    assert '<span class="hint">RecordHolding: refused: no.</span>' in alpaca


def test_quantities_are_exact_decimal_strings_as_read() -> None:
    shown = statements_of_both()
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
    shown = page_for(status, ACCOUNTS)
    assert "<script>alert" not in shown
    assert map_of(shown)["external_accounts"][0]["name"] == hostile


def test_a_closed_account_is_not_offered() -> None:
    status = synthetic_status()
    offered = Links(Sidecar().plugin())
    read = asyncio.run(offered.offered(MANAGER))
    assert [(a.account_id, a.open) for a in read.accounts] == [
        ("ACC-1", True),
        ("ACC-3", True),
        ("ACC-2", False),
    ]
    # Read with where each is held and what it is (W6.4).
    household = read.accounts[0]
    assert (household.custodian, household.account_type) == ("Schwab", "Brokerage")
    shown = deployment_admins_page(status, ACCOUNTS, offered=read)
    assert "Household" in shown and '<option value="ACC-2">' not in shown
    # The kit's map is told it is closed, and offers only open ones.
    assert {a["account_id"]: a["open"] for a in map_of(shown)["accounts"]}["ACC-2"] is False


def test_the_links_are_the_account_scopes_and_nothing_else() -> None:
    # Linked, naming the account, or not linked: there is no third state.
    scope = meridian.AccountScope(links=HELD)
    assert link_of(scope, ALPACA) == LinkView(Link.LINKED, "ACC-1", "Household")
    assert link_of(scope, "broker:other") == LinkView(Link.UNLINKED)
    assert [state.value for state in Link] == ["linked", "unlinked"]


# ── The head's status dot, and adding a brokerage ────────────────────────────

READ_AT = datetime(2026, 9, 30, 13, 12, tzinfo=UTC)


def status_dot(body: str) -> str:
    """The head's om-status, whole: its attributes and the words inside it."""
    found = re.findall(r"<om-status [^>]*>[^<]*</om-status>", head_of(body))
    assert len(found) == 1, "one status dot, in the head"
    dot: str = found[0]
    return dot


@pytest.mark.parametrize(
    ("status", "dot"),
    [
        (
            Status(mode="snaptrade", read_at=READ_AT),
            "<om-status data-om-header "
            'state="ok" label="SnapTrade read" at="2026-09-30T13:12:00+00:00" '
            'at-label="Last read">SnapTrade read. Last read 2026-09-30 13:12 UTC.'
            "</om-status>",
        ),
        (
            Status(mode="synthetic", read_at=READ_AT),
            "<om-status data-om-header "
            'state="ok" label="Synthetic mode: built-in responses, not SnapTrade" '
            'at="2026-09-30T13:12:00+00:00" at-label="Last read">Synthetic mode: built-in '
            "responses, not SnapTrade. Last read 2026-09-30 13:12 UTC.</om-status>",
        ),
        (
            Status(mode="snaptrade", read_at=READ_AT, reading=True),
            "<om-status data-om-header "
            'state="busy" label="Reading SnapTrade" at="2026-09-30T13:12:00+00:00" '
            'at-label="Last read">Reading SnapTrade. Last read 2026-09-30 13:12 UTC.'
            "</om-status>",
        ),
        (
            Status(mode="snaptrade", reading=True),
            "<om-status data-om-header "
            'state="busy" label="Reading SnapTrade">Reading SnapTrade.</om-status>',
        ),
        (
            Status(mode="synthetic", reading=True),
            "<om-status data-om-header "
            'state="busy" label="Synthetic mode: reading built-in responses, '
            'not SnapTrade">Synthetic mode: reading built-in responses, not SnapTrade.'
            "</om-status>",
        ),
        (
            Status(
                mode="snaptrade", error="listing connections failed: no answer", reading=True
            ),
            "<om-status data-om-header "
            'state="busy" label="Reading SnapTrade" detail="The last read failed: '
            'listing connections failed: no answer">Reading SnapTrade. The last read failed: '
            "listing connections failed: no answer.</om-status>",
        ),
        (
            Status(mode="snaptrade", error="listing connections failed: HTTPError, HTTP 503"),
            "<om-status data-om-header "
            'state="error" label="The last read failed" detail="listing connections '
            'failed: HTTPError, HTTP 503">The last read failed. listing connections failed: '
            "HTTPError, HTTP 503.</om-status>",
        ),
        (
            Status(mode="waiting", missing=("snaptrade_client_id", "snaptrade_consumer_key")),
            "<om-status data-om-header "
            'state="error" label="Not reading SnapTrade" detail="Waiting for '
            'settings: Client ID, Consumer key.">Not reading SnapTrade. Waiting for settings: '
            "Client ID, Consumer key.</om-status>",
        ),
        (
            Status(),
            "<om-status data-om-header "
            'state="busy" label="Starting" detail="SnapTrade has not been read '
            'yet.">Starting. SnapTrade has not been read yet.</om-status>',
        ),
    ],
    ids=[
        "read",
        "read-synthetic",
        "reading-after-a-read",
        "reading-first",
        "reading-synthetic",
        "reading-after-a-failure",
        "failed",
        "waiting-for-settings",
        "starting",
    ],
)
def test_each_syncer_state_is_the_heads_status_dot(status: Status, dot: str) -> None:
    for body in (page_for(status, CONNECTIONS), page_for(status, ACCOUNTS)):
        assert status_dot(body) == dot
        assert "Reading SnapTrade." not in head_of(body).replace(dot, "")
        assert "<om-moment" not in head_of(body)


def test_statements_has_the_dot_without_naming_settings() -> None:
    waiting = Status(mode="waiting", missing=("snaptrade_client_id",))
    assert status_dot(page_for(waiting, STATEMENTS, READER)) == (
        "<om-status data-om-header "
        'state="error" label="Not reading SnapTrade" detail="SnapTrade is not being read '
        'yet.">Not reading SnapTrade. SnapTrade is not being read yet.</om-status>'
    )
    read = Status(mode="snaptrade", read_at=READ_AT)
    assert status_dot(page_for(read, STATEMENTS, WRITER)).startswith(
        '<om-status data-om-header state="ok" label="SnapTrade read"'
    )


def test_framed_the_head_leaves_nothing_under_the_dashboards_tabs() -> None:
    # Kit 0.7.0, framed: the dashboard draws the heading, the dot (marked
    # data-om-header) and Refresh (data-om-action) where the head has it, so
    # the head has nothing left to show and the kit drops it: the page starts
    # right under the tabs (the product owner, 2026-09-30). Connections keeps
    # its Refresh in the Connections card, so its head has none.
    status = Status(mode="synthetic", read_at=READ_AT)
    for body in (
        page_for(status, CONNECTIONS),
        page_for(status, ACCOUNTS),
        page_for(status, STATEMENTS, WRITER),
        page_for(status, STATEMENTS, READER),
        page_for(status, RAW, WRITER),
        page_for(status, RAW, READER),
    ):
        head = head_of(body)
        assert status_dot(body).startswith("<om-status data-om-header ")
        left = re.sub(r"<div><h1>[^<]*</h1></div>", "", head, count=1)
        left = left.replace(status_dot(body), "", 1)
        left = re.sub(
            r'<div class="actions"><form method="post" action="/read" class="inline">'
            r'(?:<input type="hidden" [^>]*>)*' + HEADER_REFRESH + r"</form></div>",
            "",
            left,
            count=1,
        )
        assert left.strip() == "", left


def test_the_status_dot_says_an_error_as_text_and_never_a_secret() -> None:
    hostile = Status(mode="snaptrade", error='reading "<b>positions</b>" failed & stopped')
    dot = status_dot(page_for(hostile))
    assert "<b>" not in dot
    said = "reading &#34;&lt;b&gt;positions&lt;/b&gt;&#34; failed &amp; stopped"
    assert f'detail="{said}"' in dot
    # The error a real read keeps is what was asked, the type and the status:
    # never a credential (test_sync's test_nothing_it_logs_or_reports...).
    assert "secret" not in dot


def test_add_and_refresh_are_the_connections_cards_actions_with_their_forms_and_token(
    server: int,
) -> None:
    _, _, body = ask(server, "GET", CONNECTIONS, MANAGER)
    # Not in the page head, which has no actions.
    head = head_of(body)
    assert f'action="{CONNECT}"' not in head and f'action="{READ}"' not in head
    assert "<button" not in head
    assert "Connect a brokerage" not in body and "Add brokerage" not in body
    # Side by side in the Connections card's own header, beside its heading:
    # Refresh, the kit's plain button, then + Add, its primary one, named for
    # what it adds.
    token = token_of(MANAGER)
    assert connections_card(body) == (
        f'<form method="post" action="{READ}" class="inline">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="back" value="{CONNECTIONS}">'
        '<button aria-label="Refresh everything from SnapTrade">Refresh</button></form>'
        f'<form method="post" action="{CONNECT}" class="inline">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<button class="primary" {ADD}</form>'
    )
