"""One read, end to end through the SDK's operations, in each mode."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from meridian.plugin.v1 import operations_pb2 as ops

from snaptrade.contract import Contract
from snaptrade.settings import (
    CLIENT_ID,
    CONSUMER_KEY,
    SYNTHETIC,
    USER_ID,
    USER_SECRET,
    Config,
    config_from,
)
from snaptrade.sync import Syncer, venue_for
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import SnapTradeVenue, Venue, VenueError

from conftest import Sidecar, clock

SECRET = "user-secret-77aa-not-real"
GIVEN: dict[str, str | int | bool] = {
    CLIENT_ID: "CLIENT",
    CONSUMER_KEY: "KEY-not-real",
    USER_ID: "u-1",
    USER_SECRET: SECRET,
}


def syncer(sidecar: Sidecar, make_venue: Any = None) -> Syncer:
    return Syncer(sidecar.plugin(), Contract.of(sidecar), now=clock(), make_venue=make_venue)


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
    assert detail == (
        "waiting for settings: snaptrade_client_id, snaptrade_consumer_key, "
        "snaptrade_user_id, snaptrade_user_secret"
    )


async def test_synthetic_mode_records_every_account_it_can() -> None:
    sidecar = Sidecar()
    running = syncer(sidecar)
    running.configure(config_from({SYNTHETIC: True}))
    status = await running.run_once()
    assert status.mode == "synthetic" and status.error == ""
    assert len(sidecar.sent("ReportSyncStatus")) == 3
    opened = sidecar.sent("RecordHoldingsStatement")
    assert len(opened) == 3
    rows = sidecar.sent("RecordHolding")
    assert len(rows) == sum(statement.expected_rows for statement in opened)
    assert all(outcome.recorded == outcome.rows for outcome in status.outcomes.values())
    ((healthy, detail),) = sidecar.reports
    assert healthy and detail == "read 3 accounts through 3 connections (synthetic)"
    states = sorted(ops.SyncState.Name(s.state) for s in sidecar.sent("ReportSyncStatus"))
    assert states == [
        "SYNC_STATE_CURRENT",
        "SYNC_STATE_DELAYED_BY_DESIGN",
        "SYNC_STATE_DISABLED",
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
