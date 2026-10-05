"""One read, end to end through the SDK's operations, in each mode."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from meridian import Figure
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.testing import heartbeat

from snaptrade.normalise import ConnectionView, SyncState
from snaptrade.settings import (
    CLIENT_ID,
    COMMERCIAL,
    CONSUMER_KEY,
    KEY_TYPE,
    SYNTHETIC,
    USER_ID,
    USER_SECRET,
    Config,
    config_from,
)
from snaptrade.sync import Status, Syncer, figures, venue_for
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import SnapTradeVenue, Venue, VenueError

from conftest import NOW, Sidecar, clock

SECRET = "user-secret-77aa-not-real"
GIVEN: dict[str, str | int | bool] = {
    KEY_TYPE: COMMERCIAL,
    CLIENT_ID: "CLIENT",
    CONSUMER_KEY: "KEY-not-real",
    USER_ID: "u-1",
    USER_SECRET: SECRET,
}


def syncer(sidecar: Sidecar, make_venue: Any = None) -> Syncer:
    return Syncer(sidecar.plugin(), now=clock(), make_venue=make_venue)


def test_the_venue_is_synthetic_snaptrade_or_nothing() -> None:
    assert isinstance(venue_for(config_from({SYNTHETIC: True, **GIVEN})), SyntheticVenue)
    assert venue_for(config_from({})) is None
    # With every credential, SnapTrade itself; its SDK is made, not called.
    assert isinstance(venue_for(config_from(GIVEN)), SnapTradeVenue)


async def test_without_credentials_it_refuses_to_read_and_says_what_it_needs() -> None:
    sidecar = Sidecar()
    made: list[Config] = []

    def nothing(config: Config) -> None:
        made.append(config)

    running = syncer(sidecar, make_venue=nothing)
    running.configure(config_from({}))
    status = await running.run_once()
    assert status.mode == "waiting" and len(made) == 1
    assert sidecar.calls == []
    ((healthy, detail),) = sidecar.reports
    assert not healthy
    # A personal key, the default, needs no user.
    assert detail == "waiting for settings: snaptrade_client_id, snaptrade_consumer_key"


async def test_synthetic_mode_records_every_account_it_can() -> None:
    sidecar = Sidecar()
    running = syncer(sidecar)
    running.configure(config_from({SYNTHETIC: True}))
    status = await running.run_once()
    assert status.mode == "synthetic" and status.error == ""
    assert len(sidecar.sent("ReportSyncStatus")) == 4
    (reported,) = sidecar.sent("ReportExternalAccounts")
    assert len(reported.accounts) == 4
    opened = sidecar.sent("RecordHoldingsStatement")
    assert len(opened) == 4
    rows = sidecar.sent("RecordHolding")
    assert len(rows) == sum(statement.expected_rows for statement in opened)
    assert all(outcome.recorded == outcome.rows for outcome in status.outcomes.values())
    ((healthy, detail),) = sidecar.reports
    assert healthy and detail == "read 4 accounts through 4 connections (synthetic)"
    states = sorted(ops.SyncState.Name(s.state) for s in sidecar.sent("ReportSyncStatus"))
    assert states == [
        "SYNC_STATE_CURRENT",
        "SYNC_STATE_CURRENT",
        "SYNC_STATE_DELAYED_BY_DESIGN",
        "SYNC_STATE_NEEDS_SIGN_IN",
    ]


class Unreachable:
    async def connections(self) -> list[dict[str, Any]]:
        raise VenueError("listing connections")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} should not be asked")


async def test_a_read_that_fails_is_reported_unhealthy_and_shown() -> None:
    sidecar = Sidecar()
    running = syncer(sidecar, make_venue=lambda config: Unreachable())
    running.configure(config_from(GIVEN))
    status = await running.run_once()
    assert status.error == "listing connections failed: no answer"
    assert sidecar.reports == [(False, "listing connections failed: no answer")]
    assert sidecar.calls == []


async def test_nothing_it_logs_or_reports_carries_a_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sidecar = Sidecar()

    def synthetic_with_credentials(config: Config) -> Venue:
        assert config.credentials is not None
        return SyntheticVenue(clock())

    running = syncer(sidecar, make_venue=synthetic_with_credentials)
    running.configure(config_from(GIVEN))
    with caplog.at_level(logging.DEBUG):
        await running.run_once()
    said = caplog.text + repr(sidecar.calls) + repr(sidecar.reports) + repr(running.status)
    for secret in (SECRET, "KEY-not-real", "CLIENT"):
        assert secret not in said


class Held:
    """The synthetic venue, its connections held until `go` is set; `users`
    fails as nothing the read expects, when asked to."""

    def __init__(self, go: asyncio.Event, stop: bool = False) -> None:
        self._inner = SyntheticVenue(clock())
        self._go = go
        self._stop = stop

    async def connections(self) -> list[dict[str, Any]]:
        await self._go.wait()
        return await self._inner.connections()

    async def users(self) -> list[str]:
        if self._stop:
            raise RuntimeError("stopped")
        return await self._inner.users()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_read_under_way_is_said_beside_the_read_before_it() -> None:
    sidecar = Sidecar()
    go = asyncio.Event()
    running = syncer(sidecar, make_venue=lambda config: Held(go))
    running.configure(config_from({SYNTHETIC: True}))
    go.set()
    first = await running.run_once()
    assert not first.reading and first.read_at is not None

    go.clear()
    second = asyncio.create_task(running.run_once())
    await asyncio.sleep(0)
    # Reading: what the pages show is still the first read, marked as reading.
    assert running.status.reading
    assert running.status.read_at == first.read_at
    assert running.status.connections == first.connections
    go.set()
    done = await second
    assert not done.reading and running.status is done


async def test_a_read_that_stops_leaves_the_read_before_it_not_reading() -> None:
    sidecar = Sidecar()
    go = asyncio.Event()
    go.set()
    running = syncer(sidecar, make_venue=lambda config: Held(go, stop=True))
    running.configure(config_from({SYNTHETIC: True}))
    before = running.status
    with pytest.raises(RuntimeError):
        await running.run_once()
    assert not running.status.reading
    assert running.status == before


async def test_waiting_for_settings_is_never_reading() -> None:
    running = syncer(Sidecar(), make_venue=lambda config: None)
    running.configure(config_from({}))
    assert not (await running.run_once()).reading


# ── The figures on the plugin's Summary ─────────────────────────────────────

NOT_READ = [
    Figure("Connections", 0),
    Figure("Accounts reached", 0),
    Figure("Last read", "Not yet"),
]
# The synthetic read: Schwab's connection needs sign-in (SnapTrade disabled
# it), IBKR's is delayed by design.
SYNTHETIC_READ = [
    Figure(
        "Connections",
        4,
        state="warn",
        why="0 stale, 1 needing sign-in, 0 disabled, 0 with holdings unavailable",
    ),
    Figure("Accounts reached", 4),
    Figure("Last read", NOW),
]
FAILED = [
    Figure("Connections", 0),
    Figure("Accounts reached", 0),
    Figure("Last read", NOW, state="error", why="listing connections failed: no answer"),
]


async def test_a_read_reports_its_figures_with_its_health() -> None:
    """Connections, warned with each state that asks for attention; Accounts
    reached; and Last read, as the heartbeat carries them to core's Summary."""
    sidecar = Sidecar()
    running = syncer(sidecar)
    running.configure(config_from({SYNTHETIC: True}))
    await running.run_once()
    assert sidecar.heartbeats == [
        heartbeat(
            healthy=True,
            detail="read 4 accounts through 4 connections (synthetic)",
            figures=SYNTHETIC_READ,
        )
    ]


