"""SnapTrade's raw responses, as each read received them, kept per account
for a stated time: what SnapTrade actually said, to debug a discrepancy
between it and what the plugin recorded (the product owner, 2026-10-02).

The separation of duties (meridian-design spec/vendor-differences-have-a-
place-in-the-contract, "Separation of duties"): the sidecar is the
normalisation boundary, and the edge plugin keeps the vendor's raw inputs in
its own storage (decisions/028), for recordkeeping, for debugging a
discrepancy, for backfill when the contract gains a field, and as evidence
that the model lacks something. Core never reads them, and no other plugin
does: they are shown on this plugin's own Raw responses tab alone.

**The storage is the deployment's** (decisions/028; contract v11). The
plugin declares the storage it asks for, with its retention, in its version's
declaration (declaration.py), and the deployment gives an instance holding an
edge role a claim of its own, mounted in its container alone and kept when
the pod is replaced (meridian-core fbcc297), which the SDK names
(`meridian.edge.storage_dir()`). `__main__` keeps the records there, and
only where no storage is given -- a test, a development run -- in `/tmp`
(STAND_IN), which lasts as long as the pod, and from which nothing is moved.

**Each row names its record** (contract v11): a row or a statement sent to
the street references the raw record it was converted from by
`record_key(account, read, call)` -- the external account, the read and the
call, `positions`, `balances` or `activities` -- which this plugin's Raw
responses tab resolves (`find`), and nothing else reads.

What is kept, per account and per read: the read's time; each call made for
that account, by the plugin's name for it ("reading positions") and the
request it was (`GET /accounts/{accountId}/positions/all`), never the query
string, a header or a credential; and SnapTrade's JSON body as received, read
exactly (every number as written, never a float), or why the call failed. A
list call answered for every account (the connections, the accounts) is kept
as this account's own entry of it, so a record holds one account's data and
no other's. The SnapTrade users under the key are no account's data and are
not kept. Before anything is written, a field whose name is a credential's
(a secret, a token, a password, an authorization or a signature) is replaced,
and so is any credential value found anywhere in a body.

**A reported activity's record is its own, and kept as long as the history
it reported** (contract v14, the plan's question 5): each activity reported
to the street (activities.py) references `activity_key(account, id)` --
`activities/<external account>/<SnapTrade's activity ID>` -- a record holding
SnapTrade's entry for that activity as the read that first reported it
received it, with that read's time and why it was read (the backfill to the
account's `history_from`, or a sync). Written once: an activity reported
again names the record already kept, as the street answers it already
recorded. It stays in storage for its kind's window from when it was
received (seven years by default, `ACTIVITY_RETENTION_DAYS`), and is never
deleted within the history SnapTrade reported: the longest any kept record's
activity was traded before it was received (`history_reach_days`), which a
shorter window is held to for a deletion (the product owner, 2026-10-05).
`find` resolves it as it does a row's.

**Two kinds, each in units** (contract v16; archive.py). Each record is one
gzipped JSON file, written whole or not at all, under an account directory
named by a hash of the external account ID (so no ID names a path): a read's
in its day, `<root>/<account>/<YYYY-MM-DD>/<read>.json.gz`, the read its
time, which is its key; an activity's in the month it was received,
`<root>/<account>/activities/<YYYY-MM>/<hash of its ID>.json.gz`, the file's
time when it was received. Each day and each month is a unit, which archive.py
moves past its kind's window through the SDK's helpers -- archived, kept or
deleted, as the settings say -- after noting it in the account's ledger
(`moved.json`, beside them), so the Raw responses tab lists it and a record in
it is found again: the SDK's index says where it stands (archived,
restorable; restored, readable in the restore area for seven days; or
deleted). 0.12.0 kept every record flat in its account's directory (and its
activities'); `settle` moves each into its unit, keeping its time.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from meridian import edge

from .normalise import ConnectionView
from .settings import ACTIVITY, RESPONSES
from .venue import Json, Snapshot, parse_exact

log = logging.getLogger("snaptrade")

#: Where `__main__` keeps the records where the deployment gives no storage:
#: the pod's one writable place.
STAND_IN = Path(tempfile.gettempdir()) / "snaptrade" / "raw-responses"


def storage_root(granted: Path | None = None) -> Path:
    """The directory for the raw responses in the storage the deployment
    grants this instance (decisions/028, `meridian.edge.storage_dir()`), or
    the stand-in where it grants none."""
    granted = granted if granted is not None else edge.storage_dir()
    return granted / "raw-responses" if granted is not None else STAND_IN


DEFAULT_RETENTION_DAYS = 30
LEAST_RETENTION_DAYS = 1
#: How long a reported activity's record is kept from when it was received,
#: unless `activity_window_days` says otherwise: seven years, past
#: the two SnapTrade holds of an account's history at Fidelity, so each
#: activity the street holds can have its record read back.
ACTIVITY_RETENTION_DAYS = 2555
#: The longest the setting may keep one, which the storage declaration states
#: (the SDK's bound), so the deployment never keeps less than an admin chose.
MOST_ACTIVITY_RETENTION_DAYS = 36500
#: Where an account's activity records are kept, beside its reads.
ACTIVITY_DIRECTORY = "activities"
#: The first part of an activity record's key.
ACTIVITY_KEYS = "activities"

REDACTED = "[redacted]"

# The calls a read makes for an account, as the plugin names them (venue.py's
# VenueError says the same words) and the request each is, as SnapTrade's API
# documents it: the path alone, never the query string, which carries the
# user secret.
CONNECTIONS = ("listing connections", "GET /authorizations")
ACCOUNTS = ("listing accounts", "GET /accounts")
POSITIONS = ("reading positions", "GET /accounts/{accountId}/positions/all")
BALANCES = ("reading balances", "GET /accounts/{accountId}/balances")
ACTIVITIES = ("reading activities", "GET /accounts/{accountId}/activities")
EACH = (CONNECTIONS, ACCOUNTS, POSITIONS, BALANCES, ACTIVITIES)
#: A row's call, as its record key names it, and the call kept for it.
CALLS = {"positions": POSITIONS, "balances": BALANCES, "activities": ACTIVITIES}
#: What an activity's record says of the one entry it keeps.
ITS_ACTIVITY = "this activity's entry, of the page SnapTrade answered"
# What a list call kept for one account says it is.
ITS_CONNECTION = "this account's connection, of the list SnapTrade answered"
ITS_ENTRY = "this account's entry, of the list SnapTrade answered"

# A field named as a credential is: its name, lowercased, letters and digits
# only, is one of these or ends in one of the endings. Exact names, so that
# `brokerage_authorization`, the connection an account belongs to, is kept.
_SECRET_NAMES = frozenset(
    {
        "authorization",
        "signature",
        "apikey",
        "clientid",
        "consumerkey",
        "usersecret",
        "secret",
        "password",
        "token",
    }
)
_SECRET_ENDINGS = ("secret", "password", "token")
# A credential value shorter than this is not looked for in the bodies: it
# would match ordinary text, and no key SnapTrade issues is so short.
_SHORTEST_SECRET = 6

_READ = re.compile(r"^(\d{8}T\d{6}\.\d{6}Z)(?:-(\d+))?$")
_SUFFIX = ".json.gz"
# A unit's directory: an account's day of reads, its month of activity records.
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
#: Where the SDK's helpers put a restored unit, in the instance's storage, at
#: the unit's own path under it (meridian.edge: "a restore area in storage",
#: whose path `restore_unit` answers), readable there for seven days.
RESTORE_AREA = Path(".meridian", "restored")
#: Each account's ledger of the units this plugin moved or asked to move,
#: beside them in its directory: what the Raw responses tab lists, and where
#: a record of a moved unit is found again.
LEDGER = "moved.json"
#: How long a restored unit stays readable in the restore area before the SDK
#: returns it: the deployment's restore period (the plan's question 2).
RESTORE_PERIOD = timedelta(days=7)


# ── Keeping a credential out ────────────────────────────────────────────────


def _secret_name(name: str) -> bool:
    plain = re.sub(r"[^a-z0-9]", "", name.lower())
    return plain in _SECRET_NAMES or plain.endswith(_SECRET_ENDINGS)


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """`value` with every field named as a credential replaced, and every
    occurrence of each of `secrets` in a string replaced: a copy, the
    original untouched."""
    hidden = sorted({s for s in secrets if len(s) >= _SHORTEST_SECRET}, key=len, reverse=True)
    return _redacted(value, hidden)


def _redacted(value: Any, hidden: Sequence[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _secret_name(str(key)) else _redacted(item, hidden)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redacted(item, hidden) for item in value]
    if isinstance(value, str):
        for secret in hidden:
            value = value.replace(secret, REDACTED)
        return value
    return value


# ── JSON, exactly ───────────────────────────────────────────────────────────


def dumps(value: Any, indent: int | None = None) -> str:
    """JSON with every Decimal written as the number it was read as: what
    `parse_exact` reads back to the same value. `json.dumps` cannot write a
    Decimal as a number, and a float would not be exact."""
    return "".join(_dump(value, indent, 0))


def _dump(value: Any, indent: int | None, depth: int) -> Iterable[str]:
    if isinstance(value, Decimal):
        yield str(value) if value.is_finite() else json.dumps(str(value))
    elif isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        yield json.dumps(value, ensure_ascii=False)
    elif isinstance(value, float):
        # Never from SnapTrade's JSON read exactly; written as Python would.
        yield repr(value) if math.isfinite(value) else json.dumps(repr(value))
    elif isinstance(value, dict | list):
        pairs = value.items() if isinstance(value, dict) else enumerate(value)
        opening, closing = ("{", "}") if isinstance(value, dict) else ("[", "]")
        if not value:
            yield opening + closing
            return
        inner = "" if indent is None else "\n" + " " * (indent * (depth + 1))
        outer = "" if indent is None else "\n" + " " * (indent * depth)
        yield opening
        for index, (key, item) in enumerate(pairs):
            yield ("," if index else "") + inner
            if isinstance(value, dict):
                yield json.dumps(str(key), ensure_ascii=False) + (": " if indent else ":")
            yield from _dump(item, indent, depth + 1)
        yield outer + closing
    else:
        yield json.dumps(str(value), ensure_ascii=False)


# ── What a read kept ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Taken:
    """One account's part of one read, ready to keep."""

    external_account_id: str
    record: Json


