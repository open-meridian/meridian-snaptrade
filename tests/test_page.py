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
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import meridian
import pytest
from meridian.v1 import sidecar_pb2

from snaptrade.linking import Links, LinkView, Offered, link_of
from snaptrade.normalise import (
    AccountView,
    ConnectionView,
    ExternalAccount,
    Freshness,
    SyncState,
)
from snaptrade.page import (
    ACCOUNTS,
    ADMIN_PAGES,
    CONNECTIONS,
    KIT,
    STATEMENTS,
    CsrfTokens,
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


@pytest.fixture
def server(
    synced: Syncer, sidecar: Sidecar, loop: asyncio.AbstractEventLoop, woken: asyncio.Event
) -> Iterator[int]:
    running = serve(synced, Links(sidecar.plugin()), loop, 0, woken, TOKENS)
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


def test_the_accounts_tab(server: int, sidecar: Sidecar) -> None:
    status, _, body = ask(server, "GET", ACCOUNTS, ADMIN)
    assert status == 200
    assert "Link each account" in body and ALPACA in body and "no stable ID" in body
    assert 'om-grid id="accounts"' in body
    # The deployment's accounts are read for the admin viewing the page.
    (read,) = sidecar.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(ADMIN)


def test_holdings_are_no_longer_an_admin_page(server: int) -> None:
    assert ask(server, "GET", "/admin/holdings", ADMIN)[0] == 404


# ── Statements, the user side ────────────────────────────────────────────────


def link_from_the_page(port: int, external_id: str, account_id: str) -> None:
    fields = form(csrf=token_of(ADMIN), external_account_id=external_id, account_id=account_id)
    assert ask(port, "POST", f"{ACCOUNTS}/link", ADMIN, fields)[0] == 200


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
    # Not ACC-3's, which they may not read, nor one whose link is not known.
    assert IBKR not in body and "IBKR Individual" not in body
    assert "Schwab Brokerage" not in body
    # Nothing to do here: no form, and nothing asked of the sidecar for them.
    assert "<form" not in body and 'name="csrf"' not in body
    reads = [params.acting_for for params in sidecar.sent("ReadAccountsForLinking")]
    assert assertion(READER) not in reads


def test_an_account_whose_link_is_not_known_is_not_shown_to_a_reader(server: int) -> None:
    # Its rows were recorded, so it is linked; but to which account, this
    # plugin cannot say (sdk-contract/a-plugin-reads-its-own-links).
    status, _, body = ask(server, "GET", STATEMENTS, READER)
    assert status == 200 and "No statements yet" in body
    assert "An account appears once it is linked to yours." in body
    for name in NAMES:
        assert name not in body
    assert "om-grid" not in body


def test_an_account_unlinked_from_the_page_leaves_the_readers_view(server: int) -> None:
    link_from_the_page(server, ALPACA, "ACC-1")
    assert "Alpaca Margin" in ask(server, "GET", STATEMENTS, READER)[2]
    fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA)
    assert ask(server, "POST", f"{ACCOUNTS}/unlink", ADMIN, fields)[0] == 200
    assert "Alpaca Margin" not in ask(server, "GET", STATEMENTS, READER)[2]


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
    known = synced.status.connections[0].connection_id
    path = f"{CONNECTIONS}/{known}/refresh"
    status, _, body = ask(server, "POST", path, ADMIN, f"csrf={token}")
    assert status == 200 and "nothing was asked of SnapTrade" in body
    assert recording.asked == ["portal None", f"refresh {known}"]
    assert tokens_on(body) == {token}
    unknown = f"{CONNECTIONS}/not-one-of-ours/refresh"
    assert ask(server, "POST", unknown, ADMIN, f"csrf={token}")[0] == 404


def test_reading_now_answers_with_the_tab_it_was_asked_from(
    server: int, woken: asyncio.Event
) -> None:
    token = token_of(ADMIN)
    _, _, body = ask(server, "POST", "/admin/read", ADMIN, form(csrf=token, back=ACCOUNTS))
    assert "Reading SnapTrade now" in body and "Link each account" in body
    _, _, body = ask(server, "POST", "/admin/read", ADMIN, form(csrf=token, back="/elsewhere"))
    assert "Brokerage connections" in body
    assert woken.is_set()


# ── Linking, on the Accounts tab ─────────────────────────────────────────────


