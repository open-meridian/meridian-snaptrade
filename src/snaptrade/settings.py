"""What the plugin needs from the deployment, declared as settings (W4.7).

A deployment administrator gives these in the dashboard's settings form, which
is built from the names and descriptions here. The SnapTrade credentials are
secret settings: set once, never read back, displayed, logged or bundled. They
reach this process in `meridian.Settings` and nowhere else, and nothing in this
package prints them: `Credentials` keeps them out of its repr, and a failed
call to SnapTrade is described by its type and status alone (venue.py),
because the SDK puts the user secret in the request's query string.

None is `required`: a required setting that is missing makes the sidecar
report the plugin unhealthy, and synthetic mode must run before any key
exists. The plugin says itself which credentials it is waiting for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import meridian

CLIENT_ID = "snaptrade_client_id"
CONSUMER_KEY = "snaptrade_consumer_key"
USER_ID = "snaptrade_user_id"
USER_SECRET = "snaptrade_user_secret"
PERSONAL_KEY = "snaptrade_personal_key"
SYNTHETIC = "synthetic"
POLL_SECONDS = "poll_seconds"
STALE_AFTER_HOURS = "stale_after_hours"

DECLARED: tuple[meridian.Setting, ...] = (
    meridian.Setting(
        CLIENT_ID,
        str,
        secret=True,
        description="SnapTrade client ID, from the SnapTrade dashboard's API keys page.",
    ),
    meridian.Setting(
        CONSUMER_KEY,
        str,
        secret=True,
        description="SnapTrade consumer key, shown once when SnapTrade issues the key.",
    ),
    meridian.Setting(
        USER_ID,
        str,
        description=(
            "The SnapTrade user whose brokerage connections this instance reads. "
            "Not needed with a personal key."
        ),
    ),
    meridian.Setting(
        USER_SECRET,
        str,
        secret=True,
        description=(
            "That SnapTrade user's secret, returned when the user was registered. "
            "Not needed with a personal key."
        ),
    ),
    meridian.Setting(
        PERSONAL_KEY,
        bool,
        description=(
            "On when the key is a SnapTrade personal key, which belongs to one user "
            "and needs no user ID or secret. Off by default (a commercial key)."
        ),
    ),
    meridian.Setting(
        SYNTHETIC,
        bool,
        description=(
            "Serve built-in synthetic SnapTrade responses instead of calling SnapTrade, "
            "to develop against before any key exists. Off by default."
        ),
    ),
    meridian.Setting(
        POLL_SECONDS,
        int,
        description="How often to read SnapTrade, in seconds. Default 900; at least 60.",
    ),
    meridian.Setting(
        STALE_AFTER_HOURS,
        int,
        description=(
            "How old SnapTrade's last sync of an account may be before it is reported "
            "stale, in hours. Default 36, since SnapTrade's daily plan refreshes once a day."
        ),
    ),
)

DEFAULT_POLL_SECONDS = 900
LEAST_POLL_SECONDS = 60
DEFAULT_STALE_AFTER_HOURS = 36


@dataclass(frozen=True)
class Credentials:
    """What SnapTrade is called with. Never in a repr, a log line or a page."""

    client_id: str = field(repr=False)
    consumer_key: str = field(repr=False)
    user_id: str = field(repr=False, default="")
    user_secret: str = field(repr=False, default="")
    personal: bool = False


@dataclass(frozen=True)
class Config:
    """The settings as this plugin uses them, with defaults applied."""

    synthetic: bool = False
    credentials: Credentials | None = None
    # The names of the credential settings not yet given, for saying so.
    missing: tuple[str, ...] = ()
    poll_seconds: int = DEFAULT_POLL_SECONDS
    stale_after: timedelta = timedelta(hours=DEFAULT_STALE_AFTER_HOURS)
    user_id: str = ""

    @property
    def ready(self) -> bool:
        """Whether there is something to read: synthetic, or every credential."""
        return self.synthetic or self.credentials is not None


def _text(values: dict[str, str | int | bool], name: str) -> str:
    value = values.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def config_from(values: dict[str, str | int | bool]) -> Config:
    """The plugin's configuration from its delivered settings.

    Credentials are complete or absent: a partial set names what is missing
    rather than calling SnapTrade with a guess, and there is no default key
    anywhere to fall back on.
    """
    personal = values.get(PERSONAL_KEY) is True
    needed = [CLIENT_ID, CONSUMER_KEY] + ([] if personal else [USER_ID, USER_SECRET])
    missing = tuple(name for name in needed if not _text(values, name))
    credentials = (
        None
        if missing
        else Credentials(
            client_id=_text(values, CLIENT_ID),
            consumer_key=_text(values, CONSUMER_KEY),
            user_id=_text(values, USER_ID),
            user_secret=_text(values, USER_SECRET),
            personal=personal,
        )
    )
    poll = values.get(POLL_SECONDS)
    stale = values.get(STALE_AFTER_HOURS)
    return Config(
        synthetic=values.get(SYNTHETIC) is True,
        credentials=credentials,
        missing=missing,
        poll_seconds=max(poll, LEAST_POLL_SECONDS)
        if isinstance(poll, int) and not isinstance(poll, bool)
        else DEFAULT_POLL_SECONDS,
        stale_after=timedelta(hours=stale)
        if isinstance(stale, int) and not isinstance(stale, bool) and stale > 0
        else timedelta(hours=DEFAULT_STALE_AFTER_HOURS),
        user_id=_text(values, USER_ID),
    )
