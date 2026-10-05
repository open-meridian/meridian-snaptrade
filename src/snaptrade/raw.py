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
only where no storage is given -- a deployment from before it, a test -- in
`/tmp` (STAND_IN), which lasts as long as the pod. Records older than the retention are pruned.

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
recorded. Kept `ACTIVITY_RETENTION_DAYS` from when it was received, not the
read retention, so a reported activity's record can be read back as long as
the street holds the activity it explains; `find` resolves it as it does a
row's.

Each record is one gzipped JSON file, `<root>/<account>/<read>.json.gz`, the
account directory a hash of the external account ID (so no ID names a path)
and the read its time, which is the record's key. A record is written whole
or not at all. Records older than the retention are pruned when the settings
first arrive, as the plugin starts, whenever the retention is changed, and
after each read.
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
#: How long a reported activity's record is kept from when it was received:
#: seven years, past the two SnapTrade holds of an account's history at
#: Fidelity, so each activity the street holds can have its record read back.
ACTIVITY_RETENTION_DAYS = 2555
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
    """One kept read of one account, as listed: its key and when it was."""

    key: str
    read_at: datetime


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


def _moment_of(key: str) -> datetime | None:
    matched = _READ.match(key)
    if matched is None:
        return None
    return datetime.strptime(matched.group(1), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)


def _order(key: str) -> tuple[str, int]:
    matched = _READ.match(key)
    return (key, 0) if matched is None else (matched.group(1), int(matched.group(2) or 0))


class RawStore:
    """The records, under `root`, kept for `retention`.

    Every call runs on the plugin's loop, as the reads and the pages do, so a
    record is never pruned while it is being written. A record is written to
    a temporary file and renamed into place, so a reader sees it whole or
    not at all."""

    def __init__(
        self, root: Path, retention: timedelta = timedelta(days=DEFAULT_RETENTION_DAYS)
    ) -> None:
        self.root = root
        self.retention = retention
        self.activity_retention = timedelta(days=ACTIVITY_RETENTION_DAYS)
        # Why the last read's records could not be kept, when they could not:
        # safe to show (the error's type alone).
        self.failure = ""

    def _directory(self, external_account_id: str) -> Path:
        digest = hashlib.sha256(external_account_id.encode()).hexdigest()[:32]
        return self.root / f"a-{digest}"

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

    def _activity_file(self, external_account_id: str, activity_id: str) -> Path:
        digest = hashlib.sha256(activity_id.encode()).hexdigest()[:32]
        return self._directory(external_account_id) / ACTIVITY_DIRECTORY / f"{digest}{_SUFFIX}"

    def keep_activity(self, one: Taken, secrets: Iterable[str] = ()) -> bool:
        """Write one activity's record where none is kept for it, its
        credentials redacted first: whether one is kept now. The first kept
        stands, as the street keeps the first report of an activity."""
        record = one.record
        file = self._activity_file(one.external_account_id, str(record["activity_id"]))
        if file.exists():
            return True
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            self._replace(file, redact(record, tuple(secrets)))
            # The file's time is when the activity was received, which its
            # retention runs from.
            received = datetime.fromisoformat(str(record["read_at"])).timestamp()
            os.utime(file, (received, received))
        except OSError as failed:
            log.warning("an activity's raw response was not kept: %s", type(failed).__name__)
            return False
        return True

    def activity_record(self, external_account_id: str, activity_id: str) -> Record | None:
        """A reported activity's record, or None where none is kept."""
        try:
            with gzip.open(self._activity_file(external_account_id, activity_id)) as file:
                document = parse_exact(file.read())
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
        )

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
        directory = self._directory(one.external_account_id)
        directory.mkdir(parents=True, exist_ok=True)
        base = _key_of(datetime.fromisoformat(str(record["read_at"])))
        key, again = base, 1
        while (directory / f"{key}{_SUFFIX}").exists():
            again += 1
            key = f"{base}-{again}"
        self._replace(directory / f"{key}{_SUFFIX}", record)
        return key

    def prune(self, now: datetime) -> int:
        """Remove every record read before `now` less the retention, and what
        a write that stopped left; how many records were removed."""
        oldest = now - self.retention
        removed = 0
        try:
            directories = [d for d in self.root.iterdir() if d.is_dir()]
        except FileNotFoundError:
            return 0
        except OSError as failed:
            log.warning("the raw responses were not pruned: %s", type(failed).__name__)
            return 0
        for directory in directories:
            removed += self._prune_activities(directory / ACTIVITY_DIRECTORY, now)
            try:
                for file in directory.iterdir():
                    if file.is_dir():
                        continue
                    if file.name.endswith(".tmp"):
                        file.unlink(missing_ok=True)
                        continue
                    moment = _moment_of(file.name.removesuffix(_SUFFIX))
                    if moment is not None and moment < oldest:
                        file.unlink(missing_ok=True)
                        removed += 1
                if not any(directory.iterdir()):
                    directory.rmdir()
            except OSError as failed:
                log.warning("the raw responses were not pruned: %s", type(failed).__name__)
        return removed

    def _prune_activities(self, directory: Path, now: datetime) -> int:
        """Remove each activity record received before `now` less the
        activity retention, by its file's time, which is set to when it was
        received; and what a write that stopped left."""
        oldest = (now - self.activity_retention).timestamp()
        removed = 0
        try:
            files = list(directory.iterdir())
        except OSError:
            return 0
        for file in files:
            try:
                if file.name.endswith(".tmp"):
                    file.unlink(missing_ok=True)
                elif file.stat().st_mtime < oldest:
                    file.unlink(missing_ok=True)
                    removed += 1
            except OSError as failed:
                log.warning("an activity record was not pruned: %s", type(failed).__name__)
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
        return removed

    def reads(self, external_account_id: str) -> list[Read]:
        """The account's kept reads, newest first."""
        try:
            names = [f.name for f in self._directory(external_account_id).iterdir()]
        except OSError:
            return []
        keys = [n.removesuffix(_SUFFIX) for n in names if n.endswith(_SUFFIX)]
        listed = [Read(key, moment) for key in keys if (moment := _moment_of(key)) is not None]
        return sorted(listed, key=lambda read: _order(read.key), reverse=True)

    def record(self, external_account_id: str, key: str) -> Record | None:
        """One kept read of the account, or None where there is none by that
        key (or it has been pruned)."""
        moment = _moment_of(key)
        if moment is None:
            return None
        try:
            with gzip.open(self._directory(external_account_id) / f"{key}{_SUFFIX}") as file:
                document = parse_exact(file.read())
        except (OSError, ValueError, EOFError):
            return None
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
        )

    def find(self, key: str) -> tuple[Record, Json] | None:
        """The record and the call a row's reference names (contract v11), or
        None where it names none this store holds: a key that is not one, or
        a record pruned under its retention."""
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
