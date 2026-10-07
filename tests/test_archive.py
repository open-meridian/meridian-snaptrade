"""An edge plugin's older records move to the archive (contract v16;
meridian-design spec/an-edge-plugins-older-records-move-to-the-archive):
SnapTrade's two kinds declared, its 0.12.0 retention settings no longer
declared (the admin sets the windows once at upgrade, option A of
2026-10-07), each unit past its window archived, kept or deleted as the
settings say -- recorded first, refused inside the hold, never twice after a
restart -- what storage holds on the heartbeat, and the Raw responses tab's
archive, restored through the SDK's route.

The moves run on the SDK's own helpers (conftest's Sidecar binds them), in a
storage and an archive made for each test, and only the move each reports
is answered by the stand-in, as the sidecar would."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import meridian
import pytest
from meridian.testing import PageClient, caller_header
from meridian.v1 import sidecar_pb2

from snaptrade.archive import Keeper
from snaptrade.declaration import DECLARATION, RESPONSES_KIND
from snaptrade.linking import Links
from snaptrade.page import RAW, RAW_ARCHIVE, RESTORE, hold, pages
from snaptrade.raw import ACTIVITY_DIRECTORY, LEDGER, RESTORE_AREA, RawStore, storage_root
from snaptrade.settings import (
    ACTIVITY_PAST,
    ACTIVITY_WINDOW,
    DECLARED,
    DELETED,
    KEPT,
    RESPONSES_PAST,
    RESPONSES_WINDOW,
    SYNTHETIC,
    Window,
    config_from,
)
from snaptrade.sync import Syncer

from conftest import NOW, Sidecar, clock

ALPACA = "ALPACA:SYN-ALP-1001"
# The accounts synthetic SnapTrade reaches, each with its own day of reads.
ACCOUNTS = 4
HELD = (meridian.LinkedExternalAccount(ALPACA, "ACC-1", "Household"),)
ARCHIVED = sidecar_pb2.MOVE_OUTCOME_ARCHIVED
RESTORED = sidecar_pb2.MOVE_OUTCOME_RESTORED
DELETED_OUTCOME = sidecar_pb2.MOVE_OUTCOME_DELETED
# A day past the responses' window of 30, and one inside it.
OLD = NOW - timedelta(days=40)
RECENT = NOW - timedelta(days=5)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    granted = tmp_path / "storage"
    granted.mkdir()
    monkeypatch.setenv("MERIDIAN_STORAGE_DIR", str(granted))
    monkeypatch.delenv("MERIDIAN_ARCHIVE_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_ARCHIVE_BUCKET", raising=False)
    return granted


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: Path) -> Path:
    given = tmp_path / "archive"
    given.mkdir()
    monkeypatch.setenv("MERIDIAN_ARCHIVE_DIR", str(given))
    return given


def syncer_at(
    sidecar: Sidecar,
    storage: Path,
    moment: datetime = NOW,
    values: dict[str, Any] | None = None,
    archive: bool = False,
) -> Syncer:
    """A syncer keeping into the granted storage, its settings delivered as
    the SDK delivers them."""
    syncer = Syncer(
        sidecar.plugin(),
        now=clock(moment),
        raw=RawStore(storage_root(storage), storage),
    )
    syncer.configure(sidecar.deliver({SYNTHETIC: True, **(values or {})}, archive))
    return syncer


def read_at(
    sidecar: Sidecar,
    storage: Path,
    moment: datetime,
    values: dict[str, Any] | None = None,
    archive: bool = False,
) -> Syncer:
    syncer = syncer_at(sidecar, storage, moment, values, archive)
    asyncio.run(syncer.run_once())
    return syncer


def responses_units(store: RawStore) -> list[str]:
    return [unit.unit for unit in store.units(RESPONSES_KIND.name)]


# ── Its two kinds, and their windows ────────────────────────────────────────


def test_its_two_kinds_are_declared_with_their_windows_and_archivable() -> None:
    assert DECLARATION.storage is not None
    assert [
        (k.name, k.label, k.window_days, k.archivable) for k in DECLARATION.storage.kinds
    ] == [
        ("activity", "Reported activity", 2555, True),
        ("responses", "Raw responses", 30, True),
    ]
    wire = DECLARATION.to_wire().storage
    assert [k.name for k in wire.record_kinds] == ["activity", "responses"]
    # The storage asked for stays the longest a window may be.
    assert wire.retention_days == 36500
    # The window settings are the SDK's, from the kinds: never declared here.
    made = [s.name for s in DECLARATION._window_settings(archive=True)]
    assert made == [ACTIVITY_WINDOW, ACTIVITY_PAST, RESPONSES_WINDOW, RESPONSES_PAST]
    assert made == [
        "activity_window_days",
        "activity_past_window",
        "responses_window_days",
        "responses_past_window",
    ]
    assert not {s.name for s in DECLARED} & set(made)


def test_each_window_is_read_from_the_sdks_settings() -> None:
    sidecar = Sidecar()
    windows = sidecar.deliver({}).windows
    assert windows.responses == Window(30, KEPT) and windows.activity == Window(2555, KEPT)
    # Archived by default where the instance has an archive: the SDK's default.
    assert sidecar.deliver({}, archive=True).windows.responses.past == "archived"
    set_ = sidecar.deliver({RESPONSES_WINDOW: 7, ACTIVITY_PAST: DELETED}).windows
    assert set_.responses == Window(7, KEPT) and set_.activity == Window(2555, DELETED)
    # Never past the SDK's bound, and a value not among the choices is kept.
    odd = config_from({RESPONSES_WINDOW: 0, ACTIVITY_WINDOW: 99_999, RESPONSES_PAST: "x"})
    assert odd.windows.responses == Window(1, KEPT)
    assert odd.windows.activity.days == 36500


def test_0_12_0s_retention_settings_are_dropped_the_windows_alone_count() -> None:
    """Option A (the product owner, 2026-10-07): 0.12.0's two settings are no
    longer declared, so the sidecar would not deliver a value saved under
    one, and one that arrived anyway counts for nothing; the windows are
    what the admin sets once at upgrade."""
    assert not {s.name for s in DECLARED} & {"raw_retention_days", "activity_retention_days"}
    sidecar = Sidecar()
    old = sidecar.deliver({"raw_retention_days": 90, "activity_retention_days": 3650}).windows
    assert old.responses == Window(30, KEPT)
    assert old.activity == Window(2555, KEPT)
    set_ = sidecar.deliver({RESPONSES_WINDOW: 90, ACTIVITY_WINDOW: 3650}).windows
    assert (set_.responses, set_.activity) == (Window(90, KEPT), Window(3650, KEPT))


# ── Past the window ─────────────────────────────────────────────────────────


def test_a_unit_past_its_window_is_archived_recorded_first_and_gone_from_storage(
    storage: Path, archive: Path
) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    old_key = RawStore(storage_root(storage), storage).reads(ALPACA)[0].key
    syncer = read_at(sidecar, storage, NOW, archive=True)
    store = syncer.raw
    assert store is not None
    # Each account's day of 40 days ago, archived by its window, as itself.
    moved = [(kind, outcome, rule) for kind, _, outcome, rule in sidecar.outcomes()]
    assert moved == [("responses", ARCHIVED, "responses_window_days 30")] * ACCOUNTS
    assert all(person == "" for _, person in sidecar.moves)
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW]
    assert store.record(ALPACA, old_key) is None
    [unit] = [m for m in store.moved(ALPACA)]
    assert unit.unit.startswith("raw-responses/a-") and unit.unit.endswith("/2026-08-19")
    assert (archive / unit.unit / f"{old_key}.json.gz").is_file()
    assert not (storage / unit.unit).exists()
    found = sidecar.plugin().find_record(f"{unit.unit}/{old_key}.json.gz")
    assert found is not None and found.outcome == ARCHIVED
    # A move counts what it holds and when: the read, once.
    [first] = [m for m, _ in sidecar.moves if m.unit == unit.unit]
    assert first.record_count == 1 and first.first_received_ns == first.last_received_ns


def test_a_restored_unit_is_read_back_and_its_restore_recorded_for_the_person(
    storage: Path, archive: Path
) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    store = read_at(sidecar, storage, NOW, archive=True).raw
    assert store is not None
    [unit] = store.moved(ALPACA)
    old_key = unit.files[0].removesuffix(".json.gz")
    person = caller_header("write", read={"ACC-1"}, write={"ACC-1"})
    restored = asyncio.run(
        sidecar.plugin().restore_unit("responses", unit.unit, for_caller=person)
    )
    # Where the SDK restores it is where the store reads it back from.
    assert restored == storage / RESTORE_AREA / unit.unit
    back = store.record(ALPACA, old_key)
    assert back is not None and back.restored and back.read_at == OLD
    assert [r.restored for r in store.reads(ALPACA)] == [False, True]
    assert store.find(f"{ALPACA}/{old_key}/positions") is not None
    move, who = sidecar.moves[-1]
    assert move.outcome == RESTORED and move.unit == unit.unit and who == person


def test_a_deletion_inside_the_hold_is_refused_and_the_unit_kept_on_every_pass(
    storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deleted past a window of a day, as an admin chose, and refused: the
    unit's last read is five days old, inside the hold of thirty. Kept, and
    nothing recorded; a restart's pass is refused again, recording nothing."""
    sidecar = Sidecar(hold_days=30)
    values = {RESPONSES_WINDOW: 1, RESPONSES_PAST: DELETED}
    read_at(sidecar, storage, RECENT, values)
    caplog.set_level(logging.WARNING, "snaptrade")
    store = read_at(sidecar, storage, NOW, values).raw
    assert store is not None
    assert sidecar.moves == []
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW, RECENT]
    said = [r for r in caplog.records if "kept inside the deployment's hold" in r.message]
    assert len(said) == ACCOUNTS
    restarted = syncer_at(sidecar, storage, NOW, values)
    tended = asyncio.run(restarted.tend())
    assert tended.held == ACCOUNTS and tended.deleted == 0 and sidecar.moves == []
    # Past the hold, deleted, the deletion recorded first, naming the rule.
    sidecar.hold_days = 3
    tended = asyncio.run(restarted.tend())
    assert tended.deleted == ACCOUNTS
    assert {(k, o, r) for k, _, o, r in sidecar.outcomes()} == {
        ("responses", DELETED_OUTCOME, "responses_past_window deleted")
    }
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW]
    assert _state_of(sidecar, store) == "deleted"


