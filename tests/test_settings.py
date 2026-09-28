"""The settings the plugin declares, and the configuration it makes of them."""

from __future__ import annotations

from datetime import timedelta

from snaptrade.settings import (
    CLIENT_ID,
    CONSUMER_KEY,
    DECLARED,
    PERSONAL_KEY,
    POLL_SECONDS,
    STALE_AFTER_HOURS,
    SYNTHETIC,
    USER_ID,
    USER_SECRET,
    config_from,
)

GIVEN: dict[str, str | int | bool] = {
    CLIENT_ID: "CLIENT",
    CONSUMER_KEY: "KEY",
    USER_ID: "u-1",
    USER_SECRET: "SECRET",
}


def test_the_key_the_client_id_and_the_user_secret_are_secret_settings() -> None:
    secret = {setting.name for setting in DECLARED if setting.secret}
    assert secret == {CLIENT_ID, CONSUMER_KEY, USER_SECRET}
    # None is required, so synthetic mode runs before any key exists.
    assert not any(setting.required for setting in DECLARED)
    assert all(setting.description for setting in DECLARED)
    for setting in DECLARED:
        setting._declared()  # each is a type the sidecar takes


def test_with_nothing_given_it_waits_naming_what_it_needs() -> None:
    config = config_from({})
    assert not config.ready and config.credentials is None
    assert config.missing == (CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET)


def test_a_partial_set_of_credentials_is_not_used() -> None:
    config = config_from({CLIENT_ID: "CLIENT", CONSUMER_KEY: "KEY", USER_ID: "u-1"})
    assert config.credentials is None and config.missing == (USER_SECRET,)


def test_every_credential_given_makes_it_ready() -> None:
    config = config_from(GIVEN)
    assert config.ready and config.missing == ()
    assert config.credentials is not None and not config.credentials.personal
    assert config.user_id == "u-1"


def test_a_personal_key_needs_no_user() -> None:
    config = config_from({CLIENT_ID: "CLIENT", CONSUMER_KEY: "KEY", PERSONAL_KEY: True})
    assert config.ready and config.credentials is not None and config.credentials.personal


def test_synthetic_mode_is_off_by_default_and_needs_no_key() -> None:
    assert not config_from({}).synthetic
    config = config_from({SYNTHETIC: True})
    assert config.synthetic and config.ready and config.credentials is None


def test_polling_has_a_default_and_a_floor_and_staleness_a_default() -> None:
    assert config_from({}).poll_seconds == 900
    assert config_from({POLL_SECONDS: 5}).poll_seconds == 60
    assert config_from({STALE_AFTER_HOURS: 12}).stale_after == timedelta(hours=12)
    assert config_from({STALE_AFTER_HOURS: 0}).stale_after == timedelta(hours=36)


def test_no_secret_appears_in_the_configurations_repr() -> None:
    printed = repr(config_from(GIVEN))
    for secret in ("CLIENT", "KEY", "SECRET"):
        assert secret not in printed
