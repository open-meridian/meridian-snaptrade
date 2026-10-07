"""The plan-code link and the activity records' window, both read from the
plugin's settings (contract v14, W4.8, W6.11; the product owner's option A of
2026-10-05: a plugin sets none of its settings).

`plan_code_links` is a table setting -- an external account this plugin
reported, the plan's code, an instrument record -- which an admin enters in
the dashboard's Settings form; each row arrives with who changed it and when,
and an activity under a linked code is that record, naming them. The window
is the SDK's setting for the kind (contract v16), and a record is never
deleted within the history SnapTrade reported, which the Account links tab
says.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import meridian
import pytest
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.testing import PageClient
from meridian.v1 import sidecar_pb2

from snaptrade.linking import Links
from snaptrade.page import ACCOUNTS, hold, pages
from snaptrade.plan_codes import PlanCodeLink, changed_accounts, links_of
from snaptrade.raw import RawStore, Taken
from snaptrade.settings import (
    ACTIVITY_PAST,
    ACTIVITY_WINDOW,
    DECLARED,
    DEFAULT_ACTIVITY_RETENTION_DAYS,
    DELETED,
    PLAN_CODE_LINKS,
    SYNTHETIC,
    config_from,
)
from snaptrade.sync import Syncer

from conftest import NOW, Sidecar, clock

ALPACA = "ALPACA:SYN-ALP-1001"
IBKR = "INTERACTIVE-BROKERS-FLEX:SYN-IB-2002"
# The activity synthetic IBKR reinvested under the plan's own code.
PLAN_ACTIVITY = "00000000-0000-4000-8000-00000000f116"
ROW = {
    "account": IBKR,
    "code": "OQKR",
    "instrument": "INS-7",
    "changed_by": "local|ada",
    "changed_at": "2026-10-05T12:00:00.000000Z",
}


def test_the_links_are_a_table_setting_of_typed_columns() -> None:
    (declared,) = [s for s in DECLARED if s.name == PLAN_CODE_LINKS]
    sent = declared._declared()
    assert sent.type == sidecar_pb2.SETTING_TYPE_TABLE
    assert [(c.name, c.type, c.required) for c in sent.columns] == [
        ("account", sidecar_pb2.SETTING_COLUMN_TYPE_EXTERNAL_ACCOUNT, True),
        ("code", sidecar_pb2.SETTING_COLUMN_TYPE_TEXT, True),
        ("instrument", sidecar_pb2.SETTING_COLUMN_TYPE_INSTRUMENT, True),
    ]
    assert sent.most_rows == 200 and not sent.secret


def test_the_rows_delivered_are_the_links_with_who_and_when() -> None:
    (link,) = links_of([ROW, {"account": IBKR, "code": "", "instrument": "INS-8"}, "x"])
    assert link == PlanCodeLink(
        IBKR, "OQKR", "INS-7", "local|ada", "2026-10-05T12:00:00.000000Z"
    ), "a row missing a cell is no link, nothing guessed"
    assert link.person == "local|ada, 2026-10-05T12:00:00.000000Z"
    assert links_of("not rows") == ()
    config = config_from({PLAN_CODE_LINKS: [ROW], ACTIVITY_WINDOW: 3650})
    assert config.plan_codes == (link,)
    assert config.windows.activity.length == timedelta(days=3650)
    assert config_from({}).windows.activity.days == DEFAULT_ACTIVITY_RETENTION_DAYS
    assert config_from({ACTIVITY_WINDOW: 99_999}).windows.activity.days == 36500


def test_the_next_reads_activity_under_a_linked_code_is_that_record_naming_who(
    tmp_path: Path,
) -> None:
    """Linked in the settings before IBKR is linked, IBKR's first read's
    backfill reports the reinvestment under the plan's own code as the record
    the person chose, with who and when; unlinked, as the code reported."""
    linked = [meridian.LinkedExternalAccount(IBKR, "ACC-3", "Spare")]
    sidecar = Sidecar(links=linked)
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    syncer.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
    asyncio.run(syncer.run_once())
    (sent,) = [
        a
        for a in sidecar.sent("RecordActivity")
        if a.activity.external_activity_id == PLAN_ACTIVITY
    ]
    assert sent.activity.instrument_id == "INS-7"
    assert not sent.activity.HasField("instrument_as_reported")
    (provenance,) = sent.activity.provenance
    assert (provenance.field, provenance.person) == (
        "instrument_id",
        "local|ada, 2026-10-05T12:00:00.000000Z",
    )
    assert all(
        "OQKR" not in [i.value for i in r.identifiers]
        for r in sidecar.sent("ResolveIdentifier")
    )

    plain = Sidecar(links=linked)
    unlinked = Syncer(plain.plugin(), now=clock(), raw=RawStore(tmp_path / "plain"))
    unlinked.configure(config_from({SYNTHETIC: True}))
    asyncio.run(unlinked.run_once())
    (as_sent,) = [
        a
        for a in plain.sent("RecordActivity")
        if a.activity.external_activity_id == PLAN_ACTIVITY
    ]
    assert not as_sent.activity.instrument_id
    assert as_sent.activity.instrument_as_reported.code == "OQKR"


# ── A link set later re-resolves what the street holds (contract v15) ───────


def _re_resolutions(sidecar: Sidecar) -> list[Any]:
    return [
        r for r in sidecar.sent("ReResolveActivity") if r.external_activity_id == PLAN_ACTIVITY
    ]


def _held(sidecar: Sidecar) -> list[tuple[str, bytes]]:
    """The plan activity's resolutions as the street keeps them: as first
    recorded, then each re-resolution."""
    return sidecar.resolutions[("snaptrade", IBKR, PLAN_ACTIVITY)]


def test_when_a_row_was_changed_is_read_as_the_conductor_stamped_it() -> None:
    link = PlanCodeLink(IBKR, "OQKR", "INS-7", "local|ada", "2026-10-05T12:00:00.000001Z")
    assert link.changed_at_ns == 1_791_201_600_000_001_000
    for unread in ("", "yesterday", "2026-10-05T12:00:00"):
        assert PlanCodeLink(IBKR, "OQKR", "INS-7", "local|ada", unread).changed_at_ns == 0


def test_the_accounts_a_delivery_adds_or_changes_a_link_on() -> None:
    (link,) = links_of([ROW])
    alpaca = PlanCodeLink(ALPACA, "OQKR", "INS-7", "local|ada", ROW["changed_at"])
    moved = PlanCodeLink(IBKR, "OQKR", "INS-8", "local|bo", "2026-10-06T09:00:00.000000Z")
    assert changed_accounts((), (link,)) == {IBKR}
    assert changed_accounts((link,), (link,)) == set(), "a delivery again changes nothing"
    assert changed_accounts((link,), (link, alpaca)) == {ALPACA}
    assert changed_accounts((link,), (moved,)) == {IBKR}
    assert changed_accounts((link,), ()) == set(), "a row removed is no link"


def test_a_link_set_after_the_backfill_re_resolves_what_it_reported_naming_who_and_when(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """IBKR's backfill reported the reinvestment under OQKR as the code;
    the link set in the settings backfills IBKR again on the read it wakes,
    and the street, holding the activity already, keeps a re-resolution
    beside it: the record the person chose, who, and when the row says."""
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    assert not _re_resolutions(sidecar)
    assert _held(sidecar) == [("", b"")], "first recorded as the code reported"

    syncer.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
    with caplog.at_level(logging.INFO):
        asyncio.run(syncer.run_once())
    (sent,) = _re_resolutions(sidecar)
    assert sent.external_account_id == IBKR and sent.source == "snaptrade"
    assert sent.instrument_id == "INS-7"
    assert (sent.provenance.field, sent.provenance.person) == (
        "instrument_id",
        "local|ada, 2026-10-05T12:00:00.000000Z",
    )
    assert sent.provenance.kind == ops.PROVENANCE_KIND_SUPPLIED
    assert sent.resolved_at_ns == 1_791_201_600_000_000_000, "when the row was set"
    assert len(sidecar.sent("ReResolveActivity")) == 1, "only what the link resolves"
    assert [instrument for instrument, _ in _held(sidecar)] == ["", "INS-7"]
    assert syncer.status.activities[IBKR].re_resolved == 1
    assert f"backfilled {IBKR}: " in caplog.text
    assert (
        "0 recorded for the first time, 1 re-resolved through a plan-code link" in caplog.text
    )

    # The next read, and a delivery of the same settings again, re-resolve
    # nothing twice: the street answers already recorded.
    syncer.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
    asyncio.run(syncer.run_once())
    assert len(_held(sidecar)) == 2
    # Nor does a restart, whose backfill sends it again.
    restarted = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    restarted.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
    asyncio.run(restarted.run_once())
    assert len(_re_resolutions(sidecar)) == 2
    assert len(_held(sidecar)) == 2
    assert restarted.status.activities[IBKR].re_resolved == 0

    # The row changed to another record: re-resolved again, by who changed it.
    moved = {**ROW, "instrument": "INS-8", "changed_by": "local|bo",
             "changed_at": "2026-10-06T09:00:00.000000Z"}  # fmt: skip
    restarted.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [moved]}))
    asyncio.run(restarted.run_once())
    assert [instrument for instrument, _ in _held(sidecar)] == ["", "INS-7", "INS-8"]
    assert _re_resolutions(sidecar)[-1].provenance.person.startswith("local|bo, ")


def test_on_start_a_current_link_re_resolves_what_an_earlier_process_reported(
    tmp_path: Path,
) -> None:
    """Reported unresolved by a process before the link was set, the
    activity is re-resolved by the next process's first backfill."""
    sidecar = Sidecar()
    before = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    before.configure(config_from({SYNTHETIC: True}))
    asyncio.run(before.run_once())
    started = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    started.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
    asyncio.run(started.run_once())
    assert [instrument for instrument, _ in _held(sidecar)] == ["", "INS-7"]


