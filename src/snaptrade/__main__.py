"""The SnapTrade plugin: brokerage accounts, cash and positions into an Open
Meridian deployment, through SnapTrade (plans/proving-the-plugin-system, step 1).

It connects to the sidecar it is launched beside, declares its settings and
its pages (page.py: Connections and Account links under Manage, Statements
under Open and View), serves them, and then, on every poll and every settings
change, reads SnapTrade (or, in synthetic mode, its built-in responses),
normalises what it read to the platform's convention (normalise.py), and
records it through the SDK's typed operations (contract.py): each account's
sync status, a holdings statement per account, and each linked account's
activities, a backfill back to its history's first date and then each read's
(contract v14). It holds nothing between
reads that it needs; a restart reads again. What SnapTrade answered each read
is kept per account, as received, for the Raw responses tab (raw.py), in the
storage the deployment grants an edge plugin (decisions/028), which it
declares it asks for (declaration.py). Which of its external
accounts are linked, and to what, it reads beside its account scope, holding
the first delivery before its pages are served and each one after
(linking.py).

Everything goes through the sidecar. The SnapTrade credentials arrive as the
plugin's own secret settings, set by an admin of the plugin in the dashboard,
and are never logged, shown or sent anywhere but SnapTrade.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator

import meridian

from .declaration import DECLARATION
from .linking import Links
from .page import TITLE, pages, serve
from .raw import RawStore, storage_root
from .settings import DECLARED, config_from
from .sync import Syncer

log = logging.getLogger("snaptrade")


async def watch_settings(
    plugin: meridian.Plugin, syncer: Syncer, configured: asyncio.Event, wake: asyncio.Event
) -> None:
    """Apply each delivery of the settings, and read again at once."""
    async for delivered in plugin.settings():
        syncer.configure(config_from(delivered.values, delivered.missing_required))
        configured.set()
        wake.set()


async def watch_links(scopes: AsyncIterator[meridian.AccountScope], links: Links) -> None:
    """Hold each delivery of the account scope, and so the plugin's links."""
    async for scope in scopes:
        await links.hold(scope)


async def follow_links(plugin: meridian.Plugin, links: Links) -> asyncio.Task[None]:
    """Hold the first delivery of the plugin's links, which comes at once, so
    a page served after this names every link and its account; then hold each
    one after it in a task, which is returned."""
    scopes = plugin.account_scope()
    await links.hold(await anext(scopes))
    return asyncio.create_task(watch_links(scopes, links))


async def poll(syncer: Syncer, configured: asyncio.Event, wake: asyncio.Event) -> None:
    """Read SnapTrade now, then every poll_seconds or sooner when woken."""
    await configured.wait()
    while True:
        wake.clear()
        try:
            await syncer.run_once()
        except meridian.MeridianError as failed:
            # The sidecar refused something the read needed; say so and keep
            # the schedule rather than spin.
            log.warning("this read stopped: %s", failed)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=syncer.config.poll_seconds)


async def run() -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop, stopped.set)

    port = int(os.environ.get("SNAPTRADE_PAGE_PORT", "8000"))
    async with await meridian.connect(
        # Each page with the levels it serves: the dashboard shows it under
        # those buttons, Manage, Open and View, and the SDK refuses it to a
        # session at any other level.
        interface=meridian.Interface(port=port, title=TITLE, pages=pages),
        settings=DECLARED,
        # Its secret settings' names, what it receives and does not carry,
        # and the storage it asks for its raw responses (contract v11).
        declaration=DECLARATION,
        # It names accounts by SnapTrade's identifiers, which an admin of the
        # plugin links to the deployment's accounts (W6.4).
        reads_external_accounts=True,
    ) as plugin:
        log.info(
            "registered as %s, roles %s",
            plugin.identity.instance_id,
            ", ".join(plugin.identity.roles) or "none",
        )
        links = Links(plugin)
        # The storage the deployment grants it (decisions/028), or the stand-in
        # where none is mounted; and the links, which say whose activities to
        # report (contract v14).
        syncer = Syncer(
            plugin,
            raw=RawStore(storage_root()),
            linked=lambda external_account_id: (
                links.scope.link_of(external_account_id) is not None
            ),
        )
        configured, wake = asyncio.Event(), asyncio.Event()
        following = await follow_links(plugin, links)
        log.info("holding %d links to the deployment's accounts", len(links.scope.links))
        page = serve(plugin, syncer, links, wake, port)
        log.info("serving its pages on 127.0.0.1:%d", port)
        tasks = [
            following,
            asyncio.create_task(watch_settings(plugin, syncer, configured, wake)),
            asyncio.create_task(poll(syncer, configured, wake)),
        ]
        await stopped.wait()
        log.info("stopping")
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        page.shutdown()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