def history_taken(
    account: Any, read_at: datetime, calls: Sequence[Json], synthetic: bool
) -> Taken:
    """An account's history read on its own, asked for by a person or an
    agent (history.py): each page of its activities as SnapTrade answered
    it, the range and the page in the call's note, kept as any read is."""
    return Taken(
        account.external_account_id,
        {
            "external_account_id": account.external_account_id,
            "snaptrade_account_id": account.snaptrade_account_id,
            "read_at": read_at.isoformat(),
            "source": "synthetic" if synthetic else "snaptrade",
            "calls": list(calls),
        },
    )


def activities_call(body: Any, note: str) -> Json:
    """One page of an account's activities, as a record keeps it."""
    return _call(ACTIVITIES, body, note)


def activities_failed(said: str, note: str) -> Json:
    """A page of an account's activities that could not be read, and why."""
    return {"call": ACTIVITIES[0], "request": ACTIVITIES[1], "note": note, "failed": said}


def _call(asked: tuple[str, str], body: Any, note: str = "") -> Json:
    call: Json = {"call": asked[0], "request": asked[1]}
    if note:
        call["note"] = note
    call["body"] = body
    return call


def _failed(asked: tuple[str, str], said: str) -> Json:
    return {"call": asked[0], "request": asked[1], "failed": said}


