"""The plan-code link and the activity records' retention, both read from the
plugin's settings (contract v14, W4.8, W6.11; the product owner's option A of
2026-10-05: a plugin sets none of its settings).

`plan_code_links` is a table setting -- an external account this plugin
reported, the plan's code, an instrument record -- which an admin enters in
the dashboard's Settings form; each row arrives with who changed it and when,
and an activity under a linked code is that record, naming them. The
retention is an ordinary setting, kept never shorter than the history
SnapTrade reported, which the Account links tab says.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import timedelta
from pathlib import Path

import meridian
from meridian.testing import PageClient
from meridian.v1 import sidecar_pb2

from snaptrade.linking import Links
from snaptrade.page import ACCOUNTS, hold, pages
from snaptrade.plan_codes import PlanCodeLink, links_of
from snaptrade.raw import RawStore, Taken
from snaptrade.settings import (
    ACTIVITY_RETENTION_DAYS,
    DECLARED,
    DEFAULT_ACTIVITY_RETENTION_DAYS,
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
    config = config_from({PLAN_CODE_LINKS: [ROW], ACTIVITY_RETENTION_DAYS: 3650})
    assert config.plan_codes == (link,)
    assert config.activity_retention == timedelta(days=3650)
    assert config_from({}).activity_retention == timedelta(days=DEFAULT_ACTIVITY_RETENTION_DAYS)
    assert config_from({ACTIVITY_RETENTION_DAYS: 99_999}).activity_retention.days == 36500


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


def test_a_record_is_kept_never_shorter_than_the_history_reported(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    _activity_record(root, "2024-09-28T00:00:00.000Z", NOW.isoformat())
    store = RawStore(root)
    assert store.history_reach_days() == 731, "730 days and 15 hours, a part of a day as one"
    store.activity_retention = timedelta(days=365)
    assert store.kept_for() == timedelta(days=731), "held to the history reported"
    store.activity_retention = timedelta(days=3650)
    assert store.kept_for() == timedelta(days=3650)
    store.activity_retention = timedelta(days=365)
    store.prune(NOW + timedelta(days=400))
    assert store.activity_record(ALPACA, "A-2024-09-28T00:00:00.000Z") is not None
    store.prune(NOW + timedelta(days=732))
    assert store.activity_record(ALPACA, "A-2024-09-28T00:00:00.000Z") is None
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
        config_from({SYNTHETIC: True, PLAN_CODE_LINKS: [ROW], ACTIVITY_RETENTION_DAYS: 365})
    )
    asyncio.run(syncer.run_once())
    links = Links(sidecar.plugin(), settle_seconds=0)
    asyncio.run(links.hold(sidecar.scope()))
    hold(syncer, links, asyncio.Event())
    page = PageClient(pages, sidecar.plugin()).get(ACCOUNTS, "admin")
    assert page.status == 200
    assert "1 plan-code link" in page.text
    assert "kept 731 days, not the 365 set" in page.text
    answered = PageClient(pages, sidecar.plugin()).call_tool(
        "read_account_links", level="admin"
    )
    assert answered.data["plan_codes"][0]["instrument_id"] == "INS-7"
    assert answered.data["kept_for_days"] == 731