def test_an_activity_first_recorded_through_the_link_is_not_re_resolved(
    tmp_path: Path,
) -> None:
    """Linked before IBKR's first read, the activity is recorded as the
    record; a restart's backfill sends the re-resolution, and the street
    answers already recorded: its first record names it already."""
    sidecar = Sidecar()
    for _ in range(2):
        syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
        syncer.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW]}))
        asyncio.run(syncer.run_once())
    assert len(_re_resolutions(sidecar)) == 1
    assert [instrument for instrument, _ in _held(sidecar)] == ["INS-7"]


def test_a_row_saying_no_time_re_resolves_nothing_and_says_why(tmp_path: Path) -> None:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    undated = {**ROW, "changed_at": "not a time"}
    syncer.configure(config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [undated]}))
    asyncio.run(syncer.run_once())
    assert not sidecar.sent("ReResolveActivity")
    assert syncer.status.activities[IBKR].skipped == [
        f"activity {PLAN_ACTIVITY} under OQKR is not re-resolved: its link's row says no time"
    ]


def test_no_page_sets_a_setting() -> None:
    """Option A: no route of the plugin's saves a setting, and the SDK it is
    built on offers none to call."""
    from meridian.operations import Operations

    assert not hasattr(Operations, "set_plugin_settings")
    for tool in pages.tools:
        assert "plan_code" not in tool.name and "retention" not in tool.name


