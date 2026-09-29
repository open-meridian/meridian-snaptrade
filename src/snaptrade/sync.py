"""One read of SnapTrade, carried through to the sidecar, and what the admin
page shows of it.

Nothing here survives the process: plugins are ephemeral, and the next read
rebuilds everything from SnapTrade. What the page shows is the last read, kept
in memory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import meridian

from .contract import Outcome, Recorder
from .normalise import AccountView, ConnectionView, ns, views
from .settings import Config
from .synthetic import SyntheticVenue
from .venue import SnapTradeVenue, Venue, VenueError, read, utc_now

log = logging.getLogger("snaptrade")


@dataclass(frozen=True)
class Status:
    """The last read, as the admin page shows it."""

    # "synthetic", "snaptrade", or "waiting" for settings.
    mode: str = "waiting"
    read_at: datetime | None = None
    connections: tuple[ConnectionView, ...] = ()
    # By external account ID.
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    users: tuple[str, ...] = ()
    user_id: str = ""
    # Why the last read failed, when it did. Safe to show.
    error: str = ""
    # The required settings not yet given.
    missing: tuple[str, ...] = ()

    @property
    def accounts(self) -> tuple[AccountView, ...]:
        return tuple(view for connection in self.connections for view in connection.accounts)


def venue_for(config: Config, now: Callable[[], datetime] = utc_now) -> Venue | None:
    """Synthetic when asked for, SnapTrade when every credential is given,
    and nothing otherwise: there is no default key to fall back on."""
    if config.synthetic:
        return SyntheticVenue(now)
    if config.credentials is not None:
        return SnapTradeVenue(config.credentials)
    return None


class Syncer:
    """Reads SnapTrade and records what it read, one read at a time."""

    def __init__(
        self,
        plugin: meridian.Plugin,
        now: Callable[[], datetime] = utc_now,
        make_venue: Callable[[Config], Venue | None] | None = None,
    ) -> None:
        self._plugin = plugin
        self._recorder = Recorder(plugin)
        self._now = now
        self._make_venue = make_venue or (lambda config: venue_for(config, now))
        self.config = Config()
        self.venue: Venue | None = None
        self.status = Status()

    def configure(self, config: Config) -> None:
        self.config = config
        self.venue = self._make_venue(config)

    async def run_once(self) -> Status:
        config, venue = self.config, self.venue
        mode = "synthetic" if config.synthetic else "snaptrade"
        base = Status(
            mode=mode,
            user_id="synthetic-user" if config.synthetic else config.user_id,
            missing=config.missing,
        )
        if venue is None:
            detail = "waiting for settings: " + ", ".join(config.missing)
            self.status = replace(base, mode="waiting")
            await self._plugin.report(healthy=False, detail=detail)
            log.info(detail)
            return self.status

        try:
            snapshot = await read(venue, self._now)
        except VenueError as failed:
            self.status = replace(base, error=str(failed))
            await self._plugin.report(healthy=False, detail=str(failed))
            log.warning("%s", failed)
            return self.status
        try:
            users = tuple(await venue.users())
        except VenueError:
            # A personal key has no users to list; nothing else depends on it.
            users = ()

        connections = views(snapshot, config.stale_after)
        observed = ns(snapshot.read_at)
        accounts = [view for connection in connections for view in connection.accounts]
        outcomes: dict[str, Outcome] = {}
        try:
            await self._recorder.report_accounts([view.account for view in accounts])
        except meridian.MeridianError as refused:
            log.warning("reporting the accounts it reaches was refused: %s", refused)
        for view in accounts:
            external_id = view.account.external_account_id
            try:
                await self._recorder.report_sync(view.account, view.freshness, observed)
            except meridian.MeridianError as refused:
                log.warning("sync status for %s was refused: %s", external_id, refused)
            for problem in view.problems:
                log.warning("%s: %s", external_id, problem)
            if view.statement is None:
                log.info("nothing recorded for %s: %s", external_id, view.withheld)
                continue
            outcomes[external_id] = await self._recorder.record(
                view.account, view.statement, observed
            )

        self.status = replace(
            base,
            read_at=snapshot.read_at,
            connections=connections,
            outcomes=outcomes,
            users=users,
        )
        stopped = sum(1 for outcome in outcomes.values() if outcome.stopped)
        detail = f"read {len(accounts)} accounts through {len(connections)} connections"
        if config.synthetic:
            detail += " (synthetic)"
        if stopped:
            detail += f"; {stopped} statements stopped, see the plugin's page"
        await self._plugin.report(healthy=True, detail=detail)
        log.info("%s", detail)
        return self.status
