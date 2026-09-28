"""The admin portal as synthetic mode would show it, printed as one HTML file
with its stylesheet inline: for looking at the page, and at the kit's
migration, without a deployment.

    python -m snaptrade.preview > preview.html

Nothing is recorded: there is no sidecar here, so the page shows the read and
no statement outcomes.
"""

from __future__ import annotations

import asyncio

from .contract import Contract
from .normalise import views
from .page import STYLESHEET, render_admin, stylesheet
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
    page = render_admin(asyncio.run(_status()), token="preview")
    link = f'<link rel="stylesheet" href="{STYLESHEET}">'
    print(page.replace(link, f"<style>{stylesheet()}</style>"))


if __name__ == "__main__":
    main()
