"""SnapTrade's raw responses (raw.py): each read's kept per account, with
its time and each call by name, exactly as received and never a credential;
pruned past their retention when the settings arrive and after each read;
and the Raw responses tab, at write and read and never at admin, cut to the
accounts each person may read, each read formatted and downloadable as
JSON."""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import meridian
import pytest
from meridian.testing import PageClient
from meridian.v1 import sidecar_pb2

from snaptrade import synthetic
from snaptrade.linking import Links
from snaptrade.page import RAW, RAW_DOWNLOAD, READ, STATEMENTS, hold, pages
from snaptrade.raw import (
    DEFAULT_RETENTION_DAYS,
    REDACTED,
    RawStore,
    dumps,
    redact,
)
from snaptrade.settings import (
    CLIENT_ID,
    COMMERCIAL,
    CONSUMER_KEY,
    KEY_TYPE,
    RAW_RETENTION_DAYS,
    SYNTHETIC,
    USER_ID,
    USER_SECRET,
    Config,
    config_from,
)
from snaptrade.sync import Syncer
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import Json, VenueError, parse_exact

from conftest import NOW, Sidecar, clock

WRITE_LEVEL = sidecar_pb2.ACCESS_LEVEL_WRITE
READ_LEVEL = sidecar_pb2.ACCESS_LEVEL_READ

ALPACA = "ALPACA:SYN-ALP-1001"
IBKR = "INTERACTIVE-BROKERS-FLEX:SYN-IB-2002"
SCHWAB = f"snaptrade:{synthetic.SCHWAB_BROKERAGE}"
# Alpaca's to ACC-1, which the reader may read, and IBKR's to ACC-3, which
# they may not; Schwab's account nothing links.
HELD = (
    meridian.LinkedExternalAccount(ALPACA, "ACC-1", "Household"),
    meridian.LinkedExternalAccount(IBKR, "ACC-3", "Spare"),
)

SECRET = "user-secret-77aa-not-real"
CONSUMER = "KEY-not-real-consumer"
CLIENT = "CLIENT-not-real"
GIVEN: dict[str, str | int | bool] = {
    KEY_TYPE: COMMERCIAL,
    CLIENT_ID: CLIENT,
    CONSUMER_KEY: CONSUMER,
    USER_ID: "u-1",
    USER_SECRET: SECRET,
}


def store_at(root: Path, days: int = DEFAULT_RETENTION_DAYS) -> RawStore:
    return RawStore(root / "raw", timedelta(days=days))


