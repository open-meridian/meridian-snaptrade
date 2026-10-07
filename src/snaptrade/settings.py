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

**The windows** (contract v16; meridian-design spec/an-edge-plugins-older-
records-move-to-the-archive). How long each kind of raw record stays in the
plugin's storage, and what is done with it past that, are two settings per
kind the SDK declares for every edge plugin alike, from the kinds
declaration.py declares: `activity_window_days` and `activity_past_window`
for a reported activity's record, `responses_window_days` and
`responses_past_window` for each read's raw responses. Their names are the
SDK's (`<kind>_window_days`, `<kind>_past_window`), reserved: this module
declares neither, and reads them as delivered (`windows_from`).

0.12.0's two settings, `raw_retention_days` and `activity_retention_days`,
became those windows in 0.13.0 and are no longer declared, so a value saved
under one is not delivered: at upgrade the deployment admin sets the windows
once in Settings, `responses_window_days` for the first and
`activity_window_days` for the second, as the CHANGELOG and README say
(option A, ruled 2026-10-07; carrying a value across at launch is queued as
meridian-design's design/a-setting-replaces-an-old-one).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import meridian

from . import counted_as_cash
from .counted_as_cash import CountedAsCash, rows_of
from .plan_codes import ACCOUNT, CODE, INSTRUMENT, PlanCodeLink, links_of

KEY_TYPE = "snaptrade_key_type"
CLIENT_ID = "snaptrade_client_id"
CONSUMER_KEY = "snaptrade_consumer_key"
USER_ID = "snaptrade_user_id"
USER_SECRET = "snaptrade_user_secret"
POLL_SECONDS = "poll_seconds"
STALE_AFTER_HOURS = "stale_after_hours"
PLAN_CODE_LINKS = "plan_code_links"
COUNTED_AS_CASH = "counted_as_cash"
SYNTHETIC = "synthetic"
# 0.1.0's on/off for a personal key, replaced by KEY_TYPE.
PERSONAL_KEY = "snaptrade_personal_key"

PERSONAL = "personal"
COMMERCIAL = "commercial"

DEFAULT_POLL_SECONDS = 300
LEAST_POLL_SECONDS = 60
DEFAULT_STALE_AFTER_HOURS = 24
# The kinds of raw record (declaration.py) by the names their window settings
# take, and the SDK's names for those settings (W6.11, contract v16).
RESPONSES = "responses"
ACTIVITY = "activity"
RESPONSES_WINDOW = f"{RESPONSES}_window_days"
RESPONSES_PAST = f"{RESPONSES}_past_window"
ACTIVITY_WINDOW = f"{ACTIVITY}_window_days"
ACTIVITY_PAST = f"{ACTIVITY}_past_window"
# What is done with a record past its window: the SDK's three choices.
ARCHIVED, KEPT, DELETED = "archived", "kept", "deleted"
PAST_WINDOW = (ARCHIVED, KEPT, DELETED)
# How long each read's raw responses stay in storage by default, and the least
# (the SDK's bound): the responses kind's declared window.
DEFAULT_RAW_RETENTION_DAYS = 30
LEAST_RAW_RETENTION_DAYS = 1
# How long a reported activity's raw record stays in storage from when it was
# received, by default (the product owner, 2026-10-05): seven years, past the
# two SnapTrade holds of an account's history at Fidelity; an admin may set it
# longer, up to the most the SDK's window takes, and a record is never deleted
# within the history SnapTrade reported, which raw.py holds it to.
DEFAULT_ACTIVITY_RETENTION_DAYS = 2555
MOST_ACTIVITY_RETENTION_DAYS = 36500

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
        PLAN_CODE_LINKS,
        list,
        label="Plan-code links",
        columns=(
            meridian.Column(ACCOUNT, "external_account", label="Account", required=True),
            meridian.Column(
                CODE,
                label="Plan code",
                required=True,
                description="As SnapTrade names it in the account's activity.",
            ),
            meridian.Column(INSTRUMENT, "instrument", label="Instrument", required=True),
        ),
        most_rows=200,
        description=(
            "A plan's own fund codes, each linked on one account to the instrument it is "
            "(Fidelity's OQKR to VIGIX): its activity is then that instrument, naming who "
            "linked it."
        ),
    ),
    meridian.Setting(
        COUNTED_AS_CASH,
        list,
        label="Cash links",
        # Account, then what SnapTrade names, then what it is: the order of
        # Plan-code links' columns (the product owner, 2026-10-05). Rows are
        # keyed by column name, so a row saved in another order reads the same.
        columns=(
            meridian.Column(
                counted_as_cash.ACCOUNT,
                "external_account",
                label="Account",
                description=(
                    "Optional: blank applies the row to every account holding the symbol; "
                    "a row naming the account comes first."
                ),
            ),
            meridian.Column(
                counted_as_cash.SYMBOL,
                label="Symbol",
                required=True,
                description="As SnapTrade names the position, such as FDIC99532.",
            ),
            meridian.Column(
                counted_as_cash.CURRENCY,
                label="Currency",
                required=True,
                description="The ISO 4217 code of the cash it is, such as USD.",
            ),
        ),
        most_rows=200,
        description=(
            "Positions a custodian holds as cash that SnapTrade does not mark as a cash "
            "equivalent, such as a bank deposit as an IRA's core position: each is sent as "
            "cash in its currency, naming who listed it. A fund stays a fund."
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

Values = Mapping[str, str | int | bool | list[dict[str, str]]]


@dataclass(frozen=True)
class Credentials:
    """What SnapTrade is called with. Never in a repr, a log line or a page."""

    client_id: str = field(repr=False)
    consumer_key: str = field(repr=False)
    user_id: str = field(repr=False, default="")
    user_secret: str = field(repr=False, default="")
    personal: bool = False

    def secrets(self) -> tuple[str, ...]:
        """The values never to be kept anywhere, for raw.py to look for and
        redact; the user ID is not one."""
        return tuple(filter(None, (self.client_id, self.consumer_key, self.user_secret)))


@dataclass(frozen=True)
class Window:
    """One kind's window, and what is done with a record past it, as the
    settings say."""

    days: int
    past: str

    @property
    def length(self) -> timedelta:
        return timedelta(days=self.days)


@dataclass(frozen=True)
class Windows:
    """The two kinds' windows (contract v16)."""

    responses: Window = Window(DEFAULT_RAW_RETENTION_DAYS, KEPT)
    activity: Window = Window(DEFAULT_ACTIVITY_RETENTION_DAYS, KEPT)

    def of(self, kind: str) -> Window:
        return self.responses if kind == RESPONSES else self.activity


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
    # Each kind's window and what is done past it (`windows_from`).
    windows: Windows = field(default_factory=lambda: Windows())
    # The plan-code links people made, as the settings deliver them
    # (plan_codes.py).
    plan_codes: tuple[PlanCodeLink, ...] = ()
    # The positions people listed as cash, as the settings deliver them
    # (counted_as_cash.py).
    counted_as_cash: tuple[CountedAsCash, ...] = ()

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
        windows=windows_from(values),
        plan_codes=links_of(values.get(PLAN_CODE_LINKS)),
        counted_as_cash=rows_of(values.get(COUNTED_AS_CASH)),
    )


def _window(values: Values, window: str, past: str, default: int) -> Window:
    """A kind's window as delivered, within the SDK's bound."""
    days = _number(values, window)
    if days is None:
        days = default
    days = min(max(days, LEAST_RAW_RETENTION_DAYS), MOST_ACTIVITY_RETENTION_DAYS)
    chosen = values.get(past)
    return Window(days, chosen if chosen in PAST_WINDOW else KEPT)


def windows_from(values: Values) -> Windows:
    """Each kind's window and what is done past it, from the settings the SDK
    declares for it (W6.11). Past the window is `kept` where the settings say
    nothing: the SDK's default is filled in by it, `archived` only where the
    instance has an archive."""
    return Windows(
        responses=_window(values, RESPONSES_WINDOW, RESPONSES_PAST, DEFAULT_RAW_RETENTION_DAYS),
        activity=_window(
            values, ACTIVITY_WINDOW, ACTIVITY_PAST, DEFAULT_ACTIVITY_RETENTION_DAYS
        ),
    )


def label(name: str) -> str:
    """What the settings form calls a setting."""
    return next((s.label for s in DECLARED if s.name == name and s.label), name)
