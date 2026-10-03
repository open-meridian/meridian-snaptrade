"""SnapTrade's SDK behind the Venue interface: exact numbers, the user's
credentials sent only to SnapTrade, and failures that never carry them."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from snaptrade.settings import Credentials
from snaptrade.venue import SnapTradeVenue, VenueError, exact_body, read

from conftest import NOW, clock

SECRET = "user-secret-0f3c-not-real"
KEY = "consumer-key-9a1b-not-real"
COMMERCIAL = Credentials(
    client_id="CLIENT", consumer_key=KEY, user_id="u-1", user_secret=SECRET
)
PERSONAL = Credentials(client_id="CLIENT", consumer_key=KEY, personal=True)


@dataclass
class Response:
    """What the SDK returns: the decoded body, and the raw urllib3 response."""

    body: Any
    response: Any


def answered(raw: bytes, body: Any = None) -> Response:
    return Response(body=body, response=SimpleNamespace(data=raw))


class ApiException(Exception):
    """Shaped like SnapTrade's: its text holds the URL, and the URL the secret."""

    def __init__(self) -> None:
        super().__init__(f"(401) GET /accounts?userId=u-1&userSecret={SECRET}")
        self.status = 401


class FakeSdk:
    def __init__(self, fail: bool = False) -> None:
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.account_information = SimpleNamespace(
            list_user_accounts=self._answer("list_user_accounts", b'[{"id": "a1"}]'),
            get_all_account_positions=self._answer(
                "positions", b'{"results": [{"units": "12.50"}], "data_freshness": {}}'
            ),
            get_user_account_balance=self._answer(
                "balances", b'[{"currency": {"code": "USD"}, "cash": 0.1}]'
            ),
        )
        self.connections = SimpleNamespace(
            list_brokerage_authorizations=self._answer("connections", b'[{"id": "c1"}]'),
            refresh_brokerage_authorization=self._answer("refresh", b'{"detail": "queued"}'),
        )
        self.authentication = SimpleNamespace(
            list_snap_trade_users=self._answer("users", b'["u-1", "u-2"]'),
            login_snap_trade_user=self._answer(
                "login", b'{"redirectURI": "https://portal.example/abc", "sessionId": "s"}'
            ),
        )

    def _answer(self, name: str, raw: bytes) -> Any:
        def call(**kwargs: Any) -> Response:
            self.asked.append((name, kwargs))
            if self.fail:
                raise ApiException()
            return answered(raw)

        return call


async def test_numbers_are_read_from_the_bytes_received_never_as_floats() -> None:
    venue = SnapTradeVenue(COMMERCIAL, sdk=FakeSdk())
    (usd,) = await venue.balances("a1")
    assert usd["cash"] == Decimal("0.1") and isinstance(usd["cash"], Decimal)
    positions = await venue.positions("a1")
    assert positions["results"][0]["units"] == "12.50"


def test_without_raw_bytes_the_sdks_body_is_used() -> None:
    assert exact_body(Response(body=[1], response=None)) == [1]


async def test_a_commercial_key_sends_the_user_and_a_personal_key_does_not() -> None:
    sdk = FakeSdk()
    await SnapTradeVenue(COMMERCIAL, sdk=sdk).accounts()
    await SnapTradeVenue(PERSONAL, sdk=sdk).accounts()
    assert sdk.asked == [
        ("list_user_accounts", {"user_id": "u-1", "user_secret": SECRET}),
        ("list_user_accounts", {}),
    ]


async def test_the_connection_portal_is_asked_for_read_only_and_can_reconnect() -> None:
    sdk = FakeSdk()
    venue = SnapTradeVenue(COMMERCIAL, sdk=sdk)
    assert await venue.connection_portal() == "https://portal.example/abc"
    assert await venue.connection_portal(reconnect="c1") == "https://portal.example/abc"
    first, second = (kwargs for name, kwargs in sdk.asked)
    assert first["connection_type"] == "read" and "reconnect" not in first
    assert second["reconnect"] == "c1"
    assert await venue.refresh("c1") == "queued"
    assert await venue.users() == ["u-1", "u-2"]


async def test_a_failure_says_what_was_asked_and_never_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    venue = SnapTradeVenue(COMMERCIAL, sdk=FakeSdk(fail=True))
    with caplog.at_level(logging.DEBUG), pytest.raises(VenueError) as failed:
        await venue.accounts()
    assert str(failed.value) == "listing accounts failed: ApiException (HTTP 401)"
    assert failed.value.status == 401
    # Not chained: the SDK's exception, which holds the URL, goes nowhere.
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    printed = "".join(traceback.format_exception(failed.value))
    for secret in (SECRET, KEY):
        assert secret not in printed
        assert secret not in caplog.text


def test_credentials_never_appear_in_a_repr() -> None:
    for secret in (SECRET, KEY, "CLIENT"):
        assert secret not in repr(COMMERCIAL)
        assert secret not in repr(SnapTradeVenue(COMMERCIAL, sdk=FakeSdk()))


class OneAccountFails:
    async def users(self) -> list[str]:
        return []

    async def connections(self) -> list[dict[str, Any]]:
        return [{"id": "c1"}]

    async def accounts(self) -> list[dict[str, Any]]:
        return [{"id": "a1"}, {"id": "a2"}]

    async def positions(self, account_id: str) -> dict[str, Any]:
        if account_id == "a1":
            raise VenueError("reading positions")
        return {"results": []}

    async def balances(self, account_id: str) -> list[dict[str, Any]]:
        return []

    async def activities(self, account_id: str, start: date, end: date) -> list[dict[str, Any]]:
        return []

    async def connection_portal(self, reconnect: str | None = None) -> str:
        return ""

    async def refresh(self, connection_id: str) -> str:
        return ""


async def test_one_account_that_cannot_be_read_does_not_stop_the_others() -> None:
    snapshot = await read(OneAccountFails(), clock())
    assert snapshot.read_at == NOW
    assert snapshot.failures == {"a1": "reading positions failed: no answer"}
    assert snapshot.positions == {"a2": {"results": []}}