def taken(
    snapshot: Snapshot, connections: Sequence[ConnectionView], synthetic: bool
) -> list[Taken]:
    """Each account's part of a read: its connection's entry and its own of
    the lists, then its positions and balances as SnapTrade answered them, or
    why a call failed. `connections` is the read normalised, which names each
    account as it is linked (its external account ID)."""
    by_id = {str(c.get("id", "")): c for c in snapshot.connections}
    accounts = {str(a.get("id", "")): a for a in snapshot.accounts}
    kept: list[Taken] = []
    for connection in connections:
        for view in connection.accounts:
            account = view.account
            snaptrade_id = account.snaptrade_account_id
            calls: list[Json] = []
            if account.connection_id in by_id:
                calls.append(_call(CONNECTIONS, by_id[account.connection_id], ITS_CONNECTION))
            calls.append(_call(ACCOUNTS, accounts.get(snaptrade_id, {}), ITS_ENTRY))
            if snaptrade_id in snapshot.positions:
                calls.append(_call(POSITIONS, snapshot.positions[snaptrade_id]))
            if snaptrade_id in snapshot.balances:
                calls.append(_call(BALANCES, snapshot.balances[snaptrade_id]))
            if snaptrade_id in snapshot.activities:
                calls.append(_call(ACTIVITIES, snapshot.activities[snaptrade_id]))
            elif snaptrade_id in snapshot.unread_activities:
                calls.append(_failed(ACTIVITIES, snapshot.unread_activities[snaptrade_id]))
            failure = snapshot.failures.get(snaptrade_id, "")
            if failure:
                asked = next((a for a in EACH if failure.startswith(a[0])), POSITIONS)
                calls.append(_failed(asked, failure))
            kept.append(
                Taken(
                    account.external_account_id,
                    {
                        "external_account_id": account.external_account_id,
                        "snaptrade_account_id": snaptrade_id,
                        "read_at": snapshot.read_at.isoformat(),
                        "source": "synthetic" if synthetic else "snaptrade",
                        "calls": calls,
                    },
                )
            )
    return kept


