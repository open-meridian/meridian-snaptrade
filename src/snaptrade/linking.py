"""Linking each external account to one of the deployment's accounts (W6.4),
on the Account links tab, for the deployment admin viewing it.

Ruled by the product owner on 2026-09-28 (kernel/a-plugins-admin-view, point
8): each plugin manages its own external accounts' links, on its admin page,
and the dashboard lists and links none. The link is this plugin's right to
the account: a statement for an account nothing links is refused.

Every call here is sent acting for the person the page request came from,
their `Meridian-Caller` header handed back as `acting_for`, and the sidecar
refuses it unless that person is a deployment admin. A link names an existing
account, or a new account's name for the conductor to create and link in one
step, or neither, to remove the link.

What an external account is linked to, this plugin cannot read: the contract
gives it no read of its own links. So it says what it knows and how: a link it
made or removed itself since it started, then what the last read showed
(rows recorded for the account, which only a link allows, or refused because
nothing links it), and otherwise that it is not known.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

import meridian
from meridian.plugin.v1 import operations_pb2 as ops

from .contract import Outcome
from .normalise import AccountView


class Link(Enum):
    LINKED = "linked"
    UNLINKED = "unlinked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LinkView:
    """One external account's link, as far as this plugin knows it."""

    state: Link
    # The deployment's account it is linked to, when this plugin linked it.
    account_id: str = ""
    # How it is known, for the page.
    how: str = ""


@dataclass(frozen=True)
class DeploymentAccount:
    """One of the deployment's accounts, as a link may name it."""

    account_id: str
    name: str
    open: bool = True
    # Where it is held and what it is, as the deployment describes it (W6.3),
    # to tell accounts of one name apart. Either may be empty.
    custodian: str = ""
    account_type: str = ""

    def label(self) -> str:
        """Its name, with its custodian and type beside it where it has them."""
        name = self.name or self.account_id
        described = ", ".join(filter(None, (self.custodian, self.account_type)))
        return f"{name} ({described})" if described else name


@dataclass(frozen=True)
class Offered:
    """The deployment's accounts, read for the admin viewing the page, or why
    they could not be."""

    accounts: tuple[DeploymentAccount, ...] = ()
    refused: str = ""

    def name_of(self, account_id: str) -> str:
        return next((a.name for a in self.accounts if a.account_id == account_id), "")


def refusal(failed: meridian.MeridianError) -> str:
    """The sidecar's own words for a refusal, to show as they are."""
    if isinstance(failed, meridian.CallFailed) and failed.detail:
        return failed.detail
    return str(failed)


class Links:
    """Sends links for the admin viewing the page, and keeps what it sent."""

    def __init__(self, plugin: meridian.Plugin) -> None:
        self._plugin = plugin
        # By external account ID: the account this process linked it to, or
        # "" where it removed the link.
        self._made: dict[str, str] = {}
        self._lock = threading.Lock()

    def of(self, view: AccountView, outcome: Outcome | None) -> LinkView:
        """What is known of one external account's link."""
        with self._lock:
            made = self._made.get(view.account.external_account_id)
        return link_of(made, outcome)

    async def offered(self, acting_for: str) -> Offered:
        """The deployment's accounts, for the admin the header names."""
        try:
            read = await self._plugin.read_accounts_for_linking(acting_for=acting_for)
        except meridian.MeridianError as failed:
            return Offered(refused=refusal(failed))
        return Offered(
            tuple(
                DeploymentAccount(
                    account.account_id,
                    account.name,
                    account.state != ops.ACCOUNT_STATE_CLOSED,
                    account.custodian,
                    account.account_type,
                )
                for account in read.accounts
            )
        )

    async def link(
        self,
        acting_for: str,
        external_account_id: str,
        account_id: str = "",
        name: str = "",
        custodian: str = "",
        account_type: str = "",
    ) -> str:
        """Link to `account_id`, or to a new account called `name`, held at
        `custodian` and of `account_type` as the admin left them, or with
        neither, remove the link. Returns the account linked to, or "". A
        refusal is raised as the SDK raises it."""
        linked = await self._plugin.link_external_account(
            external_account_id=external_account_id,
            account_id=account_id,
            new_account_name=name,
            new_account_custodian=custodian if name else "",
            new_account_type=account_type if name else "",
            acting_for=acting_for,
        )
        with self._lock:
            self._made[external_account_id] = linked.account_id
        return linked.account_id


def link_of(made: str | None, outcome: Outcome | None) -> LinkView:
    """A link this process made or removed, else what the last read showed,
    else not known."""
    if made is not None:
        if made:
            return LinkView(Link.LINKED, made, "Linked from this page.")
        return LinkView(Link.UNLINKED, how="Unlinked from this page.")
    if outcome is not None and outcome.recorded:
        return LinkView(Link.LINKED, how="Its rows were recorded on the last read.")
    if outcome is not None and outcome.unlinked:
        return LinkView(
            Link.UNLINKED,
            how=(
                "Its rows were refused on the last read: nothing is recorded until it is "
                "linked."
            ),
        )
    return LinkView(
        Link.UNKNOWN,
        how=(
            "Nothing was recorded for it on the last read, so whether it is linked is "
            "not known."
        ),
    )
