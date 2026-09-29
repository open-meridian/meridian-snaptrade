"""The admin portal as synthetic mode would show it, printed as one HTML file:
for looking at the page without a deployment.

    python -m snaptrade.preview > preview.html

It links the kit where the dashboard serves it, /.meridian/ui/<version>/, so
serve it beside the kit to see it styled (meridian-ui's `make serve` serves
the kit under that path); opened on its own it is the page without the kit,
unstyled, which must work too.

Nothing is recorded: there is no sidecar here, so the page shows the read and
no statement outcomes.
"""

from __future__ import annotations

import asyncio

from .contract import Contract
from .normalise import views
from .page import render_admin
from .settings import Config
from .sync import Status
from .synthetic import USER_ID, SyntheticVenue
from .venue import read, utc_now


async def _status() -> Status:
    venue = SyntheticVenue(utc_now)
    snapshot = await read(venue, utc_now)
    return Status(
        mode="synthetic",
        read_at=snapshot.read_at,
        connections=views(snapshot, Config().stale_after),
        users=tuple(await venue.users()),
        user_id=USER_ID,
        waiting=Contract().waiting(),
    )


def main() -> None:
    print(render_admin(asyncio.run(_status()), token="preview"))


if __name__ == "__main__":
    main()
