"""The pages as synthetic mode would show them, each printed as one HTML file:
for looking at the pages without a deployment.

    python -m snaptrade.preview accounts > preview/accounts.html

The page is connections (the default), accounts or statements. Each links the kit
where the dashboard serves it, /.meridian/ui/<version>/, so serve it beside
the kit to see it styled (meridian-ui's `make serve` serves the kit under that
path); opened on its own it is the page without the kit, unstyled, which must
work too.

There is no sidecar here, so nothing is recorded. The Account links tab shows
one account of each kind of link, as if the last read had recorded the first,
had the second's rows refused for want of a link, and had not recorded the
third yet; and the deployment's accounts it offers are invented, like the
rest. Statements is as a reader sees it who may read the two accounts linked
from the Account links tab, and not the third.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import meridian

from .contract import Outcome
from .linking import DeploymentAccount, LinkView, Offered, link_of
from .normalise import views
from .page import render_accounts, render_connections, render_statements, visible
from .settings import Config
from .sync import Status
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


def _linked(status: Status) -> tuple[Status, dict[str, LinkView]]:
    """The read with the first account's rows recorded and linked from this
    page, the second's refused for want of a link, and the rest not recorded
    yet; and each one's link as the page would know it."""
    first, second, *rest = status.accounts
    unlinked = second.account.external_account_id
    rows = [len(view.statement.holdings) if view.statement else 0 for view in (first, second)]
    outcomes = {
        first.account.external_account_id: Outcome(rows=rows[0], recorded=rows[0]),
        second.account.external_account_id: Outcome(
            rows=rows[1],
            stopped=(
                f"RecordHolding: refused: external account {unlinked} is not linked to an "
                "account"
            ),
            unlinked=True,
        ),
    }
    links = {
        first.account.external_account_id: link_of("ACC-1002", None),
        second.account.external_account_id: link_of(
            None, outcomes[second.account.external_account_id]
        ),
        **{view.account.external_account_id: link_of(None, None) for view in rest},
    }
    return replace(status, outcomes=outcomes), links


def _read(status: Status) -> str:
    """Statements for a reader who may read ACC-1001 and ACC-1002, to which
    the first two accounts were linked from the Account links tab, and whose
    rows the last read recorded."""
    first, second, *rest = status.accounts
    outcomes = {
        view.account.external_account_id: Outcome(
            rows=len(view.statement.holdings), recorded=len(view.statement.holdings)
        )
        for view in (first, second)
        if view.statement is not None
    }
    links = {
        first.account.external_account_id: link_of("ACC-1001", None),
        second.account.external_account_id: link_of("ACC-1002", None),
        **{view.account.external_account_id: link_of(None, None) for view in rest},
    }
    reader = meridian.Caller(
        "reader", "A Reader", "preview", read=frozenset({"ACC-1001", "ACC-1002"})
    )
    read = replace(status, outcomes=outcomes)
    return render_statements(read, visible(read, links, reader), everyone=False)


def main() -> None:
    page = sys.argv[1] if len(sys.argv) > 1 else "connections"
    status = asyncio.run(_status())
    if page == "accounts":
        linked, links = _linked(status)
        print(render_accounts(linked, "preview", links, OFFERED))
    elif page == "statements":
        print(_read(status))
    elif page == "connections":
        print(render_connections(status, "preview"))
    else:
        sys.exit(f"no page {page!r}: connections, accounts or statements")


if __name__ == "__main__":
    main()