async def test_a_failed_read_marks_last_read_an_error_and_says_why() -> None:
    sidecar = Sidecar()
    running = syncer(sidecar, make_venue=lambda config: Unreachable())
    running.configure(config_from(GIVEN))
    await running.run_once()
    assert sidecar.heartbeats == [
        heartbeat(healthy=False, detail="listing connections failed: no answer", figures=FAILED)
    ]


async def test_waiting_for_settings_reports_nothing_read_yet() -> None:
    sidecar = Sidecar()
    running = syncer(sidecar, make_venue=lambda config: None)
    running.configure(config_from({}))
    await running.run_once()
    (sent,) = sidecar.heartbeats
    assert not sent.healthy
    assert sent.figures == heartbeat(figures=NOT_READ).figures


async def test_the_figures_follow_every_read() -> None:
    """Each read that changes them reports them again: a failure after a read
    marks Last read an error, and the next read that succeeds clears it."""
    sidecar = Sidecar()
    venues: list[Any] = [SyntheticVenue(clock()), Unreachable(), SyntheticVenue(clock())]
    running = syncer(sidecar, make_venue=lambda config: venues.pop(0))
    for _ in range(3):
        running.configure(config_from({SYNTHETIC: True}))
        await running.run_once()
    assert [list(beat.figures) for beat in sidecar.heartbeats] == [
        list(heartbeat(figures=shown).figures)
        for shown in (SYNTHETIC_READ, FAILED, SYNTHETIC_READ)
    ]


def test_connections_needing_nothing_are_not_marked() -> None:
    fine = ConnectionView("c1", "", "Broker", "read", SyncState.CURRENT, "", None)
    delayed = ConnectionView("c2", "", "Broker", "read", SyncState.DELAYED_BY_DESIGN, "", None)
    shown = figures(Status(mode="snaptrade", read_at=NOW, connections=(fine, delayed)))
    assert shown[0] == Figure("Connections", 2)


def test_a_connection_whose_holdings_are_unavailable_asks_for_attention() -> None:
    """Its remedy is a person's, to connect the account another way (the
    product owner, 2026-09-28), so the figure counts it and says so."""
    hidden = ConnectionView(
        "c1", "", "Broker", "read", SyncState.HOLDINGS_UNAVAILABLE, "", None
    )
    shown = figures(Status(mode="snaptrade", read_at=NOW, connections=(hidden,)))
    assert shown[0] == Figure(
        "Connections",
        1,
        state="warn",
        why="0 stale, 0 needing sign-in, 0 disabled, 1 with holdings unavailable",
    )


def test_a_long_failure_is_cut_to_what_a_figure_carries() -> None:
    """Refused rather than cut by the SDK, so cut here, where it is made."""
    shown = figures(Status(mode="snaptrade", error="x" * 300, failed_at=NOW))
    assert shown[2].why == "x" * 199 + "\N{HORIZONTAL ELLIPSIS}"
    heartbeat(figures=shown)  # within every bound
