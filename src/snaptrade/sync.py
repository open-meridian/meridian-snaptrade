"""One read of SnapTrade, carried through to the sidecar, and what the admin
page shows of it.

Nothing here survives the process: the next read rebuilds everything from
SnapTrade, and what the pages show is the last read, kept in memory. The one
thing kept beyond it is SnapTrade's raw responses to each read, per account,
in the plugin's own storage for their retention (raw.py, decisions/028), for
the Raw responses tab: never read back into what is recorded.

Each read ends in a report of the plugin's health and its figures, which core
draws on its Summary under Manage beside its own status (the product owner,
2026-10-01): Connections, with how many need attention; Accounts reached;
and Last read, marked an error, with why, when it failed. The SDK sends both
again on every heartbeat until the next read reports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import meridian
from meridian.bounds import PLUGIN_FIGURE_WHY_LENGTH

from .contract import Outcome, Recorder
from .declaration import seen
from .normalise import AccountView, ConnectionView, SyncState, ns, views
from .raw import RawStore, Taken, taken
from .settings import Config
from .synthetic import SyntheticVenue
from .venue import Snapshot, SnapTradeVenue, Venue, VenueError, read, utc_now

log = logging.getLogger("snaptrade")

#: The connection states that ask a person to do something, each as the
#: Connections figure counts it.
ATTENTION: dict[SyncState, str] = {
    SyncState.STALE: "stale",
    SyncState.NEEDS_SIGN_IN: "needing sign-in",
    SyncState.DISABLED: "disabled",
    # A person connects the account another way (the product owner,
    # 2026-09-28).
    SyncState.HOLDINGS_UNAVAILABLE: "with holdings unavailable",
}


@dataclass(frozen=True)
class Status:
    """The last read, as the pages show it."""

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
    # When it failed.
    failed_at: datetime | None = None
    # The required settings not yet given.
    missing: tuple[str, ...] = ()
    # A read is under way: the rest is the read before it, until it ends.
    reading: bool = False

    @property
    def accounts(self) -> tuple[AccountView, ...]:
        return tuple(view for connection in self.connections for view in connection.accounts)


def figures(status: Status) -> list[meridian.Figure]:
    """The figures on the plugin's Summary, from the last read: how many
    connections, warned with how many in each state that asks for attention
    when any does; how many accounts they reach; and when the last read was,
    an error with its reason when it failed, "Not yet" before any. Counts
    and a moment, and no account's data: Manage shows none."""
    states = [connection.state for connection in status.connections]
    attention = sum(states.count(state) for state in ATTENTION)
    connections = meridian.Figure(
        "Connections",
        len(status.connections),
        state="warn" if attention else None,
        why=", ".join(f"{states.count(state)} {said}" for state, said in ATTENTION.items())
        if attention
        else None,
    )
    last: meridian.Figure
    if status.error:
        why = status.error
        if len(why) > PLUGIN_FIGURE_WHY_LENGTH.most:
            why = why[: PLUGIN_FIGURE_WHY_LENGTH.most - 1] + "\N{HORIZONTAL ELLIPSIS}"
        last = meridian.Figure(
            "Last read", status.failed_at or "Failed", state="error", why=why
        )
    else:
        last = meridian.Figure("Last read", status.read_at or "Not yet")
    return [connections, meridian.Figure("Accounts reached", len(status.accounts)), last]


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
        raw: RawStore | None = None,
    ) -> None:
        self._plugin = plugin
        # SnapTrade's raw responses, kept per account; None keeps none.
        self.raw = raw
        self._recorder = Recorder(plugin)
        self._now = now
        self._make_venue = make_venue or (lambda config: venue_for(config, now))
        self.config = Config()
        self.venue: Venue | None = None
        self.status = Status()

    def now(self) -> datetime:
        """The time, by the clock this syncer reads by."""
        return self._now()

    def keep_history(self, one: Taken) -> str:
        """An account's history read on its own (history.py), kept as any
        read is, its credentials redacted: the key it was kept under, or ""
        where nothing is kept."""
        if self.raw is None:
            return ""
        config = self.config
        secrets = config.credentials.secrets() if config.credentials is not None else ()
        return self.raw.keep_one(one, secrets)

    def configure(self, config: Config) -> None:
        self.config = config
        self.venue = self._make_venue(config)
        if self.raw is not None:
            # The first delivery comes as the plugin starts: what is past the
            # retention, as the settings now say it, goes before any read.
            self.raw.retention = config.raw_retention
            self.raw.prune(self._now())

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
            await self._report(healthy=False, detail=detail)
            log.info(detail)
            return self.status

        self.status = replace(self.status, reading=True)
        try:
            return await self._read(config, venue, base)
        finally:
            if self.status.reading:
                # It stopped before it came to anything: the read before it stands.
                self.status = replace(self.status, reading=False)

    async def _read(self, config: Config, venue: Venue, base: Status) -> Status:
        try:
            snapshot = await read(venue, self._now)
        except VenueError as failed:
            self.status = replace(base, error=str(failed), failed_at=self._now())
            await self._report(healthy=False, detail=str(failed))
            log.warning("%s", failed)
            return self.status
        try:
            users = tuple(await venue.users())
        except VenueError:
            # A personal key has no users to list; nothing else depends on it.
            users = ()

        connections = views(snapshot, config.stale_after)
        self._keep_raw(config, snapshot, connections)
        self._note_not_carried(snapshot)
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
        await self._report(healthy=True, detail=detail)
        log.info("%s", detail)
        return self.status

    def _keep_raw(
        self, config: Config, snapshot: Snapshot, connections: tuple[ConnectionView, ...]
    ) -> None:
        """SnapTrade's responses to this read, kept per account with every
        credential redacted, then whatever is past the retention pruned."""
        if self.raw is None:
            return
        secrets = config.credentials.secrets() if config.credentials is not None else ()
        self.raw.keep(taken(snapshot, connections, config.synthetic), secrets)
        self.raw.prune(self._now())

    def _note_not_carried(self, snapshot: Snapshot) -> None:
        """Count each name SnapTrade sent this read that the plugin declares
        it does not carry (declaration.py): by name only, for the heartbeat."""
        for scheme, name in seen(snapshot):
            self._plugin.note_not_carried(scheme, name)

    async def _report(self, *, healthy: bool, detail: str) -> None:
        """The plugin's health, and its figures from the status just set: both
        stand on every heartbeat after, until the next read reports."""
        await self._plugin.report(healthy=healthy, detail=detail, figures=figures(self.status))