# ── How long an activity's record is kept ───────────────────────────────────


def _activity_record(root: Path, traded: str, received: str) -> None:
    """One activity record, as `RawStore.keep_activity` writes it."""
    RawStore(root).keep_activity(
        Taken(
            ALPACA,
            {
                "external_account_id": ALPACA,
                "snaptrade_account_id": "a-1",
                "activity_id": f"A-{traded}",
                "read_at": received,
                "source": "synthetic",
                "calls": [
                    {"call": "activities", "request": "", "body": {"trade_date": traded}}
                ],
            },
        )
    )


def test_a_record_is_never_deleted_within_the_history_reported(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    _activity_record(root, "2024-09-28T00:00:00.000Z", NOW.isoformat())
    store = RawStore(root)
    assert store.history_reach_days() == 731, "730 days and 15 hours, a part of a day as one"
    store.activity_window = timedelta(days=365)
    assert store.kept_for() == timedelta(days=731), "held to the history reported"
    store.activity_window = timedelta(days=3650)
    assert store.kept_for() == timedelta(days=3650)
    _activity_record(root, "2023-09-28T00:00:00.000Z", NOW.isoformat())
    assert RawStore(root).history_reach_days() == 1097
    files = list(root.rglob("*.json.gz"))
    assert files and all(json.loads(gzip.decompress(f.read_bytes())) for f in files)


def test_the_account_links_tab_says_what_the_settings_hold_and_how_long_records_are_kept(
    tmp_path: Path,
) -> None:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    _activity_record(tmp_path / "raw", "2024-09-28T00:00:00.000Z", NOW.isoformat())
    syncer.configure(
        config_from(
            {
                SYNTHETIC: True,
                PLAN_CODE_LINKS: [ROW],
                ACTIVITY_WINDOW: 365,
                ACTIVITY_PAST: DELETED,
            }
        )
    )
    asyncio.run(syncer.run_once())
    links = Links(sidecar.plugin(), settle_seconds=0)
    asyncio.run(links.hold(sidecar.scope()))
    hold(syncer, links, asyncio.Event())
    page = PageClient(pages, sidecar.plugin()).get(ACCOUNTS, "admin")
    assert page.status == 200
    assert "1 plan-code link;" in page.text and "0 cash links;" in page.text
    assert "in storage 365 days, then deleted, never within the 731 days" in page.text
    answered = PageClient(pages, sidecar.plugin()).call_tool(
        "read_account_links", level="admin"
    )
    assert answered.data["plan_codes"][0]["instrument_id"] == "INS-7"
    assert answered.data["activity_window_days"] == 365
    assert answered.data["activity_past_window"] == "deleted"
    assert answered.data["history_reach_days"] == 731
