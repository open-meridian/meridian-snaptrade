"""The SnapTrade plugin: brokerage accounts, cash and positions into an Open
Meridian deployment, through SnapTrade (plans/proving-the-plugin-system, step 1).

It connects to the sidecar it is launched beside, declares its settings and
its admin pages, and then, on every poll and every settings change, reads
SnapTrade (or, in synthetic mode, its built-in responses), normalises what it
read to the platform's convention (normalise.py), and records it through the
SDK's typed operations (contract.py): each account's sync status, and a
holdings statement per account. It holds nothing between reads; a restart
reads again.

Everything goes through the sidecar. The SnapTrade credentials arrive as the
plugin's own secret settings, set by a deployment administrator in the
dashboard, and are never logged, shown or sent anywhere but SnapTrade.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import meridian

from .linking import Links
from .page import ADMIN_PAGES, TITLE, serve
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
        # Shown as tabs in the dashboard's admin view of the instance.
        interface=meridian.Interface(port=port, title=TITLE, admin_pages=ADMIN_PAGES),
        settings=DECLARED,
        # It names accounts by SnapTrade's identifiers, which a deployment
        # admin links to accounts (W6.4).
        reads_external_accounts=True,
    ) as plugin:
        log.info(
            "registered as %s, roles %s",
            plugin.identity.instance_id,
            ", ".join(plugin.identity.roles) or "none",
        )
        syncer = Syncer(plugin)
        configured, wake = asyncio.Event(), asyncio.Event()
        page = serve(syncer, Links(plugin), loop, port, wake)
        log.info("serving its admin pages on 127.0.0.1:%d", port)
        tasks = [
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