def links_sent(sidecar: Sidecar) -> list[Any]:
    return sidecar.sent("LinkExternalAccount")


def row_of(body: str, external_id: str) -> str:
    """One account's row in the account mapping."""
    start = body.index(f'<div class="list-row" data-account="{external_id}">')
    ends = [
        found
        for found in (
            body.find('<div class="list-row"', start + 1),
            body.find("</section>", start),
        )
        if found != -1
    ]
    return body[start : min(ends)]


def nothing_linked_or_read(sidecar: Sidecar) -> bool:
    return links_sent(sidecar) == [] and sidecar.sent("ReadAccountsForLinking") == []


def test_an_account_the_read_recorded_is_linked_and_offers_unlink(server: int) -> None:
    _, _, body = ask(server, "GET", ACCOUNTS, ADMIN)
    row = row_of(body, ALPACA)
    assert '<span class="badge good">Linked</span>' in row
    assert "Its rows were recorded on the last read." in row


def unlinked(name: str, params: Any) -> Exception | None:
    """The sidecar's refusal of a row for the Alpaca account, which nothing links."""
    if name == "RecordHolding" and params.external_account_id == ALPACA:
        return meridian.CallFailed(
            "RecordHolding", "refused", f"external account {ALPACA} is not linked to an account"
        )
    return None


def test_an_unlinked_account_offers_an_existing_account_or_a_new_one(
    loop: asyncio.AbstractEventLoop,
) -> None:
    refusing = Sidecar(refuse=unlinked)
    syncer = read_once(refusing, loop)
    running = serve(syncer, Links(refusing.plugin()), loop, 0, asyncio.Event(), TOKENS)
    try:
        _, _, body = ask(running.server_address[1], "GET", ACCOUNTS, ADMIN)
    finally:
        running.shutdown()
    row = row_of(body, ALPACA)
    assert '<span class="badge warn">Not linked</span>' in row
    assert "refused on the last read" in row
    # The picker holds the deployment's open accounts, and nothing closed,
    # each with its custodian and type beside its name where it has them.
    assert '<option value="ACC-1">Household (Schwab, Brokerage)</option>' in row
    assert '<option value="ACC-3">Spare</option>' in row and "Retired" not in row
    assert 'action="/admin/accounts/link"' in row and 'name="account_id"' in row
    # A new account, named from the external one, editable.
    assert 'action="/admin/accounts/create"' in row
    assert 'name="new_account_name" value="Alpaca Margin"' in row
    # Its custodian from the connection's brokerage, and its type from the
    # venue's account type, both editable (W6.4).
    assert 'name="new_account_custodian" value="Alpaca" maxlength="200"' in row
    assert 'name="new_account_type" value="margin" maxlength="200"' in row
    assert "Unlink" not in row
    (read,) = refusing.sent("ReadAccountsForLinking")
    assert read.acting_for == assertion(ADMIN)


def test_linking_to_an_existing_account_is_sent_for_the_admin(
    server: int, sidecar: Sidecar, woken: asyncio.Event
) -> None:
    fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA, account_id="ACC-1")
    status, _, body = ask(server, "POST", f"{ACCOUNTS}/link", ADMIN, fields)
    assert status == 200 and "Linked Alpaca Margin." in body
    (sent,) = links_sent(sidecar)
    assert (sent.external_account_id, sent.account_id, sent.new_account_name) == (
        ALPACA,
        "ACC-1",
        "",
    )
    assert sent.acting_for == assertion(ADMIN)
    assert "Linked to Household. Linked from this page." in row_of(body, ALPACA)
    # A read follows, so the account's rows are recorded.
    assert woken.is_set()


def test_creating_a_new_account_names_it_and_links_in_one_step(
    server: int, sidecar: Sidecar
) -> None:
    fields = form(
        csrf=token_of(ADMIN), external_account_id=ALPACA, new_account_name="  Alpaca margin  "
    )
    status, _, body = ask(server, "POST", f"{ACCOUNTS}/create", ADMIN, fields)
    assert status == 200 and "Created Alpaca margin and linked Alpaca Margin to it." in body
    (sent,) = links_sent(sidecar)
    assert (sent.account_id, sent.new_account_name) == ("", "Alpaca margin")
    assert (sent.new_account_custodian, sent.new_account_type) == ("", "")
    assert sent.acting_for == assertion(ADMIN)


