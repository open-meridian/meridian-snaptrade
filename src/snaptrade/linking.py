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
step, or neither, to remove the link. A link to another account replaces the
one standing: the conductor keeps one link per external account.

What each external account is linked to, and that account's name, this plugin
reads beside its account scope (W4.11): `__main__` holds the first delivery
before the pages are served and every one after it, and hands each to
`Links`. So after a restart every link is known, and an external account no
link names is not linked. There is no third state, and nothing is kept here
beyond the latest delivery: plugins are ephemeral.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from enum import Enum

import meridian
from meridian.plugin.v1 import operations_pb2 as ops

# How long a link sent from the page waits for the account scope to show it,
# so the page answering the form shows the link as it now stands.
SETTLE_SECONDS = 5.0


class Link(Enum):
    LINKED = "linked"
    UNLINKED = "unlinked"


@dataclass(frozen=True)
class LinkView:
    """One external account's link, as the plugin's account scope gives it."""

    state: Link
    # The deployment's account it is linked to, and that account's name as
    # the deployment holds it now (W6.3); both empty when it is not linked.
    account_id: str = ""
    account_name: str = ""


def link_of(scope: meridian.AccountScope, external_account_id: str) -> LinkView:
    """One external account's link in `scope`: linked, naming the account, or
    not linked."""
    link = scope.link_of(external_account_id)
    if link is None:
        return LinkView(Link.UNLINKED)
    return LinkView(Link.LINKED, link.account_id, link.account_name)


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


def refusal(failed: meridian.MeridianError) -> str:
    """The sidecar's own words for a refusal, to show as they are."""
    if isinstance(failed, meridian.CallFailed) and failed.detail:
        return failed.detail
    return str(failed)


class Links:
    """The plugin's links as its account scope last gave them, and links sent
    for the admin viewing the page."""

    def __init__(self, plugin: meridian.Plugin, settle_seconds: float = SETTLE_SECONDS) -> None:
        self._plugin = plugin
        self._settle = settle_seconds
        self._scope = meridian.AccountScope()
        # The page reads from its own threads; deliveries arrive on the loop.
        self._lock = threading.Lock()
        self._changed = asyncio.Condition()

    @property
    def scope(self) -> meridian.AccountScope:
        with self._lock:
            return self._scope

    async def hold(self, scope: meridian.AccountScope) -> None:
        """Keep the latest delivery of the account scope, in place of the last."""
        with self._lock:
            self._scope = scope
        async with self._changed:
            self._changed.notify_all()

    def of(self, external_account_id: str) -> LinkView:
        """One external account's link, as the latest delivery gives it."""
        return link_of(self.scope, external_account_id)

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
        neither, remove the link; a link replaces the one standing. Returns
        the account linked to, or "", once the account scope shows it or a
        few seconds have passed. A refusal is raised as the SDK raises it."""
        linked = await self._plugin.link_external_account(
            external_account_id=external_account_id,
            account_id=account_id,
            new_account_name=name,
            new_account_custodian=custodian if name else "",
            new_account_type=account_type if name else "",
            acting_for=acting_for,
        )
        await self._settled(external_account_id, linked.account_id)
        return linked.account_id

    async def _settled(self, external_account_id: str, account_id: str) -> None:
        """Wait until the account scope shows `external_account_id` linked to
        `account_id` (or, for "", not linked), or until the wait runs out."""

        def shown() -> bool:
            return self.of(external_account_id).account_id == account_id

        with contextlib.suppress(TimeoutError):
            async with self._changed:
                await asyncio.wait_for(self._changed.wait_for(shown), self._settle)
