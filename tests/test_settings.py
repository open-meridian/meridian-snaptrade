"""The settings the plugin declares, and the configuration it makes of them."""

from __future__ import annotations

from datetime import timedelta

import meridian
import pytest
from meridian.v1 import sidecar_pb2

from snaptrade.settings import (
    CLIENT_ID,
    COMMERCIAL,
    CONSUMER_KEY,
    DECLARED,
    KEY_TYPE,
    PERSONAL,
    PERSONAL_KEY,
    POLL_SECONDS,
    STALE_AFTER_HOURS,
    SYNTHETIC,
    USER_ID,
    USER_SECRET,
    config_from,
    key_type_of,
    label,
)

BY_NAME = {setting.name: setting for setting in DECLARED}
KEYS: dict[str, str | int | bool] = {CLIENT_ID: "CLIENT", CONSUMER_KEY: "KEY"}
COMMERCIAL_GIVEN: dict[str, str | int | bool] = {
    KEY_TYPE: COMMERCIAL,
    **KEYS,
    USER_ID: "u-1",
    USER_SECRET: "SECRET",
}


def delivered(held: dict[str, str]) -> meridian.Settings:
    """The settings as the SDK hands them over: typed, the declared defaults
    filled in, and the required ones held nothing for named."""
    defaults = {s.name: s.default for s in DECLARED if s.default is not None}
    values: dict[str, str | int | bool | list[dict[str, str]]] = {
        **defaults,
        **{name: BY_NAME[name]._parsed(text) for name, text in held.items()},
    }
    missing = tuple(s.name for s in DECLARED if s.required and s.name not in held)
    return meridian.Settings(values=values, missing_required=missing)


# ── The declarations ─────────────────────────────────────────────────────────


def test_every_declaration_is_one_the_sidecar_takes() -> None:
    for setting in DECLARED:
        setting._declared()
        assert setting.label and setting.description


def test_the_key_type_is_a_required_choice_asked_first_defaulting_to_personal() -> None:
    first = DECLARED[0]
    assert first.name == KEY_TYPE and first.required and first.default == PERSONAL
    assert [(c.value, c.label) for c in first.choices] == [
        (PERSONAL, "Personal key"),
        (COMMERCIAL, "Commercial key"),
    ]
    assert all(c.description for c in first.choices)
    assert first._declared().type == sidecar_pb2.SETTING_TYPE_CHOICE


def test_the_client_id_and_consumer_key_are_required_secrets() -> None:
    for name, said in ((CLIENT_ID, "Client ID"), (CONSUMER_KEY, "Consumer key")):
        setting = BY_NAME[name]
        assert setting.secret and setting.required and setting.applies_when is None
        assert setting.label == said
    assert {s.name for s in DECLARED if s.secret} == {CLIENT_ID, CONSUMER_KEY, USER_SECRET}


def test_the_user_is_required_only_with_a_commercial_key() -> None:
    for name in (USER_ID, USER_SECRET):
        setting = BY_NAME[name]
        assert setting.required
        assert setting.applies_when == meridian.AppliesWhen(KEY_TYPE, (COMMERCIAL,))
        declared = setting._declared()
        assert (declared.applies_when.setting, list(declared.applies_when.one_of)) == (
            KEY_TYPE,
            [COMMERCIAL],
        )


def test_reading_and_staleness_are_optional_with_a_default_and_a_unit() -> None:
    assert (label(POLL_SECONDS), BY_NAME[POLL_SECONDS].unit) == ("Read every", "seconds")
    assert (label(STALE_AFTER_HOURS), BY_NAME[STALE_AFTER_HOURS].unit) == (
        "Stale after",
        "hours",
    )
    assert BY_NAME[POLL_SECONDS].default == 300 and BY_NAME[STALE_AFTER_HOURS].default == 24
    assert not BY_NAME[POLL_SECONDS].required and not BY_NAME[STALE_AFTER_HOURS].required
    assert BY_NAME[POLL_SECONDS]._declared().default_value == "300"


