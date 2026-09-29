"""What the plugin needs from the deployment, declared as settings (W4.7).

A deployment administrator gives these in the dashboard's settings form, which
is built from the declarations here (W6.11): each one's label, its default
greyed in the empty field, its unit, and for the key type a choice whose
answer decides which fields follow (kernel/a-plugins-admin-view, points 4 and
7). The SnapTrade credentials are secret settings: set once, never read back,
displayed, logged or bundled. They reach this process in `meridian.Settings`
and nowhere else, and nothing in this package prints them: `Credentials`
keeps them out of its repr, and a failed call to SnapTrade is described by
its type and status alone (venue.py), because the SDK puts the user secret in
the request's query string.

The client ID and consumer key are required, and the user ID and secret are
required only with a commercial key. While a required one is missing the
sidecar reports the plugin unhealthy, naming it, and the plugin itself says
which it is waiting for. Synthetic mode, a developer's setting, still runs
without them.

The old way of saying the key's kind, `snaptrade_personal_key` (0.1.0), is
still declared, as a developer's setting, so a value saved under it is not
lost. The sidecar delivers only the settings a plugin declares, so leaving it
undeclared would hide the saved value from this plugin; and the dashboard's
form leaves a setting it does not show as it is when it saves. It is read only
while `snaptrade_key_type` is unset (`key_type_of`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import meridian

KEY_TYPE = "snaptrade_key_type"
CLIENT_ID = "snaptrade_client_id"
CONSUMER_KEY = "snaptrade_consumer_key"
USER_ID = "snaptrade_user_id"
USER_SECRET = "snaptrade_user_secret"
POLL_SECONDS = "poll_seconds"
STALE_AFTER_HOURS = "stale_after_hours"
SYNTHETIC = "synthetic"
# 0.1.0's on/off for a personal key, replaced by KEY_TYPE.
PERSONAL_KEY = "snaptrade_personal_key"

PERSONAL = "personal"
COMMERCIAL = "commercial"

DEFAULT_POLL_SECONDS = 300
LEAST_POLL_SECONDS = 60
DEFAULT_STALE_AFTER_HOURS = 24

_WITH_A_COMMERCIAL_KEY = meridian.AppliesWhen(KEY_TYPE, (COMMERCIAL,))

DECLARED: tuple[meridian.Setting, ...] = (
    meridian.Setting(
        KEY_TYPE,
        str,
        required=True,
        label="Key type",
        default=PERSONAL,
        choices=(
            meridian.Choice(PERSONAL, "Personal key", "Belongs to one SnapTrade user: you."),
            meridian.Choice(
                COMMERCIAL,
                "Commercial key",
                "Registers SnapTrade users of its own, each with a secret.",
            ),
        ),
        description="Which kind of key SnapTrade issued. It decides the fields that follow.",
    ),
    meridian.Setting(
        CLIENT_ID,
        str,
        required=True,
        secret=True,
        label="Client ID",
        description="From the SnapTrade dashboard's API keys page.",
    ),
    meridian.Setting(
        CONSUMER_KEY,
        str,
        required=True,
        secret=True,
        label="Consumer key",
        description="Shown once, when SnapTrade issues the key.",
    ),
    meridian.Setting(
        USER_ID,
        str,
        required=True,
        label="User ID",
        applies_when=_WITH_A_COMMERCIAL_KEY,
        description="The SnapTrade user whose brokerage connections this instance reads.",
    ),
    meridian.Setting(
        USER_SECRET,
        str,
        required=True,
        secret=True,
        label="User secret",
        applies_when=_WITH_A_COMMERCIAL_KEY,
        description="That user's secret, returned when the user was registered.",
    ),
    meridian.Setting(
        POLL_SECONDS,
        int,
        label="Read every",
        default=DEFAULT_POLL_SECONDS,
        unit="seconds",
        description=f"How often to read SnapTrade. At least {LEAST_POLL_SECONDS}.",
    ),
    meridian.Setting(
        STALE_AFTER_HOURS,
        int,
        label="Stale after",
        default=DEFAULT_STALE_AFTER_HOURS,
        unit="hours",
        description=(
            "How old SnapTrade's last sync of an account may be before it is reported stale."
        ),
    ),
    meridian.Setting(
        SYNTHETIC,
        bool,
        label="Synthetic mode",
        default=False,
        developer=True,
        description=(
            "Serve built-in synthetic SnapTrade responses instead of calling SnapTrade, "
            "to develop against before any key exists."
        ),
    ),
    meridian.Setting(
        PERSONAL_KEY,
        bool,
        label="Personal key (the old way)",
        developer=True,
        description=(
            "Replaced by Key type, and read only while Key type is unset: on means a "
            "personal key, off a commercial one. Kept so a value saved before 0.2.0 "
            "still counts."
        ),
    ),
)

Values = Mapping[str, str | int | bool]


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
    key_type: str = PERSONAL
    credentials: Credentials | None = None
    # The names of the required settings that apply and are not yet given,
    # for saying so.
    missing: tuple[str, ...] = ()
    poll_seconds: int = DEFAULT_POLL_SECONDS
    stale_after: timedelta = timedelta(hours=DEFAULT_STALE_AFTER_HOURS)
    user_id: str = ""

    @property
    def ready(self) -> bool:
        """Whether there is something to read: synthetic, or every credential."""
        return self.synthetic or self.credentials is not None


def _text(values: Values, name: str) -> str:
    value = values.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _number(values: Values, name: str) -> int | None:
    value = values.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def key_type_of(values: Values, unset: Collection[str] = ()) -> str:
    """The kind of key, as said, or as 0.1.0's setting said it.

    `unset` names the settings the deployment holds nothing for, which the
    sidecar's delivery lists among the required ones missing: the SDK fills a
    declared default into the values, so an unset key type reads as the
    default there. While it is unset, a saved `snaptrade_personal_key` decides,
    and with neither saved it is the declared default.
    """
    said = values.get(KEY_TYPE)
    if KEY_TYPE not in unset and said in (PERSONAL, COMMERCIAL):
        return str(said)
    old = values.get(PERSONAL_KEY)
    if isinstance(old, bool):
        return PERSONAL if old else COMMERCIAL
    return PERSONAL


def _applies(setting: meridian.Setting, values: Values) -> bool:
    """Whether a setting applies as the values stand, as the sidecar and the
    dashboard's form decide it (W4.8)."""
    condition = setting.applies_when
    return condition is None or values.get(condition.setting) in condition.one_of


