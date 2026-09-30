"""The pages as synthetic mode would show them, each printed as one HTML file:
for looking at the pages without a deployment.

    python -m snaptrade.preview accounts > preview/accounts.html

The page is connections (the default), accounts or statements. Each links the kit
where the dashboard serves it, /.meridian/ui/<version>/, so serve it beside
the kit to see it styled (meridian-ui's `make serve` serves the kit under that
path); opened on its own it is the page without the kit, unstyled, which must
work too.

There is no sidecar here, so nothing is recorded. The Account links tab shows
the first account linked, as the plugin's account scope would give it, and
the other two not linked, as if the last read had recorded the first, had the
second's rows refused for want of a link, and had not recorded the third yet;
the deployment's accounts it offers are invented, like the rest. Statements
is as a reader sees it who may read the accounts the first two are linked to,
and not the third, which nothing links.
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


def _links(status: Status, scope: meridian.AccountScope) -> dict[str, LinkView]:
    return {
        view.account.external_account_id: link_of(scope, view.account.external_account_id)
        for view in status.accounts
    }


def _linked(status: Status) -> tuple[Status, dict[str, LinkView]]:
    """The read with the first account linked to ACC-1002 and its rows
    recorded, the second's refused for want of a link, and the third not
    recorded yet; and each one's link as the account scope gives it."""
    first, second, *_ = status.accounts
    unlinked = second.account.external_account_id
    rows = [len(view.statement.holdings) if view.statement else 0 for view in (first, second)]
    refused = meridian.NotLinked(
        "RecordHolding", f"external account {unlinked} is not linked to an account"
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
    return replace(status, outcomes=outcomes), _links(status, scope)


def _read(status: Status) -> str:
    """Statements for a reader who may read ACC-1001 and ACC-1002, to which
    the first two accounts are linked, and whose rows the last read
    recorded."""
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
    reader = meridian.Caller(
        "reader", "A Reader", "preview", read=frozenset({"ACC-1001", "ACC-1002"})
    )
    read = replace(status, outcomes=outcomes)
    return render_statements(read, visible(read, _links(read, scope), reader), everyone=False)


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