def _state_of(sidecar: Sidecar, store: RawStore) -> str:
    [unit] = store.moved(ALPACA)
    found = sidecar.plugin().find_record(unit.unit)
    assert found is not None
    return {ARCHIVED: "archived", RESTORED: "restored", DELETED_OUTCOME: "deleted"}[
        found.outcome
    ]


def test_a_restart_moves_nothing_twice(storage: Path, archive: Path) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    read_at(sidecar, storage, NOW, archive=True)
    recorded = list(sidecar.moves)
    assert len(recorded) == ACCOUNTS
    # A new process, the same storage: its pass finds nothing past the window.
    again = syncer_at(sidecar, storage, NOW, archive=True)
    tended = asyncio.run(again.tend())
    assert (tended.archived, tended.deleted, tended.failed) == (0, 0, 0)
    asyncio.run(again.run_once())
    assert sidecar.moves == recorded


def test_with_no_archive_or_kept_by_choice_a_unit_past_its_window_stays(
    storage: Path,
) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, {RESPONSES_PAST: "archived"})
    store = read_at(sidecar, storage, NOW, {RESPONSES_PAST: "archived"}).raw
    assert store is not None
    assert sidecar.moves == [] and len(store.reads(ALPACA)) == 2
    assert asyncio.run(
        syncer_at(sidecar, storage, NOW, {RESPONSES_PAST: "archived"}).tend()
    ).failed
    kept = syncer_at(sidecar, storage, NOW, {RESPONSES_PAST: KEPT})
    assert asyncio.run(kept.tend()).kept == ACCOUNTS and sidecar.moves == []