def missing_from(values: Values) -> tuple[str, ...]:
    """The required settings that apply and hold nothing, in the order declared."""
    return tuple(
        setting.name
        for setting in DECLARED
        if setting.required
        and setting.name != KEY_TYPE
        and _applies(setting, values)
        and not _text(values, setting.name)
    )


def config_from(values: Values, unset: Collection[str] = ()) -> Config:
    """The plugin's configuration from its delivered settings, and the names of
    those the deployment holds nothing for (`Settings.missing_required`).

    Credentials are complete or absent: a partial set names what is missing
    rather than calling SnapTrade with a guess, and there is no default key
    anywhere to fall back on.
    """
    key_type = key_type_of(values, unset)
    missing = missing_from({**values, KEY_TYPE: key_type})
    personal = key_type == PERSONAL
    credentials = (
        None
        if missing
        else Credentials(
            client_id=_text(values, CLIENT_ID),
            consumer_key=_text(values, CONSUMER_KEY),
            user_id="" if personal else _text(values, USER_ID),
            user_secret="" if personal else _text(values, USER_SECRET),
            personal=personal,
        )
    )
    poll = _number(values, POLL_SECONDS)
    stale = _number(values, STALE_AFTER_HOURS)
    return Config(
        synthetic=values.get(SYNTHETIC) is True,
        key_type=key_type,
        credentials=credentials,
        missing=missing,
        poll_seconds=DEFAULT_POLL_SECONDS if poll is None else max(poll, LEAST_POLL_SECONDS),
        stale_after=timedelta(
            hours=stale if stale is not None and stale > 0 else DEFAULT_STALE_AFTER_HOURS
        ),
        user_id="" if personal else _text(values, USER_ID),
    )


def label(name: str) -> str:
    """What the settings form calls a setting."""
    return next((s.label for s in DECLARED if s.name == name and s.label), name)