def test_synthetic_mode_and_the_old_personal_key_are_developer_settings() -> None:
    assert [s.name for s in DECLARED if s.developer] == [SYNTHETIC, PERSONAL_KEY]
    assert BY_NAME[SYNTHETIC].default is False
    # No default: unset has to stay unset, or the new key type would never decide.
    assert BY_NAME[PERSONAL_KEY].default is None and not BY_NAME[PERSONAL_KEY].required


# ── The configuration ────────────────────────────────────────────────────────


def test_with_nothing_given_it_waits_naming_what_a_personal_key_needs() -> None:
    config = config_from({})
    assert config.key_type == PERSONAL
    assert not config.ready and config.credentials is None
    assert config.missing == (CLIENT_ID, CONSUMER_KEY)


def test_a_commercial_key_also_needs_the_user() -> None:
    config = config_from({KEY_TYPE: COMMERCIAL})
    assert config.missing == (CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET)


def test_a_partial_set_of_credentials_is_not_used() -> None:
    config = config_from({KEY_TYPE: COMMERCIAL, **KEYS, USER_ID: "u-1"})
    assert config.credentials is None and config.missing == (USER_SECRET,)


def test_every_credential_given_makes_it_ready() -> None:
    config = config_from(COMMERCIAL_GIVEN)
    assert config.ready and config.missing == ()
    assert config.credentials is not None and not config.credentials.personal
    assert config.user_id == "u-1"


def test_a_personal_key_needs_no_user_and_ignores_one_left_behind() -> None:
    config = config_from({KEY_TYPE: PERSONAL, **KEYS, USER_ID: "u-1"})
    assert config.ready and config.credentials is not None and config.credentials.personal
    assert config.user_id == "" and config.credentials.user_id == ""


def test_the_product_owners_saved_values_still_mean_a_personal_key() -> None:
    # Saved under 0.1.0: the key and the old on/off, and no key type.
    settings = delivered({CLIENT_ID: "CLIENT", CONSUMER_KEY: "KEY", PERSONAL_KEY: "true"})
    assert settings.values[KEY_TYPE] == PERSONAL  # the SDK's filled-in default
    config = config_from(settings.values, settings.missing_required)
    assert config.key_type == PERSONAL and config.missing == ()
    assert config.credentials is not None and config.credentials.personal


def test_while_the_key_type_is_unset_the_old_setting_off_means_commercial() -> None:
    settings = delivered({CLIENT_ID: "CLIENT", CONSUMER_KEY: "KEY", PERSONAL_KEY: "false"})
    config = config_from(settings.values, settings.missing_required)
    assert config.key_type == COMMERCIAL and config.missing == (USER_ID, USER_SECRET)


@pytest.mark.parametrize("old", ["true", "false"])
def test_once_the_key_type_is_saved_the_old_setting_is_not_read(old: str) -> None:
    for said in (PERSONAL, COMMERCIAL):
        settings = delivered({KEY_TYPE: said, PERSONAL_KEY: old})
        assert key_type_of(settings.values, settings.missing_required) == said


def test_with_neither_saved_the_key_type_is_the_default() -> None:
    settings = delivered({})
    assert key_type_of(settings.values, settings.missing_required) == PERSONAL


def test_synthetic_mode_is_off_by_default_and_needs_no_key() -> None:
    assert not config_from(delivered({}).values).synthetic
    config = config_from({SYNTHETIC: True})
    assert config.synthetic and config.ready and config.credentials is None


def test_reading_has_a_default_and_a_floor_and_staleness_a_default() -> None:
    assert config_from({}).poll_seconds == 300
    assert config_from(delivered({}).values).poll_seconds == 300
    assert config_from({POLL_SECONDS: 5}).poll_seconds == 60
    assert config_from({POLL_SECONDS: 600}).poll_seconds == 600
    assert config_from({}).stale_after == timedelta(hours=24)
    assert config_from({STALE_AFTER_HOURS: 12}).stale_after == timedelta(hours=12)
    assert config_from({STALE_AFTER_HOURS: 0}).stale_after == timedelta(hours=24)


def test_no_secret_appears_in_the_configurations_repr() -> None:
    printed = repr(config_from(COMMERCIAL_GIVEN))
    for secret in ("CLIENT", "KEY", "SECRET"):
        assert secret not in printed