def test_what_storage_holds_of_each_kind_goes_on_the_heartbeat(
    storage: Path, archive: Path
) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    store = read_at(sidecar, storage, NOW, archive=True).raw
    assert store is not None
    activity, responses = sidecar.plugin().stored
    assert responses.record_kind == "responses" and responses.record_count == ACCOUNTS
    now_ns = int(NOW.timestamp()) * 10**9
    assert responses.first_received_ns == responses.last_received_ns == now_ns
    files = list((store.root).glob(f"a-*/{ACTIVITY_DIRECTORY}/*/*.json.gz"))
    assert activity.record_kind == "activity" and activity.record_count == len(files) > 0
    assert activity.first_received_ns == int(OLD.timestamp()) * 10**9
    beat = meridian.testing.heartbeat(healthy=True)
    beat.stored.extend(sidecar.plugin().stored)
    assert [s.record_kind for s in beat.stored] == ["activity", "responses"]


def test_the_heartbeat_reports_the_bytes_each_kind_uses_of_the_archive(
    storage: Path, archive: Path
) -> None:
    """StoredSpan.bytes (named 2026-10-07): the reads archived past their
    window are what the responses use of the archive, every account's day,
    as the SDK's index sums them; activity, none archived, none. The
    Summary draws them against the archive's bound."""
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    store = read_at(sidecar, storage, NOW, archive=True).raw
    assert store is not None
    on_archive = sum(f.stat().st_size for f in archive.rglob("*.json.gz"))
    assert on_archive > 0
    beat = {s.record_kind: s for s in sidecar.plugin()._stored_with_bytes()}
    assert beat["responses"].bytes == on_archive
    assert beat["responses"].record_count == ACCOUNTS, "storage's count stands"
    assert beat["activity"].bytes == 0
    # What the plugin set is its own, the bytes the SDK's alone.
    assert all(s.bytes == 0 for s in sidecar.plugin().stored)