@dataclass(frozen=True)
class Read:
    """One kept read of one account, as listed: its key, when it was, and
    whether it is restored from the archive."""

    key: str
    read_at: datetime
    restored: bool = False


@dataclass(frozen=True)
class Record:
    """One kept read of one account, whole."""

    key: str
    read_at: datetime
    external_account_id: str
    synthetic: bool
    calls: tuple[Json, ...]
    # The file as kept, for downloading as it is.
    document: Json
    # Read from the restore area, restored from the archive.
    restored: bool = False


def _key_of(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%S.%fZ}"


def read_key(read_at_ns: int) -> str:
    """A read's key from its time, as a record of it is written under."""
    seconds, nanos = divmod(read_at_ns, 1_000_000_000)
    return _key_of(datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000))


def record_key(account: Any, read_at_ns: int, call: str) -> str:
    """A raw record's key as a row references it (contract v11): the
    external account, the read and the call. Opaque past this plugin."""
    return f"{account.external_account_id}/{read_key(read_at_ns)}/{call}"


def parse_record_key(key: str) -> tuple[str, str, str] | None:
    """The external account, read and call a record key names, or None."""
    account, _, rest = key.rpartition("/")
    if not account:
        return None
    account, _, read = account.rpartition("/")
    call = rest
    if not account or _moment_of(read) is None or call not in CALLS:
        return None
    return account, read, call


def activity_key(account: Any, activity_id: str) -> str:
    """A reported activity's raw record key (contract v14): the external
    account and SnapTrade's activity ID. Opaque past this plugin."""
    return f"{ACTIVITY_KEYS}/{account.external_account_id}/{activity_id}"


def parse_activity_key(key: str) -> tuple[str, str] | None:
    """The external account and activity an activity record key names, or None."""
    first, _, rest = key.partition("/")
    account, _, activity_id = rest.rpartition("/")
    if first != ACTIVITY_KEYS or not account or not activity_id:
        return None
    return account, activity_id


def activity_taken(
    account: Any, activity: Json, read_at: datetime, why: str, synthetic: bool
) -> Taken:
    """One activity's record: SnapTrade's entry for it, as the read that
    reports it received it, and why that read was made."""
    return Taken(
        account.external_account_id,
        {
            "external_account_id": account.external_account_id,
            "snaptrade_account_id": account.snaptrade_account_id,
            "activity_id": str(activity.get("id", "")),
            "read_at": read_at.isoformat(),
            "source": "synthetic" if synthetic else "snaptrade",
            "calls": [_call(ACTIVITIES, activity, f"{ITS_ACTIVITY}; {why}")],
        },
    )


def _reach_of(record: Any) -> int:
    """How many days before its record was received an activity was traded,
    by SnapTrade's entry in it (`activity_taken`); 0 where it does not say,
    rounded up, so a part of a day counts as one."""
    try:
        received = datetime.fromisoformat(str(record["read_at"]))
        traded = str(record["calls"][0]["body"]["trade_date"])
        day = datetime.fromisoformat(traded[:10]).replace(tzinfo=received.tzinfo or UTC)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0
    seconds = (received - day).total_seconds()
    return max(0, math.ceil(seconds / 86_400))


def _moment_of(key: str) -> datetime | None:
    matched = _READ.match(key)
    if matched is None:
        return None
    return datetime.strptime(matched.group(1), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)


def _order(key: str) -> tuple[str, int]:
    matched = _READ.match(key)
    return (key, 0) if matched is None else (matched.group(1), int(matched.group(2) or 0))


def _day_of(key: str) -> str:
    """The day a read's key falls on, as its unit's directory names it."""
    return f"{key[0:4]}-{key[4:6]}-{key[6:8]}"


def _month_of(received_ns: int) -> str:
    """The month an activity record was received in, as its unit's directory
    names it."""
    return f"{datetime.fromtimestamp(received_ns / 1e9, UTC):%Y-%m}"


def _ns(moment: datetime) -> int:
    seconds = int(moment.timestamp())
    return seconds * 1_000_000_000 + moment.microsecond * 1000


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _entries(directory: Path) -> list[os.DirEntry[str]]:
    """A directory's entries, read once: a unit's thousands of files are
    told apart by what the directory says of them, not a call each."""
    try:
        with os.scandir(directory) as listed:
            return list(listed)
    except OSError:
        return []


def _subdirectories(directory: Path, named: re.Pattern[str]) -> list[Path]:
    return sorted(
        Path(e.path) for e in _entries(directory) if e.is_dir() and named.match(e.name)
    )


def _files(directory: Path, suffix: str = _SUFFIX) -> list[Path]:
    return [
        Path(e.path) for e in _entries(directory) if e.is_file() and e.name.endswith(suffix)
    ]


