"""Past the window: each kind's units moved to the archive, kept or deleted, as
the settings say (contract v16; meridian-design spec/an-edge-plugins-older-
records-move-to-the-archive, plans/an-edge-plugins-older-records-move-to-
the-archive row 6).

The plugin keeps two kinds of raw record (declaration.py), each in units
(raw.py): an account's day of reads, and an account's month of reported
activity records. A unit whose last record was received before its kind's
window (settings.Windows) is past it, and is, as the kind's past-the-window
setting says:

- **archived**, through the SDK's `plugin.archive_unit`, which writes it to
  the archive, checks it landed, records the move through the sidecar, and
  only then removes it from storage; where the deployment gives the instance
  no archive, or a deployment admin has allowed none, the SDK or the
  conductor refuses it before anything moves, and it is kept, tried again
  on the next pass (`archived` is the SDK's default only where an archive is
  given, and the conductor refuses it as a setting where none is allowed);
- **kept**, left where it is; or
- **deleted**, through `plugin.delete_unit`, which records the deletion
  first, so a refusal keeps it: the sidecar refuses one inside the
  deployment's hold (REFUSAL_REASON_WITHIN_HOLD), and the unit stays, tried
  again on the next pass and refused again, nothing recorded. A reported
  activity's unit is never deleted within the history SnapTrade reported
  (`RawStore.kept_for`).

Each unit is noted in its account's ledger before it is moved, so the Raw
responses tab lists it whatever the move came to; the SDK's index, read with
`plugin.find_record`, says where it stands. Nothing is remembered between
passes but what the storage holds: a restart's pass finds the archived units
gone from storage and the refused ones refused again, and records nothing
twice.

A unit is moved only once its day or month has ended, so nothing is written
into it after: a read is kept in the day it was read, and an activity's
record in the month it was received. A unit the SDK's index says moved
already is moved again only where it holds what the ledger noted -- one
recorded before a restart could remove it, which the SDK then removes --
and is kept otherwise, with a warning: never moved under a path that held
other records.

A pass runs when the settings arrive, at start and on every change, and
after each read; it ends by saying what each kind holds in storage on the
heartbeat (`plugin.stored`, one `StoredSpan` per kind), which the plugin's
Summary shows beside what the archive holds from the moves. The stand-in
store, where the deployment grants no storage, moves nothing and says
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import meridian
from meridian.v1 import sidecar_pb2

from .raw import RawStore, Unit, _ns
from .settings import ACTIVITY, ARCHIVED, DELETED, RESPONSES, Window, Windows

log = logging.getLogger("snaptrade")

#: The kinds, in the order a pass takes them and the heartbeat names them.
KINDS = (ACTIVITY, RESPONSES)


@dataclass
class Tended:
    """What one pass came to."""

    archived: int = 0
    deleted: int = 0
    # Past the window and left: kept by the setting.
    kept: int = 0
    # A deletion refused inside the deployment's hold, the unit kept.
    held: int = 0
    # A move refused or failed -- no archive given or allowed, say -- the
    # unit kept and tried again on the next pass.
    failed: int = 0

    def said(self) -> str:
        return (
            f"past the window: {self.archived} archived, {self.deleted} deleted, "
            f"{self.kept} kept, {self.held} kept inside the deployment's hold, "
            f"{self.failed} not moved"
        )


class Keeper:
    """Moves the plugin's raw records past their windows, one pass at a time."""

    def __init__(
        self, plugin: meridian.Plugin, store: RawStore, now: Callable[[], datetime]
    ) -> None:
        self._plugin = plugin
        self._store = store
        self._now = now
        self._lock = asyncio.Lock()
        # The units whose refusal this process has said once: said again only
        # when it changes, so a refused deletion tried on every pass is one
        # line in the log, not one a read.
        self._said: dict[str, str] = {}

    async def tend(self, windows: Windows) -> Tended:
        """One pass over every unit of each kind, then what storage holds of
        each on the heartbeat."""
        store = self._store
        tended = Tended()
        if store.storage is None:
            return tended
        async with self._lock:
            now = self._now()
            store.responses_window = windows.responses.length
            store.activity_window = windows.activity.length
            store.settle(now)
            ended = {RESPONSES: f"{now:%Y-%m-%d}", ACTIVITY: f"{now:%Y-%m}"}
            stored = []
            for kind in KINDS:
                window = windows.of(kind)
                past = _ns(now - window.length)
                # A deletion of reported activity waits for the history too.
                deletable = _ns(now - store.kept_for()) if kind == ACTIVITY else past
                count, first, last = 0, 0, 0
                for unit in store.units(kind):
                    if (
                        unit.path.name < ended[kind]
                        and unit.last_received_ns < past
                        and self._movable(unit)
                    ) and await self._move(
                        unit, window, unit.last_received_ns < deletable, tended
                    ):
                        continue
                    count += unit.record_count
                    first = min(first or unit.first_received_ns, unit.first_received_ns)
                    last = max(last, unit.last_received_ns)
                stored.append(
                    meridian.StoredSpan(
                        record_kind=kind,
                        record_count=count,
                        first_received_ns=first,
                        last_received_ns=last,
                    )
                )
            self._plugin.stored = stored
        if tended.archived or tended.deleted or tended.failed:
            log.info("%s", tended.said())
        return tended

    def _movable(self, unit: Unit) -> bool:
        """Whether a unit may be moved: never moved before, or recorded moved
        with the very files the ledger noted, whose removal a restart cut
        short."""
        if self._plugin.find_record(unit.unit) is None:
            return True
        noted = self._store.noted(unit)
        if noted is not None and set(noted.files) == set(unit.files):
            return True
        self._once(unit, "kept: the SDK's index holds a move of this unit with other records")
        return False

    async def _move(self, unit: Unit, window: Window, deletable: bool, tended: Tended) -> bool:
        """Move one unit past its window as `window.past` says: whether it
        left the storage."""
        if window.past == ARCHIVED:
            self._store.note(unit, self._now())
            try:
                await self._plugin.archive_unit(
                    unit.kind,
                    unit.unit,
                    record_count=unit.record_count,
                    first_received_ns=unit.first_received_ns,
                    last_received_ns=unit.last_received_ns,
                )
            except (meridian.MeridianError, RuntimeError, OSError, ValueError) as failed:
                self._once(unit, f"not archived, and kept: {failed}")
                tended.failed += 1
                return False
            log.info(
                "archived %s (%s, %d records), past %s",
                unit.unit,
                unit.kind,
                unit.record_count,
                _named(window),
            )
            tended.archived += 1
            return True
        if window.past == DELETED and deletable:
            self._store.note(unit, self._now())
            try:
                await self._plugin.delete_unit(
                    unit.kind,
                    unit.unit,
                    record_count=unit.record_count,
                    first_received_ns=unit.first_received_ns,
                    last_received_ns=unit.last_received_ns,
                )
            except meridian.CommandRefused as refused:
                if refused.reason == sidecar_pb2.REFUSAL_REASON_WITHIN_HOLD:
                    self._once(unit, "kept inside the deployment's hold")
                    tended.held += 1
                    return False
                self._once(unit, f"not deleted, and kept: {refused}")
                tended.failed += 1
                return False
            except (meridian.MeridianError, RuntimeError, OSError, ValueError) as failed:
                self._once(unit, f"not deleted, and kept: {failed}")
                tended.failed += 1
                return False
            log.info(
                "deleted %s (%s, %d records), past %s",
                unit.unit,
                unit.kind,
                unit.record_count,
                _named(window),
            )
            tended.deleted += 1
            return True
        tended.kept += 1
        return False

    def _once(self, unit: Unit, said: str) -> None:
        if self._said.get(unit.unit) != said:
            self._said[unit.unit] = said
            log.warning("%s: %s", unit.unit, said)


def _named(window: Window) -> str:
    return f"its window of {window.days} days"
