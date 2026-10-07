"""The pages as synthetic mode would show them, each printed as one HTML file:
for looking at the pages without a deployment.

    python -m snaptrade.preview accounts > preview/accounts.html

The page is connections (the default), accounts, statements, raw, raw-kept or
raw-archive. Each is served
by its own view, as the sidecar would ask it, and links the kit where the
dashboard serves it, /.meridian/ui/<version>/, so serve it beside the kit to
see it styled (meridian-ui's `make serve` serves the kit under that path);
opened on its own it is the page without the kit, unstyled, which must work
too.

There is no sidecar here, so nothing is recorded. Connections and Account
links are as a deployment admin sees them under Manage. The Account links tab
shows the first account linked, as the plugin's account scope would give it,
and the other two not linked, as if the last read had recorded the first, had
the second's rows refused for want of a link, and had not recorded the third
yet; the deployment's accounts it offers are invented, like the rest.
Statements is as a reader sees it under View who may read the accounts the
first two are linked to, and not the third, which nothing links; Raw
responses is the same reader's, the synthetic read kept twice an hour apart
in a temporary directory, removed after. Its kept reads (raw-kept) are forty
reads an hour apart, paged; its archive (raw-archive) is as the same person
sees it under Open, thirty days of each account's reads and two months of
its activity moved past their window, most archived, one restored and one
deleted, as the SDK's index would say.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import meridian
from meridian.testing import caller_header
from meridian.v1 import sidecar_pb2

from .contract import Outcome
from .linking import DeploymentAccount, Links, Offered
from .normalise import views
from .page import ACCOUNTS, CONNECTIONS, RAW, RAW_ARCHIVE, STATEMENTS, hold, pages
from .raw import RESTORE_AREA, RawStore, Unit, taken
from .settings import Config
from .sync import Status, Syncer
from .synthetic import USER_ID, SyntheticVenue
from .venue import read, utc_now

OFFERED = Offered(
    (
        DeploymentAccount("ACC-1001", "Household brokerage", custodian="Schwab"),
        DeploymentAccount(
            "ACC-1002", "Alpaca margin", custodian="Alpaca", account_type="margin"
        ),
        DeploymentAccount("ACC-1003", "Old retirement", open=False),
    )
)


class _Offering(Links):
    """The plugin's links with no sidecar behind them: the deployment's
    accounts it offers are OFFERED."""

    async def offered(self, acting_for: str) -> Offered:
        return OFFERED


async def _status() -> Status:
    venue = SyntheticVenue(utc_now)
    snapshot = await read(venue, utc_now)
    return Status(
        mode="synthetic",
        read_at=snapshot.read_at,
        connections=views(snapshot, Config().stale_after),
        users=tuple(await venue.users()),
        user_id=USER_ID,
    )


class _Index:
    """Where each moved unit stands, as the SDK's index would answer
    `find_record`: the preview has no sidecar and no index of its own."""

    def __init__(self, outcomes: dict[str, sidecar_pb2.MoveOutcome]) -> None:
        self.outcomes = outcomes

    def find_record(self, key: str) -> sidecar_pb2.RecordMoveRequest | None:
        held = next((u for u in self.outcomes if key == u or key.startswith(u + "/")), None)
        if held is None:
            return None
        return sidecar_pb2.RecordMoveRequest(unit=held, outcome=self.outcomes[held])


def _shown(
    status: Status,
    scope: meridian.AccountScope,
    path: str,
    header: str,
    raw: RawStore | None = None,
    query: dict[str, str] | None = None,
    index: _Index | None = None,
) -> str:
    """The page at `path`, served by its view for the caller `header` names,
    from `status`, the links `scope` holds and the raw responses `raw` keeps."""
    plugin = cast(meridian.Plugin, index)
    syncer = Syncer(plugin, raw=raw)
    syncer.status = status
    links = _Offering(plugin)
    asyncio.run(links.hold(scope))
    hold(syncer, links, asyncio.Event())
    request = meridian.Request(
        "GET", path, meridian.Caller.from_header(header), plugin, query=query or {}
    )
    answer = asyncio.run(pages.dispatch(request))
    if answer.status != 200:
        sys.exit(f"{path} answered {answer.status}: {answer.text}")
    return answer.text


# A deployment admin, under Manage: they are offered a new account too.
MANAGING = caller_header("admin", subject="preview|admin", deployment_admin=True)


def _linked(status: Status) -> tuple[Status, meridian.AccountScope]:
    """The read with the first account linked to ACC-1002 and its rows
    recorded, the second's refused for want of a link, and the third not
    recorded yet; and the links as the account scope gives them."""
    first, second, *_ = status.accounts
    unlinked = second.account.external_account_id
    rows = [len(view.statement.holdings) if view.statement else 0 for view in (first, second)]
    refused = meridian.NotLinked(
        "RecordHoldingsStatement", f"external account {unlinked} is not linked to an account"
    )
    outcomes = {
        first.account.external_account_id: Outcome(rows=rows[0], recorded=rows[0]),
        unlinked: Outcome(rows=rows[1], stopped=str(refused), unlinked=True),
    }
    scope = meridian.AccountScope(
        links=(
            meridian.LinkedExternalAccount(
                first.account.external_account_id, "ACC-1002", "Alpaca margin"
            ),
        )
    )
    return replace(status, outcomes=outcomes), scope


def _read(
    status: Status,
    path: str = STATEMENTS,
    raw: RawStore | None = None,
    query: dict[str, str] | None = None,
    index: _Index | None = None,
    level: str = "read",
) -> str:
    """Statements (or `path`) under View (or `level`), for a reader who may
    read ACC-1001 and ACC-1002, to which the first two accounts are linked,
    and whose rows the last read recorded."""
    first, second, *_ = status.accounts
    outcomes = {
        view.account.external_account_id: Outcome(
            rows=len(view.statement.holdings), recorded=len(view.statement.holdings)
        )
        for view in (first, second)
        if view.statement is not None
    }
    scope = meridian.AccountScope(
        links=(
            meridian.LinkedExternalAccount(
                first.account.external_account_id, "ACC-1001", "Household brokerage"
            ),
            meridian.LinkedExternalAccount(
                second.account.external_account_id, "ACC-1002", "Alpaca margin"
            ),
        )
    )
    mine = ("ACC-1001", "ACC-1002")
    reader = caller_header(
        level,
        read=mine,
        write=mine if level == "write" else (),
        subject="preview|reader",
        display_name="A Reader",
    )
    return _shown(replace(status, outcomes=outcomes), scope, path, reader, raw, query, index)


def _keep(store: RawStore, hours: int) -> None:
    """The synthetic read kept `hours` times, an hour apart."""
    for ago in range(hours - 1, -1, -1):

        def then(ago: int = ago) -> datetime:
            return utc_now() - timedelta(hours=ago)

        snapshot = asyncio.run(read(SyntheticVenue(then), then))
        store.keep(taken(snapshot, views(snapshot, Config().stale_after), synthetic=True))


def _raw(status: Status, view: str = "read") -> str:
    """Raw responses for the same reader: the synthetic read kept twice, an
    hour apart; or forty times, its kept reads paged; or its archive."""
    with tempfile.TemporaryDirectory() as kept:
        storage = Path(kept)
        store = RawStore(storage / "raw-responses", storage)
        if view == "archive":
            _keep(store, 1)
            return _read(status, RAW_ARCHIVE, store, index=_archived(store), level="write")
        _keep(store, 40 if view == "kept" else 2)
        first = status.accounts[0].account.external_account_id
        query = {"account": first, "view": "kept"} if view == "kept" else None
        return _read(status, RAW, store, query)


def _archived(store: RawStore) -> _Index:
    """Thirty days of each account's reads and two months of its activity,
    noted in its ledger as moved past their window: one day restored, the
    oldest deleted, the rest archived."""
    assert store.storage is not None
    outcomes: dict[str, sidecar_pb2.MoveOutcome] = {}
    now = utc_now().replace(hour=15, minute=0, second=0, microsecond=0)
    for account in sorted(store.root.iterdir()):
        days = [
            (
                account / f"{now - timedelta(days=40 + n):%Y-%m-%d}",
                288,
                now - timedelta(days=40 + n),
            )
            for n in range(30)
        ]
        months = [
            (
                account / "activities" / f"{now - timedelta(days=31 * m):%Y-%m}",
                41,
                now - timedelta(days=31 * m),
            )
            for m in (13, 14)
        ]
        for path, count, moment in days + months:
            kind = "activity" if path.parent.name == "activities" else "responses"
            at = int(moment.timestamp()) * 1_000_000_000
            unit = Unit(kind, store.unit_of(path), path, count, at - 3600 * 10**9 * 8, at, ())
            path.parent.mkdir(parents=True, exist_ok=True)
            store.note(unit, now)
            outcomes[unit.unit] = sidecar_pb2.MOVE_OUTCOME_ARCHIVED
        restored = store.unit_of(days[2][0])
        outcomes[restored] = sidecar_pb2.MOVE_OUTCOME_RESTORED
        (store.storage / RESTORE_AREA / restored).mkdir(parents=True)
        outcomes[store.unit_of(days[-1][0])] = sidecar_pb2.MOVE_OUTCOME_DELETED
    return _Index(outcomes)


def main() -> None:
    page = sys.argv[1] if len(sys.argv) > 1 else "connections"
    status = asyncio.run(_status())
    if page == "accounts":
        linked, scope = _linked(status)
        print(_shown(linked, scope, ACCOUNTS, MANAGING))
    elif page == "statements":
        print(_read(status))
    elif page == "raw":
        print(_raw(status))
    elif page == "raw-kept":
        print(_raw(status, "kept"))
    elif page == "raw-archive":
        print(_raw(status, "archive"))
    elif page == "connections":
        print(_shown(status, meridian.AccountScope(), CONNECTIONS, MANAGING))
    else:
        sys.exit(
            f"no page {page!r}: connections, accounts, statements, raw, raw-kept or raw-archive"
        )


if __name__ == "__main__":
    main()