def test_a_new_account_is_sent_with_the_custodian_and_type_the_admin_left(
    server: int, sidecar: Sidecar
) -> None:
    fields = form(
        csrf=token_of(ADMIN),
        external_account_id=ALPACA,
        new_account_name="Alpaca margin",
        new_account_custodian=" Alpaca Securities ",
        new_account_type="Margin",
    )
    status, _, body = ask(server, "POST", f"{ACCOUNTS}/create", ADMIN, fields)
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
    fields = form(
        csrf=token_of(ADMIN),
        external_account_id=ALPACA,
        account_id="ACC-1",
        new_account_custodian="Alpaca",
        new_account_type="margin",
    )
    status, _, _ = ask(server, "POST", f"{ACCOUNTS}/link", ADMIN, fields)
    assert status == 200
    (sent,) = links_sent(sidecar)
    assert (sent.account_id, sent.new_account_custodian, sent.new_account_type) == (
        "ACC-1",
        "",
        "",
    )


def test_unlinking_sends_neither_account_nor_name(server: int, sidecar: Sidecar) -> None:
    fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA)
    status, _, body = ask(server, "POST", f"{ACCOUNTS}/unlink", ADMIN, fields)
    assert status == 200 and "Unlinked Alpaca Margin." in body
    (sent,) = links_sent(sidecar)
    assert (sent.external_account_id, sent.account_id, sent.new_account_name) == (
        ALPACA,
        "",
        "",
    )
    assert sent.acting_for == assertion(ADMIN)
    row = row_of(body, ALPACA)
    assert "Not linked" in row and "Unlinked from this page." in row


@pytest.mark.parametrize(
    ("action", "fields", "said"),
    [
        ("link", {"account_id": ""}, "Choose the account to link it to."),
        ("create", {"new_account_name": "   "}, "Name the new account."),
    ],
)
def test_a_form_missing_its_choice_asks_for_it_and_sends_nothing(
    server: int, sidecar: Sidecar, action: str, fields: dict[str, str], said: str
) -> None:
    sent = form(csrf=token_of(ADMIN), external_account_id=ALPACA, **fields)
    status, _, body = ask(server, "POST", f"{ACCOUNTS}/{action}", ADMIN, sent)
    assert status == 200 and said in body and links_sent(sidecar) == []


def test_only_an_account_the_read_reached_is_linked(server: int, sidecar: Sidecar) -> None:
    for external_id in ("not-one-of-ours", ""):
        fields = form(csrf=token_of(ADMIN), external_account_id=external_id, account_id="ACC-1")
        assert ask(server, "POST", f"{ACCOUNTS}/link", ADMIN, fields)[0] == 404
    fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA)
    assert ask(server, "POST", f"{ACCOUNTS}/delete", ADMIN, fields)[0] == 404
    assert links_sent(sidecar) == []


def test_the_sidecars_refusal_is_shown_plainly(
    synced: Syncer, loop: asyncio.AbstractEventLoop
) -> None:
    said = "LinkExternalAccount is admitted only acting for a deployment admin"

    def refuse(name: str, params: Any) -> Exception | None:
        if name == "LinkExternalAccount":
            return meridian.CallFailed("LinkExternalAccount", "refused", said)
        return None

    refusing = Sidecar(refuse=refuse)
    running = serve(synced, Links(refusing.plugin()), loop, 0, asyncio.Event(), TOKENS)
    try:
        fields = form(csrf=token_of(ADMIN), external_account_id=ALPACA, account_id="ACC-1")
        status, _, body = ask(
            running.server_address[1], "POST", f"{ACCOUNTS}/link", ADMIN, fields
        )
    finally:
        running.shutdown()
    assert status == 200
    assert (
        f'<div class="notice bad" role="alert">The sidecar refused this: {said}</div>' in body
    )
    # Still as the last read left it.
    assert "Its rows were recorded on the last read." in row_of(body, ALPACA)


def test_when_the_deployments_accounts_cannot_be_read_only_a_new_one_is_offered(
    loop: asyncio.AbstractEventLoop,
) -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "ReadAccountsForLinking":
            return meridian.CallFailed("ReadAccountsForLinking", "refused", "not an admin")
        return unlinked(name, params)

    refusing = Sidecar(refuse=refuse)
    syncer = read_once(refusing, loop)
    running = serve(syncer, Links(refusing.plugin()), loop, 0, asyncio.Event(), TOKENS)
    try:
        _, _, body = ask(running.server_address[1], "GET", ACCOUNTS, ADMIN)
    finally:
        running.shutdown()
    assert "The deployment's accounts could not be read: not an admin" in body
    row = row_of(body, ALPACA)
    assert 'name="account_id"' not in row and 'name="new_account_name"' in row
    assert "none is offered here" in row