@dataclass(frozen=True)
class Unit:
    """A unit of one kind of raw record, as the archive moves it (contract
    v16): an account's day of reads (`responses`), or an account's month of
    reported activity records (`activity`), a directory in the storage named
    by its path there, with how many records it holds and when the first and
    the last were received."""

    kind: str
    unit: str
    path: Path
    record_count: int
    first_received_ns: int
    last_received_ns: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class Moved:
    """A unit this plugin moved, or asked to move, as its ledger holds it:
    whether it was, and where it stands now, the SDK's index says
    (`plugin.find_record(unit)`)."""

    kind: str
    unit: str
    record_count: int
    first_received_ns: int
    last_received_ns: int
    files: tuple[str, ...]
    at: str


class RawStore:
    """The records, under `root`, in the storage the deployment grants
    (`storage`, which holds `root`; None for the stand-in, where nothing is
    moved).

    Every call runs on the plugin's loop, as the reads and the pages do, so a
    unit is never moved while a record of it is being written. A record is
    written to a temporary file and renamed into place, so a reader sees it
    whole or not at all."""

    def __init__(self, root: Path, storage: Path | None = None) -> None:
        self.root = root
        self.storage = storage
        # Each kind's window, as the settings say it (settings.Windows);
        # `kept_for` is what an activity record is held to before deletion.
        self.responses_window = timedelta(days=DEFAULT_RETENTION_DAYS)
        self.activity_window = timedelta(days=ACTIVITY_RETENTION_DAYS)
        # How far back the history reported reaches, in days, once a record
        # has been read or written: None until then.
        self._reach: int | None = None
        # Each account's ledger as last read, by its file's time.
        self._ledgers: dict[Path, tuple[int, dict[str, Moved]]] = {}
        # Why the last read's records could not be kept, when they could not:
        # safe to show (the error's type alone).
        self.failure = ""

    # ── Where things are ────────────────────────────────────────────────

    def _directory(self, external_account_id: str) -> Path:
        return self.root / f"a-{_digest(external_account_id)}"

    def _accounts(self) -> list[Path]:
        try:
            return sorted(
                d for d in self.root.iterdir() if d.is_dir() and d.name.startswith("a-")
            )
        except OSError:
            return []

    def _restored(self, path: Path) -> Path | None:
        """Where the SDK restores the unit or record at `path` (RESTORE_AREA),
        or None for the stand-in."""
        if self.storage is None:
            return None
        return self.storage / RESTORE_AREA / path.relative_to(self.storage)

    def restored_at(self, unit: str) -> datetime | None:
        """When a restored unit was put in the restore area, by its time
        there; None where it is not there."""
        if self.storage is None:
            return None
        try:
            stamp = (self.storage / RESTORE_AREA / unit).stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(int(stamp), UTC)

    def unit_of(self, path: Path) -> str:
        """A unit's path in the storage, as the SDK's helpers name it."""
        if self.storage is None:
            raise ValueError("the stand-in is no storage of the deployment's: nothing moves")
        return path.relative_to(self.storage).as_posix()

    # ── Writing ──────────────────────────────────────────────────────────

    def keep(self, kept: Iterable[Taken], secrets: Iterable[str] = ()) -> int:
        """Write each account's record, its credentials redacted first. How
        many were written; a failure to write is noted in `failure`, logged
        by its type, and leaves the read as it was."""
        hidden = tuple(secrets)
        written = 0
        try:
            for one in kept:
                self._write(one, redact(one.record, hidden))
                written += 1
        except OSError as failed:
            self.failure = f"the raw responses were not kept: {type(failed).__name__}"
            log.warning("%s", self.failure)
            return written
        self.failure = ""
        return written

    def keep_one(self, one: Taken, secrets: Iterable[str] = ()) -> str:
        """Write one account's record, its credentials redacted first: the
        key it was kept under, or "" where it could not be written, logged
        by the error's type, as `keep` does."""
        try:
            return self._write(one, redact(one.record, tuple(secrets)))
        except OSError as failed:
            log.warning("a raw response was not kept: %s", type(failed).__name__)
            return ""

    def keep_activity(self, one: Taken, secrets: Iterable[str] = ()) -> bool:
        """Write one activity's record where none is kept for it, its
        credentials redacted first: whether one is kept now. The first kept
        stands, as the street keeps the first report of an activity: one in
        storage, restored, or moved by a window is never written again."""
        record = one.record
        account, activity_id = one.external_account_id, str(record["activity_id"])
        if self._activity_file(account, activity_id) is not None or self._moved_holding(
            account, ACTIVITY_DIRECTORY, f"{_digest(activity_id)}{_SUFFIX}"
        ):
            return True
        try:
            received = datetime.fromisoformat(str(record["read_at"]))
            month = _month_of(_ns(received))
            file = self._directory(account) / ACTIVITY_DIRECTORY / month
            file = file / f"{_digest(activity_id)}{_SUFFIX}"
            file.parent.mkdir(parents=True, exist_ok=True)
            self._replace(file, redact(record, tuple(secrets)))
            # The file's time is when the activity was received, which its
            # window runs from.
            os.utime(file, (received.timestamp(), received.timestamp()))
        except OSError as failed:
            log.warning("an activity's raw response was not kept: %s", type(failed).__name__)
            return False
        if self._reach is not None:
            self._reach = max(self._reach, _reach_of(record))
        return True

    def _replace(self, target: Path, record: Json) -> None:
        """Write `record` to `target` whole, through a temporary file."""
        made, written = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with (
                os.fdopen(made, "wb") as file,
                gzip.GzipFile(fileobj=file, mode="wb", mtime=0) as zipped,
            ):
                zipped.write(dumps(record).encode())
            os.replace(written, target)
        except BaseException:
            Path(written).unlink(missing_ok=True)
            raise

    def _write(self, one: Taken, record: Json) -> str:
        base = _key_of(datetime.fromisoformat(str(record["read_at"])))
        directory = self._directory(one.external_account_id) / _day_of(base)
        directory.mkdir(parents=True, exist_ok=True)
        key, again = base, 1
        while (directory / f"{key}{_SUFFIX}").exists():
            again += 1
            key = f"{base}-{again}"
        self._replace(directory / f"{key}{_SUFFIX}", record)
        return key

    # ── Settling: what a stopped write left, and 0.12.0's layout ─────────

    def settle(self, now: datetime) -> int:
        """Remove what a write that stopped left (a temporary file over an
        hour old), and move each record kept flat by 0.12.0 into its unit:
        a read into its day, an activity record into the month it was
        received in, keeping its time. How many were moved."""
        moved = 0
        stale = now.timestamp() - 3600
        for account in self._accounts():
            activities = account / ACTIVITY_DIRECTORY
            for directory in (
                account,
                activities,
                *_subdirectories(account, _DAY),
                *_subdirectories(activities, _MONTH),
            ):
                for file in _files(directory, ""):
                    try:
                        if file.name.endswith(".tmp"):
                            if file.stat().st_mtime < stale:
                                file.unlink(missing_ok=True)
                            continue
                        if not file.name.endswith(_SUFFIX):
                            continue
                        if directory == account:
                            key = file.name.removesuffix(_SUFFIX)
                            if _moment_of(key) is None:
                                continue
                            target = account / _day_of(key) / file.name
                        elif directory == activities:
                            target = activities / _month_of(file.stat().st_mtime_ns) / file.name
                        else:
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            os.replace(file, target)
                            moved += 1
                    except OSError as failed:
                        log.warning("a raw record was not settled: %s", type(failed).__name__)
        return moved

    # ── The units, and what was moved ────────────────────────────────────

    def units(self, kind: str) -> list[Unit]:
        """Every unit of `kind` in the storage, oldest first within an
        account: the day directories of each account's reads, or the month
        directories of its activity records. Empty for the stand-in."""
        if self.storage is None:
            return []
        found: list[Unit] = []
        for account in self._accounts():
            if kind == RESPONSES:
                for day in _subdirectories(account, _DAY):
                    times = {
                        f.name: _ns(moment)
                        for f in _files(day)
                        if (moment := _moment_of(f.name.removesuffix(_SUFFIX))) is not None
                    }
                    if times:
                        found.append(self._unit(kind, day, times))
            else:
                for month in _subdirectories(account / ACTIVITY_DIRECTORY, _MONTH):
                    times = {}
                    for f in _files(month):
                        try:
                            times[f.name] = f.stat().st_mtime_ns
                        except OSError:
                            continue
                    if times:
                        found.append(self._unit(kind, month, times))
        return found

    def _unit(self, kind: str, path: Path, times: dict[str, int]) -> Unit:
        return Unit(
            kind=kind,
            unit=self.unit_of(path),
            path=path,
            record_count=len(times),
            first_received_ns=max(1, min(times.values())),
            last_received_ns=max(1, max(times.values())),
            files=tuple(sorted(times)),
        )

    def _ledger_file(self, unit_path: Path) -> Path:
        """An account's ledger, beside its units: the account's directory."""
        account = unit_path.parent
        if account.name == ACTIVITY_DIRECTORY:
            account = account.parent
        return account / LEDGER

    def note(self, unit: Unit, now: datetime) -> None:
        """Note a unit in its account's ledger before it is moved, so the Raw
        responses tab lists it and a record in it is found again whatever
        becomes of the move: the SDK's index says whether it moved."""
        ledger = self._ledger_file(unit.path)
        held = dict(self._read_ledger(ledger))
        held[unit.unit] = Moved(
            unit.kind,
            unit.unit,
            unit.record_count,
            unit.first_received_ns,
            unit.last_received_ns,
            unit.files,
            now.isoformat(),
        )
        body = {
            "units": {
                name: {
                    "kind": m.kind,
                    "record_count": m.record_count,
                    "first_received_ns": m.first_received_ns,
                    "last_received_ns": m.last_received_ns,
                    "files": list(m.files),
                    "at": m.at,
                }
                for name, m in held.items()
            }
        }
        made, written = tempfile.mkstemp(dir=ledger.parent, suffix=".tmp")
        try:
            with os.fdopen(made, "w") as file:
                json.dump(body, file, sort_keys=True)
            os.replace(written, ledger)
        except BaseException:
            Path(written).unlink(missing_ok=True)
            raise

    def _read_ledger(self, ledger: Path) -> dict[str, Moved]:
        try:
            stamp = ledger.stat().st_mtime_ns
        except OSError:
            return {}
        cached = self._ledgers.get(ledger)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            body = json.loads(ledger.read_text())
            held = {
                name: Moved(
                    str(m["kind"]),
                    name,
                    int(m["record_count"]),
                    int(m["first_received_ns"]),
                    int(m["last_received_ns"]),
                    tuple(str(f) for f in m["files"]),
                    str(m["at"]),
                )
                for name, m in body["units"].items()
            }
        except (OSError, ValueError, KeyError, TypeError) as failed:
            log.warning("a ledger of moved units was not read: %s", type(failed).__name__)
            return {}
        self._ledgers[ledger] = (stamp, held)
        return held

    def noted(self, unit: Unit) -> Moved | None:
        """The ledger's note of a unit, or None."""
        return self._read_ledger(self._ledger_file(unit.path)).get(unit.unit)

    def moved(self, external_account_id: str) -> list[Moved]:
        """The units of the account this plugin moved or asked to move, the
        newest first."""
        held = self._read_ledger(self._directory(external_account_id) / LEDGER)
        return sorted(held.values(), key=lambda m: m.last_received_ns, reverse=True)

    def _moved_holding(
        self, external_account_id: str, kind_dir: str, name: str
    ) -> Moved | None:
        """The moved unit holding the file `name`, of reads (`kind_dir` "")
        or activity records (ACTIVITY_DIRECTORY), or None."""
        kind = ACTIVITY if kind_dir == ACTIVITY_DIRECTORY else RESPONSES
        for moved in self.moved(external_account_id):
            if moved.kind == kind and name in moved.files:
                return moved
        return None

    def moved_read(self, external_account_id: str, read: str) -> Moved | None:
        """The moved unit holding the account's read by its key, or None."""
        return self._moved_holding(external_account_id, "", f"{read}{_SUFFIX}")

    def moved_activity(self, external_account_id: str, activity_id: str) -> Moved | None:
        """The moved unit holding the account's activity record, or None."""
        name = f"{_digest(activity_id)}{_SUFFIX}"
        return self._moved_holding(external_account_id, ACTIVITY_DIRECTORY, name)

    # ── An activity's record ─────────────────────────────────────────────

    def _activity_file(self, external_account_id: str, activity_id: str) -> Path | None:
        """Where an activity's record is: in its month, in 0.12.0's flat
        place, or restored; None where it is in none."""
        name = f"{_digest(activity_id)}{_SUFFIX}"
        base = self._directory(external_account_id) / ACTIVITY_DIRECTORY
        restored = self._restored(base)
        directories = [base, *_subdirectories(base, _MONTH)]
        if restored is not None:
            directories += _subdirectories(restored, _MONTH)
        for directory in directories:
            if (directory / name).is_file():
                return directory / name
        return None

    def history_reach_days(self) -> int:
        """How far back the history SnapTrade reported reaches: the most days
        any kept activity record's activity was traded before its record was
        received; 0 where none is kept. Read from the records once, then kept
        up as each is written."""
        if self._reach is None:
            reach = 0
            for account in self._accounts():
                base = account / ACTIVITY_DIRECTORY
                for directory in (base, *_subdirectories(base, _MONTH)):
                    for file in _files(directory):
                        try:
                            with gzip.open(file) as opened:
                                document = parse_exact(opened.read())
                        except (OSError, ValueError, EOFError):
                            continue
                        if isinstance(document, dict):
                            reach = max(reach, _reach_of(document))
            self._reach = reach
        return self._reach

    def kept_for(self) -> timedelta:
        """How long an activity record stays before a window may delete it:
        its window, and never shorter than the history reported reaches, nor
        longer than the most a window may be."""
        days = max(self.activity_window.days, self.history_reach_days())
        return timedelta(days=min(days, MOST_ACTIVITY_RETENTION_DAYS))

    def activity_record(self, external_account_id: str, activity_id: str) -> Record | None:
        """A reported activity's record, or None where none is kept."""
        file = self._activity_file(external_account_id, activity_id)
        if file is None:
            return None
        try:
            with gzip.open(file) as opened:
                document = parse_exact(opened.read())
        except (OSError, ValueError, EOFError):
            return None
        if (
            not isinstance(document, dict)
            or document.get("external_account_id") != external_account_id
            or document.get("activity_id") != activity_id
        ):
            return None
        try:
            read_at = datetime.fromisoformat(str(document.get("read_at")))
        except ValueError:
            return None
        calls = document.get("calls")
        return Record(
            key=f"{ACTIVITY_KEYS}/{external_account_id}/{activity_id}",
            read_at=read_at,
            external_account_id=external_account_id,
            synthetic=document.get("source") == "synthetic",
            calls=tuple(c for c in calls if isinstance(c, dict))
            if isinstance(calls, list)
            else (),
            document=document,
            restored=self.storage is not None and RESTORE_AREA.parts[0] in file.parts,
        )

    # ── An account's reads ───────────────────────────────────────────────

    def _read_directories(self, external_account_id: str) -> list[tuple[Path, bool]]:
        """Where the account's reads are: 0.12.0's flat place, each day in
        storage, and each day restored; each with whether it is restored."""
        account = self._directory(external_account_id)
        restored = self._restored(account)
        return [
            (account, False),
            *((day, False) for day in _subdirectories(account, _DAY)),
            *(
                ((day, True) for day in _subdirectories(restored, _DAY))
                if restored is not None
                else ()
            ),
        ]

    def reads(self, external_account_id: str) -> list[Read]:
        """The account's kept reads, newest first: in storage, and restored
        from the archive."""
        listed: dict[str, Read] = {}
        for directory, restored in self._read_directories(external_account_id):
            for file in _files(directory):
                key = file.name.removesuffix(_SUFFIX)
                moment = _moment_of(key)
                if moment is not None and key not in listed:
                    listed[key] = Read(key, moment, restored)
        return sorted(listed.values(), key=lambda read: _order(read.key), reverse=True)

    def record(self, external_account_id: str, key: str) -> Record | None:
        """One kept read of the account, in storage or restored, or None
        where there is none by that key (or it has moved)."""
        moment = _moment_of(key)
        if moment is None:
            return None
        account = self._directory(external_account_id)
        candidates = [(account / f"{key}{_SUFFIX}", False)]
        candidates.append((account / _day_of(key) / f"{key}{_SUFFIX}", False))
        restored = self._restored(account / _day_of(key))
        if restored is not None:
            candidates.append((restored / f"{key}{_SUFFIX}", True))
        for file, was_restored in candidates:
            try:
                with gzip.open(file) as opened:
                    document = parse_exact(opened.read())
            except (OSError, ValueError, EOFError):
                continue
            if (
                not isinstance(document, dict)
                or document.get("external_account_id") != external_account_id
            ):
                return None
            calls = document.get("calls")
            return Record(
                key=key,
                read_at=moment,
                external_account_id=external_account_id,
                synthetic=document.get("source") == "synthetic",
                calls=tuple(c for c in calls if isinstance(c, dict))
                if isinstance(calls, list)
                else (),
                document=document,
                restored=was_restored,
            )
        return None

    def find(self, key: str) -> tuple[Record, Json] | None:
        """The record and the call a row's reference names (contract v11), or
        None where it names none this store holds now: a key that is not one,
        or a record moved past its window and not restored."""
        activity = parse_activity_key(key)
        if activity is not None:
            reported = self.activity_record(*activity)
            return (reported, reported.calls[0]) if reported and reported.calls else None
        named = parse_record_key(key)
        if named is None:
            return None
        account, read, call = named
        found = self.record(account, read)
        if found is None:
            return None
        asked = CALLS[call][0]
        for held in found.calls:
            if held.get("call") == asked:
                return found, held
        return None

    def latest(self, external_account_id: str) -> Record | None:
        """The account's newest kept read that can be read back."""
        for read in self.reads(external_account_id):
            found = self.record(external_account_id, read.key)
            if found is not None:
                return found
        return None