def reading(
    store: RawStore,
    moment: Any = NOW,
    settings: dict[str, str | int | bool] | None = None,
    venue: Any = None,
    sidecar: Sidecar | None = None,
) -> Syncer:
    """A syncer keeping into `store`, configured and read once at `moment`."""
    syncer = Syncer(
        (sidecar or Sidecar()).plugin(),
        now=clock(moment),
        make_venue=(lambda config: venue) if venue is not None else None,
        raw=store,
    )
    syncer.configure(config_from(settings if settings is not None else {SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    return syncer


def calls_of(store: RawStore, external_id: str) -> dict[str, Json]:
    latest = store.latest(external_id)
    assert latest is not None
    return {str(call["call"]): call for call in latest.calls}


def kept_text(store: RawStore) -> str:
    """Every record under the store, decompressed, as one text."""
    return "\n".join(
        gzip.decompress(file.read_bytes()).decode()
        for file in sorted(store.root.rglob("*.json.gz"))
    )


# ── What each read keeps ────────────────────────────────────────────────────


def test_each_read_keeps_each_accounts_responses_with_its_time_and_calls(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    reading(store)
    venue = SyntheticVenue(clock())
    positions = asyncio.run(venue.positions(synthetic.ALPACA_MARGIN))
    balances = asyncio.run(venue.balances(synthetic.ALPACA_MARGIN))
    for external_id in (ALPACA, IBKR, SCHWAB):
        [read] = store.reads(external_id)
        assert read.read_at == NOW
    record = store.latest(ALPACA)
    assert record is not None and record.read_at == NOW and record.synthetic
    assert [(c["call"], c["request"]) for c in record.calls] == [
        ("listing connections", "GET /authorizations"),
        ("listing accounts", "GET /accounts"),
        ("reading positions", "GET /accounts/{accountId}/positions/all"),
        ("reading balances", "GET /accounts/{accountId}/balances"),
    ]
    calls = calls_of(store, ALPACA)
    # As SnapTrade answered, read exactly: the same values, Decimals and all.
    assert calls["reading positions"]["body"] == positions
    assert calls["reading balances"]["body"] == balances
    assert record.document["read_at"] == NOW.isoformat()
    assert record.document["snaptrade_account_id"] == synthetic.ALPACA_MARGIN


def test_a_list_call_is_kept_as_this_accounts_own_entry(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store)
    calls = calls_of(store, ALPACA)
    assert calls["listing accounts"]["body"]["id"] == synthetic.ALPACA_MARGIN
    assert calls["listing accounts"]["note"].startswith("this account's entry")
    assert calls["listing connections"]["body"]["id"] == synthetic.ALPACA
    # A record holds one account's data and no other's.
    record = store.latest(ALPACA)
    assert record is not None
    text = dumps(record.document)
    for other in (synthetic.IBKR_INDIVIDUAL, synthetic.SCHWAB_BROKERAGE, "SYN-IB-2002"):
        assert other not in text


def test_numbers_are_kept_as_snaptrade_wrote_them() -> None:
    body = parse_exact('{"a": 0.012345678, "b": 1.50, "c": 1e5, "d": 12, "e": "12.5"}')
    assert dumps(body) == '{"a":0.012345678,"b":1.50,"c":1E+5,"d":12,"e":"12.5"}'
    assert parse_exact(dumps(body)) == body
    assert isinstance(parse_exact(dumps(body))["b"], Decimal)
    assert dumps({"x": [1, {"y": None}]}, indent=2) == (
        '{\n  "x": [\n    1,\n    {\n      "y": null\n    }\n  ]\n}'
    )
    assert dumps([]) == "[]" and dumps({}) == "{}"


class PositionsFail(SyntheticVenue):
    """Synthetic SnapTrade whose positions for IBKR's account cannot be read."""

    async def positions(self, account_id: str) -> Json:
        if account_id == synthetic.IBKR_INDIVIDUAL:
            raise VenueError("reading positions")
        return await super().positions(account_id)


def test_a_call_that_failed_is_kept_as_why_and_the_rest_as_answered(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    reading(store, venue=PositionsFail(clock()))
    calls = calls_of(store, IBKR)
    assert "reading positions" in calls and "reading balances" not in calls
    assert calls["reading positions"]["failed"] == "reading positions failed: no answer"
    assert "body" not in calls["reading positions"]
    assert "body" in calls_of(store, ALPACA)["reading positions"]


def test_each_read_adds_a_record_the_newest_first(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store, NOW - timedelta(hours=1))
    reading(store, NOW)
    # Two reads at the one moment are two records.
    reading(store, NOW)
    reads = store.reads(ALPACA)
    assert [r.read_at for r in reads] == [NOW, NOW, NOW - timedelta(hours=1)]
    assert len({r.key for r in reads}) == 3
    assert reads[0].key.endswith("-2")


def test_a_record_is_written_whole(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store)
    files = list(store.root.rglob("*"))
    assert not [f for f in files if f.name.endswith(".tmp")]
    kept = [f for f in files if f.is_file()]
    assert len(kept) == 3 and all(f.name.endswith(".json.gz") for f in kept)
    # No path is made from an account's ID.
    assert all(re.fullmatch(r"a-[0-9a-f]{32}", f.parent.name) for f in kept)
    # What a write that stopped leaves is gone at the next prune.
    stray = kept[0].parent / "half.tmp"
    stray.write_bytes(b"{")
    store.prune(NOW)
    assert not stray.exists()


# ── Retention ───────────────────────────────────────────────────────────────


def test_the_retention_is_a_setting_thirty_days_by_default_and_at_least_one() -> None:
    assert config_from({}).raw_retention == timedelta(days=30)
    assert config_from({RAW_RETENTION_DAYS: 7}).raw_retention == timedelta(days=7)
    assert config_from({RAW_RETENTION_DAYS: 0}).raw_retention == timedelta(days=1)
    assert Config().raw_retention == timedelta(days=30)


def test_reads_past_the_retention_are_pruned_after_each_read(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store, NOW - timedelta(days=31))
    reading(store, NOW - timedelta(days=29))
    assert len(store.reads(ALPACA)) == 2
    reading(store, NOW)
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW, NOW - timedelta(days=29)]


def test_reads_past_the_retention_are_pruned_on_start(tmp_path: Path) -> None:
    reading(store_at(tmp_path), NOW - timedelta(days=40))
    store = store_at(tmp_path)
    syncer = Syncer(Sidecar().plugin(), now=clock(), raw=store)
    # The settings' first delivery, as the plugin starts: before any read.
    syncer.configure(config_from({SYNTHETIC: True}))
    assert store.reads(ALPACA) == []
    assert list(store.root.iterdir()) == []


def test_a_shorter_retention_prunes_at_once(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store, NOW - timedelta(days=2))
    syncer = Syncer(Sidecar().plugin(), now=clock(), raw=store)
    syncer.configure(config_from({SYNTHETIC: True}))
    assert len(store.reads(ALPACA)) == 1
    syncer.configure(config_from({SYNTHETIC: True, RAW_RETENTION_DAYS: 1}))
    assert store.retention == timedelta(days=1)
    assert store.reads(ALPACA) == []


# ── Never a credential ──────────────────────────────────────────────────────


class Hostile(SyntheticVenue):
    """Synthetic SnapTrade answering with credentials in its bodies, as no
    real answer should: by a credential's name, and by value in a string."""

    async def positions(self, account_id: str) -> Json:
        body = await super().positions(account_id)
        return {
            **body,
            "userSecret": SECRET,
            "meta": {"consumer_key": "anything", "access_token": "t-1", "note": SECRET},
            "echo": f"https://api.snaptrade.com/x?userSecret={SECRET}&clientId={CLIENT}",
            "signature": CONSUMER,
        }


def test_no_credential_is_ever_kept(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store, settings=GIVEN, venue=Hostile(clock()))
    text = kept_text(store)
    assert text
    for secret in (SECRET, CONSUMER, CLIENT, "t-1"):
        assert secret not in text
    for file in store.root.rglob("*"):
        if file.is_file():
            assert SECRET.encode() not in file.read_bytes()
    body = calls_of(store, ALPACA)["reading positions"]["body"]
    assert body["userSecret"] == REDACTED and body["signature"] == REDACTED
    assert body["meta"] == {
        "consumer_key": REDACTED,
        "access_token": REDACTED,
        "note": REDACTED,
    }
    assert (
        body["echo"] == f"https://api.snaptrade.com/x?userSecret={REDACTED}&clientId={REDACTED}"
    )
    # Not a credential: the connection an account belongs to stays.
    entry = calls_of(store, ALPACA)["listing accounts"]["body"]
    assert entry["brokerage_authorization"] == synthetic.ALPACA
    # The record is not real data mistaken for SnapTrade's.
    assert store.latest(ALPACA) is not None


def test_redacting_leaves_the_original_as_it_was() -> None:
    body = {"userSecret": "s", "list": [{"Authorization": "Bearer x", "n": 1}]}
    assert redact(body) == {
        "userSecret": REDACTED,
        "list": [{"Authorization": REDACTED, "n": 1}],
    }
    assert body["userSecret"] == "s"
    # A credential too short to be one is not looked for in ordinary text.
    assert redact({"x": "abc"}, ["abc"]) == {"x": "abc"}


def test_a_record_holds_no_header_query_or_credential_field(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    reading(store, settings=GIVEN, venue=SyntheticVenue(clock()))
    record = store.latest(ALPACA)
    assert record is not None
    assert set(record.document) == {
        "external_account_id",
        "snaptrade_account_id",
        "read_at",
        "source",
        "calls",
    }
    assert record.document["source"] == "snaptrade" and not record.synthetic
    for call in record.calls:
        assert set(call) <= {"call", "request", "note", "body", "failed"}
        assert "?" not in str(call["request"])


def test_a_store_that_cannot_be_written_leaves_the_read_as_it_was(tmp_path: Path) -> None:
    blocked = tmp_path / "raw"
    blocked.write_text("a file where the directory would be")
    store = RawStore(blocked)
    sidecar = Sidecar()
    syncer = reading(store, sidecar=sidecar)
    assert store.failure == "the raw responses were not kept: NotADirectoryError"
    assert sidecar.sent("RecordHoldingsStatement")
    assert syncer.status.error == ""


# ── The Raw responses tab ───────────────────────────────────────────────────


def serving(
    store: RawStore, links: tuple[meridian.LinkedExternalAccount, ...] = HELD
) -> Syncer:
    """The pages holding a read kept into `store` and `links` as the plugin's."""
    sidecar = Sidecar()
    syncer = reading(store, sidecar=sidecar)
    held = Links(sidecar.plugin())
    asyncio.run(held.hold(meridian.AccountScope(links=links)))
    hold(syncer, held, asyncio.Event())
    return syncer


def reader(**accounts: Any) -> PageClient:
    return PageClient(pages, Sidecar().plugin(), **accounts)


def section_of(body: str, external_id: str) -> str:
    start = body.index(f'<section class="panel" data-account="{external_id}">')
    return body[start : body.index("</section>", start)]


def test_raw_responses_is_a_tab_under_open_and_view_and_never_manage(tmp_path: Path) -> None:
    serving(store_at(tmp_path))
    declared = {page.path: (page.title, tuple(page.levels)) for page in pages.declared}
    assert declared[RAW] == ("Raw responses", (WRITE_LEVEL, READ_LEVEL))
    client = reader(read={"ACC-1"}, write={"ACC-1"})
    served = {(r.page.path, r.level): r.response.status for r in client.every_page()}
    assert served[(RAW, "write")] == served[(RAW, "read")] == 200
    assert served[(RAW, "admin")] == 403
    # Its download is no Manage route either.
    assert client.get(RAW_DOWNLOAD, "admin", account=ALPACA).status == 403
    assert client.get(RAW_DOWNLOAD, "read", account=ALPACA).status == 200


def test_manage_shows_no_raw_response(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    serving(store)
    body = calls_of(store, ALPACA)["reading positions"]["body"]
    held = {
        "reading positions",
        "positions/all",
        "0.012345678",
        "SYN-LOT-1",
        dumps(body, indent=2),
    }
    reader(read={"ACC-1"}).assert_no_account_data(*sorted(held))
    PageClient(pages, Sidecar().plugin(), deployment_admin=True).assert_no_account_data(
        *sorted(held)
    )


def test_a_reader_sees_the_raw_responses_of_the_accounts_they_may_read_alone(
    tmp_path: Path,
) -> None:
    serving(store_at(tmp_path))
    client = reader(read={"ACC-1"})
    answer = client.get(RAW, "read")
    assert answer.status == 200
    body = answer.text
    assert "<title>Raw responses · SnapTrade</title>" in body
    assert f'data-account="{ALPACA}"' in body
    for hidden in (IBKR, SCHWAB, "SYN-IB-2002", synthetic.IBKR_INDIVIDUAL, "SAP.DE"):
        assert hidden not in body
    # Nor by asking for it: the same answer as for no such account.
    for other in (IBKR, SCHWAB, "nothing-at-all"):
        assert client.get(RAW, "read", account=other).status == 404
        assert client.get(RAW_DOWNLOAD, "read", account=other).status == 404


def test_the_latest_read_is_shown_formatted_with_when_and_from_which_call(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    serving(store)
    body = reader(read={"ACC-1"}).get(RAW, "read").text
    shown = section_of(body, ALPACA)
    assert '<time datetime="2026-09-28T15:00:00+00:00">2026-09-28 15:00 UTC</time>' in shown
    assert "synthetic, not SnapTrade" in shown
    for call, request in (
        ("listing connections", "GET /authorizations"),
        ("listing accounts", "GET /accounts"),
        ("reading positions", "GET /accounts/{accountId}/positions/all"),
        ("reading balances", "GET /accounts/{accountId}/balances"),
    ):
        assert f"<strong>{call}</strong> <code>{request}</code>" in shown
    positions = calls_of(store, ALPACA)["reading positions"]["body"]
    formatted = dumps(positions, indent=2).replace('"', "&#34;")
    assert f"<pre><code>{formatted}</code></pre>" in shown
    assert "&#34;units&#34;: &#34;0.012345678&#34;" in shown
    # Synthetic data says so.
    assert "Synthetic mode: every figure here is invented" in body


def test_older_reads_within_the_retention_are_listed_and_each_opened(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    earlier = NOW - timedelta(days=3)
    reading(store, earlier)
    serving(store)
    client = reader(read={"ACC-1"})
    shown = section_of(client.get(RAW, "read").text, ALPACA)
    assert "Older reads kept: 1" in shown
    [older] = [r for r in store.reads(ALPACA) if r.read_at == earlier]
    assert f'href="/raw?account={ALPACA.replace(":", "%3A")}&amp;read={older.key}"' in shown
    opened = section_of(client.get(RAW, "read", account=ALPACA, read=older.key).text, ALPACA)
    assert "2026-09-25 15:00 UTC" in opened and "Older reads kept: 1" in opened
    # A read no longer kept, or no read at all, is said so; no path is followed.
    for gone in ("20200101T000000.000000Z", "../../etc/passwd", "x"):
        answer = client.get(RAW, "read", account=ALPACA, read=gone)
        assert answer.status == 200 and "That read is no longer kept" in answer.text
        assert client.get(RAW_DOWNLOAD, "read", account=ALPACA, read=gone).status == 404


def test_older_reads_are_paged(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for hours in range(25, 0, -1):
        reading(store, NOW - timedelta(hours=hours))
    serving(store)
    client = reader(read={"ACC-1"})
    first = client.get(RAW, "read", account=ALPACA).text
    assert "1 to 20 of 25, newest first." in first and "older=20" in first
    second = client.get(RAW, "read", account=ALPACA, older="20").text
    assert "21 to 25 of 25, newest first." in second and "older=0" in second


def test_a_read_downloads_as_its_json(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    serving(store)
    client = reader(read={"ACC-1"})
    answer = client.get(RAW_DOWNLOAD, "read", account=ALPACA)
    assert answer.status == 200
    assert answer.content_type == "application/json; charset=utf-8"
    record = store.latest(ALPACA)
    assert record is not None
    headers = dict(answer.headers)
    assert headers["content-disposition"] == (
        f'attachment; filename="snaptrade-raw-ALPACA-SYN-ALP-1001-{record.key}.json"'
    )
    assert headers["cache-control"] == "no-store"
    downloaded = parse_exact(answer.text)
    assert downloaded == record.document
    assert json.loads(answer.text)["external_account_id"] == ALPACA
    # The page links it.
    shown = section_of(client.get(RAW, "read").text, ALPACA)
    linked = f"/raw/download?account={ALPACA.replace(':', '%3A')}&amp;read={record.key}"
    assert f'href="{linked}" download' in shown


def test_somebody_with_nothing_to_read_is_told_so(tmp_path: Path) -> None:
    serving(store_at(tmp_path))
    body = reader().get(RAW, "read").text
    assert "Nothing here for you" in body and "data-account" not in body
    # Nor does a deployment admin read any by being one.
    body = PageClient(pages, Sidecar().plugin(), deployment_admin=True).get(RAW, "read").text
    assert "Nothing here for you" in body


def test_a_kept_read_is_shown_before_the_next_read_reaches_the_account(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    reading(store)
    # After a restart, before any read: the links name it, the store has it.
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=store)
    links = Links(sidecar.plugin())
    asyncio.run(links.hold(meridian.AccountScope(links=HELD)))
    hold(syncer, links, asyncio.Event())
    shown = section_of(reader(read={"ACC-1"}).get(RAW, "read").text, ALPACA)
    assert "Not reached by the last read" in shown and "reading positions" in shown


def test_refresh_is_offered_under_open_and_answers_with_the_tab(tmp_path: Path) -> None:
    serving(store_at(tmp_path))
    client = reader(read={"ACC-1"}, write={"ACC-1"})
    assert 'data-om-action="refresh"' in client.get(RAW, "write").text
    assert 'data-om-action="refresh"' not in client.get(RAW, "read").text
    # Sent from the tab, which marks it as the tab it answers with.
    answer = client.post(READ, "write", {"back": RAW}, headers={"Referer": f"http://p{RAW}"})
    assert answer.status == 200 and "Reading SnapTrade now" in answer.text
    assert "<title>Raw responses · SnapTrade</title>" in answer.text
    assert f'data-account="{ALPACA}"' in answer.text
    answer = client.post(READ, "write", {"back": STATEMENTS})
    assert "Reading SnapTrade now" in answer.text and "data-call" not in answer.text


def test_a_storage_failure_is_said_on_the_tab(tmp_path: Path) -> None:
    blocked = tmp_path / "raw"
    blocked.write_text("a file where the directory would be")
    serving(RawStore(blocked))
    body = reader(read={"ACC-1"}).get(RAW, "read").text
    assert "On the last read, the raw responses were not kept: NotADirectoryError." in body


@pytest.mark.parametrize("level", ["write", "read"])
def test_the_tab_is_built_on_the_kit_with_no_style_of_its_own(
    tmp_path: Path, level: str
) -> None:
    serving(store_at(tmp_path))
    body = reader(read={"ACC-1"}, write={"ACC-1"}).get(RAW, level).text
    assert "<style" not in body and "style=" not in body
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", body)
    assert set(re.findall(r"<script[^>]*>", body)) <= {
        '<script src="/.meridian/ui/0.8.0/meridian.js">'
    }