def test_an_archive_past_its_bound_keeps_the_unit_and_says_so_once(
    storage: Path,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MERIDIAN_ARCHIVE_MOST_BYTES (W8.3): a unit that would take the archive
    past its bound is refused by the SDK before anything moves; the pass
    keeps it, says why once, and records nothing."""
    monkeypatch.setenv("MERIDIAN_ARCHIVE_MOST_BYTES", "1")
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    caplog.set_level(logging.WARNING, "snaptrade")
    store = read_at(sidecar, storage, NOW, archive=True).raw
    assert store is not None
    assert sidecar.moves == [] and list(archive.rglob("*.json.gz")) == []
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW, OLD]
    said = [r.message for r in caplog.records if "MERIDIAN_ARCHIVE_MOST_BYTES" in r.message]
    assert len(said) == ACCOUNTS and all("not archived, and kept" in m for m in said)
    again = syncer_at(sidecar, storage, NOW, archive=True)
    assert asyncio.run(again.tend()).failed == ACCOUNTS and sidecar.moves == []


def test_a_reported_activitys_unit_is_archived_whole_and_never_written_twice(
    storage: Path, archive: Path
) -> None:
    """Alpaca's backfill received 60 days ago, in July: past a window of 30
    its month is archived, and a row's activity key then resolves through the
    ledger to the archived unit; reported again, its record is not written
    again, as the first report stands."""
    sidecar = Sidecar()
    july = NOW - timedelta(days=60)
    values = {ACTIVITY_WINDOW: 30}
    first = read_at(sidecar, storage, july, values, archive=True).raw
    assert first is not None
    reported = {f.name for f in first.root.glob(f"a-*/{ACTIVITY_DIRECTORY}/2026-07/*.json.gz")}
    key = sidecar.sent("RecordActivity")[0].activity.raw_record.key
    store = read_at(sidecar, storage, NOW, values, archive=True).raw
    assert store is not None
    archived = [m.unit for m, _ in sidecar.moves if m.record_kind == "activity"]
    assert archived and {unit.rsplit("/", 1)[1] for unit in archived} == {"2026-07"}
    account, activity_id = key.split("/", 2)[1:]
    assert store.find(key) is None
    held = store.moved_activity(account, activity_id)
    assert held is not None and held.unit in archived
    # The read now reported every activity again: none was written again.
    again = {f.name for f in store.root.glob(f"a-*/{ACTIVITY_DIRECTORY}/2026-09/*.json.gz")}
    assert reported and not reported & again


def test_a_reported_activity_is_never_deleted_within_the_history_reported(
    storage: Path,
) -> None:
    """Deleted past a window of 30 days, as chosen, the July month waits: the
    history SnapTrade reported reaches back further than its age."""
    sidecar = Sidecar()
    values = {ACTIVITY_WINDOW: 30, ACTIVITY_PAST: DELETED}
    read_at(sidecar, storage, NOW - timedelta(days=60), values)
    store = read_at(sidecar, storage, NOW, values).raw
    assert store is not None
    assert store.history_reach_days() > 60
    assert not [m for m, _ in sidecar.moves if m.record_kind == "activity"]


def test_a_unit_whose_day_or_month_has_not_ended_is_never_moved(
    storage: Path, archive: Path
) -> None:
    """Records are written into today's day and this month's: neither moves,
    whatever the window, so nothing is written into a unit after it moved."""
    sidecar = Sidecar()
    values = {ACTIVITY_WINDOW: 1}
    read_at(sidecar, storage, NOW - timedelta(days=10), values, archive=True)
    read_at(sidecar, storage, NOW, values, archive=True)
    assert not [m for m, _ in sidecar.moves if m.record_kind == "activity"]


def test_a_unit_moved_before_holding_other_records_is_kept(
    storage: Path, archive: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    store = read_at(sidecar, storage, NOW, archive=True).raw
    assert store is not None
    [unit] = store.moved(ALPACA)
    # A record put under the archived day's path afterwards.
    read_at(sidecar, storage, OLD + timedelta(hours=1), archive=True)
    caplog.set_level(logging.WARNING, "snaptrade")
    before = len(sidecar.moves)
    keeper = Keeper(sidecar.plugin(), store, clock())
    asyncio.run(keeper.tend(config_from(sidecar._settings_now or {}).windows))
    assert len(sidecar.moves) == before
    assert (storage / unit.unit).is_dir()
    assert any(
        "holds a move of this unit with other records" in r.message for r in caplog.records
    )


# ── The Raw responses tab's archive ─────────────────────────────────────────


def serving(sidecar: Sidecar, syncer: Syncer) -> None:
    links = Links(sidecar.plugin())
    asyncio.run(links.hold(meridian.AccountScope(links=HELD)))
    hold(syncer, links, asyncio.Event())


def archived_and_served(storage: Path) -> tuple[Sidecar, RawStore, str]:
    sidecar = Sidecar()
    read_at(sidecar, storage, OLD, archive=True)
    syncer = read_at(sidecar, storage, NOW, archive=True)
    serving(sidecar, syncer)
    assert syncer.raw is not None
    [unit] = syncer.raw.moved(ALPACA)
    return sidecar, syncer.raw, unit.unit


def test_the_archive_lists_each_unit_moved_and_offers_a_restore_under_open(
    storage: Path, archive: Path
) -> None:
    sidecar, _, unit = archived_and_served(storage)
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1"}, write={"ACC-1"})
    under_view = client.get(RAW_ARCHIVE, "read").text
    assert f'data-unit="{unit}" data-state="archived"' in under_view
    assert "Responses</td><td>2026-08-19</td>" in under_view
    assert "Restoring is under Open" in under_view and RESTORE not in under_view
    # A view of Raw responses: its tab is the one marked, and the page titled
    # as it, though the archive is a route of its own.
    assert '<a class="tab on" href="/raw" aria-current="page">Raw responses</a>' in under_view
    assert "<title>Raw responses · SnapTrade</title>" in under_view
    under_open = client.get(RAW_ARCHIVE, "write").text
    assert f'<form method="post" action="{RESTORE}" class="inline">' in under_open
    assert '<input type="hidden" name="record_kind" value="responses">' in under_open
    assert f'<input type="hidden" name="unit" value="{unit}">' in under_open
    assert '<om-pager total="1" offset="0" size="1" size-param="limit">' in under_open
    # The archive is an account's data: never under Manage.
    assert client.get(RAW_ARCHIVE, "admin").status == 403
    # Only the accounts the person may read.
    assert (
        "data-unit"
        not in PageClient(pages, sidecar.plugin(), read={"ACC-3"}).get(RAW_ARCHIVE, "read").text
    )


def test_a_restore_through_the_sdks_route_is_recorded_and_read_back(
    storage: Path, archive: Path
) -> None:
    sidecar, store, unit = archived_and_served(storage)
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1"}, write={"ACC-1"})
    answer = client.post(
        RESTORE,
        "write",
        {"record_kind": "responses", "unit": unit},
        headers={"Referer": f"http://p{RAW_ARCHIVE}?account={ALPACA}"},
    )
    assert answer.status == 303 and dict(answer.headers)["location"] == RAW_ARCHIVE
    move, person = sidecar.moves[-1]
    assert (
        move.outcome == RESTORED
        and move.unit == unit
        and "Ada Park" in str(meridian.Caller.from_header(person).display_name)
    )
    listed = client.get(RAW_ARCHIVE, "read").text
    assert f'data-unit="{unit}" data-state="restored"' in listed and "until 2026-" in listed
    [old] = [r for r in store.reads(ALPACA) if r.restored]
    shown = client.get(RAW, "read", account=ALPACA, read=old.key).text
    assert "restored from the archive" in shown and "reading positions" in shown
    kept = client.get(RAW, "read", account=ALPACA, view="kept").text
    assert '<span class="badge good">Restored</span>' in kept
    # Under View, the route is refused before it runs.
    assert (
        client.post(RESTORE, "read", {"record_kind": "responses", "unit": unit}).status == 403
    )


def test_a_rows_reference_to_an_archived_record_resolves_archived_restorable(
    storage: Path, archive: Path
) -> None:
    sidecar, store, unit = archived_and_served(storage)
    old_key = unit.rsplit("/", 1)[1]  # the day
    [read] = [m for m in store.moved(ALPACA)]
    key = read.files[0].removesuffix(".json.gz")
    assert key.startswith(old_key.replace("-", ""))
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1"}, write={"ACC-1"})
    shown = client.get(RAW, "write", ref=f"{ALPACA}/{key}/positions").text
    assert "That record is in the archive, and restorable" in shown
    assert f'<input type="hidden" name="unit" value="{unit}">' in shown
    assert "That read is not in storage" not in shown


def test_the_archive_is_a_tool_naming_each_unit_for_restore_unit(
    storage: Path, archive: Path
) -> None:
    sidecar, _, unit = archived_and_served(storage)
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1"}, write={"ACC-1"})
    answered = client.call_tool("read_archived_units", {"account": ALPACA}, level="read")
    assert answered.status == 200 and answered.outcome == "unchanged"
    [listed] = answered.data["units"]
    assert (listed["record_kind"], listed["unit"], listed["state"]) == (
        "responses",
        unit,
        "archived",
    )
    assert listed["record_count"] == 1 and listed["first_received"].startswith("2026-08-19")
    refused = client.call_tool("read_archived_units", {"account": "nobody"}, level="read")
    assert refused.status != 200
    # The SDK's restore route, derived as a tool, restores what it names.
    restored = client.call_tool(
        "restore_unit", {"record_kind": "responses", "unit": unit}, level="write"
    )
    assert restored.status == 200 and restored.data["outcome"] == "restored"


def test_the_ledger_is_beside_the_units_and_no_unit(storage: Path, archive: Path) -> None:
    _, store, unit = archived_and_served(storage)
    ledger = storage / unit.rsplit("/", 1)[0] / LEDGER
    assert ledger.is_file()
    assert unit not in responses_units(store)