def test_linking_is_only_for_an_administrator(server: int, sidecar: Sidecar) -> None:
    # Even with a token that is theirs, somebody else is refused before
    # anything is sent.
    for action in ("link", "create", "unlink"):
        fields = form(
            csrf=token_of(PERSON),
            external_account_id=ALPACA,
            account_id="ACC-1",
            new_account_name="Mine",
        )
        status, _, body = ask(server, "POST", f"{ACCOUNTS}/{action}", PERSON, fields)
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
    for path in (
        "/admin/connect",
        f"{CONNECTIONS}/{known}/refresh",
        f"{CONNECTIONS}/{known}/reconnect",
        "/admin/read",
        f"{ACCOUNTS}/link",
        f"{ACCOUNTS}/create",
        f"{ACCOUNTS}/unlink",
    ):
        status, _, body = ask(server, "POST", path, ADMIN, (sent or "") + rest)
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


def links_for(status: Status) -> dict[str, LinkView]:
    return {
        view.account.external_account_id: link_of(
            None, status.outcomes.get(view.account.external_account_id)
        )
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


def grid(shown: str, grid_id: str) -> dict[str, list[dict[str, object]]]:
    data = re.search(
        rf'<script type="application/json" id="{grid_id}-data">(.*?)</script>', shown
    )
    assert data is not None
    parsed: dict[str, list[dict[str, object]]] = json.loads(data.group(1))
    return parsed


def test_each_table_is_in_the_html_until_the_kits_grid_replaces_it() -> None:
    pages = synthetic_pages()
    for path, grid_id, row_key in (
        (ACCOUNTS, "accounts", "id"),
        (STATEMENTS, "rows-0", "key"),
        (STATEMENTS, "rows-1", "key"),
    ):
        shown = pages[path]
        element = re.search(
            rf'<om-grid id="{grid_id}" row-key="{row_key}"[^>]*>(.*?)</om-grid>', shown
        )
        assert element is not None and "<table>" in element.group(1)
        data = grid(shown, grid_id)
        # As many rows in the table a browser shows as the grid is given.
        assert element.group(1).count("<tr>") == len(data["rows"]) + 1
        assert len({row[row_key] for row in data["rows"]}) == len(data["rows"])
        # Set only once the kit has defined the grid.
        assert 'customElements.whenDefined("om-grid")' in shown


def test_the_accounts_grid_says_each_link() -> None:
    rows = grid(synthetic_pages()[ACCOUNTS], "accounts")["rows"]
    assert {row["link"] for row in rows} == {"Linked"}
    assert {row["link_tone"] for row in rows} == {"good"}


def test_quantities_are_exact_decimal_strings_as_read() -> None:
    shown = synthetic_pages()[STATEMENTS]
    rows = [row for index in range(2) for row in grid(shown, f"rows-{index}")["rows"]]
    quantities = {row["instrument"]: row["quantity"] for row in rows}
    assert all(isinstance(quantity, str) for quantity in quantities.values())
    # Crypto to nine decimals, a short, and cash as SnapTrade gave it.
    assert "0.012345678" in quantities.values() and "-40" in quantities.values()
    assert "200.00" in quantities.values()
    assert '<td class="num">0.012345678</td>' in shown


def test_the_grids_data_cannot_close_its_script() -> None:
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
    unlinked = {key: link_of("", None) for key in links_for(status)}
    shown = render_accounts(status, "t", unlinked, read)
    assert "Household" in shown and "Retired" not in shown


def test_an_account_whose_link_is_not_known_offers_linking_and_unlinking() -> None:
    status = synthetic_status()
    unknown = {key: link_of(None, None) for key in links_for(status)}
    offered = asyncio.run(Links(Sidecar().plugin()).offered(ADMIN))
    row = row_of(render_accounts(status, "t", unknown, offered), ALPACA)
    assert '<span class="badge">Not known</span>' in row
    assert 'name="account_id"' in row and 'name="new_account_name"' in row
    assert f'action="{ACCOUNTS}/unlink"' in row and "It may be linked already." in row
