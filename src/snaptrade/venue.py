"""SnapTrade, behind the small interface the rest of the plugin uses.

`Venue` is what the plugin asks of SnapTrade: the SnapTrade users under the
key, the user's brokerage connections (SnapTrade calls them brokerage
authorizations), their accounts, each account's positions and balances, a
Connection Portal link, and a refresh. Answers are SnapTrade's JSON as its API
documents it, with every JSON number read as a `Decimal` from its text and
never as a float: a quantity or amount is exact from the first moment this
process holds it (decisions/023).

Two implementations: `SnapTradeVenue` here, the only module that imports
SnapTrade's SDK, and `synthetic.SyntheticVenue`, which serves built-in
responses of the same shape. Tests substitute either.

Credentials never leave this module except as SnapTrade's SDK sends them. A
failed call is described by what was asked, the exception's type and the HTTP
status, and nothing else: the SDK sends the user secret as a query parameter,
so its exception text and its traceback can carry it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from .settings import Credentials

Json = dict[str, Any]


class VenueError(Exception):
    """A call to SnapTrade failed. Its text is safe to log and show."""

    def __init__(self, asked: str, failed: BaseException | None = None) -> None:
        self.asked = asked
        self.status = _status(failed) if failed is not None else None
        kind = type(failed).__name__ if failed is not None else "no answer"
        said = f"{asked} failed: {kind}"
        if self.status is not None:
            said += f" (HTTP {self.status})"
        super().__init__(said)


def _status(failed: BaseException) -> int | None:
    for holder in (failed, getattr(failed, "api_response", None)):
        status = getattr(holder, "status", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


class Venue(Protocol):
    """What the plugin asks of SnapTrade."""

    async def users(self) -> list[str]:
        """The SnapTrade user IDs registered under the key."""
        ...

    async def connections(self) -> list[Json]:
        """The configured user's brokerage authorizations."""
        ...

    async def accounts(self) -> list[Json]:
        """Every account across those connections."""
        ...

    async def positions(self, account_id: str) -> Json:
        """`GET /accounts/{accountId}/positions/all`: `results` and `data_freshness`."""
        ...

    async def balances(self, account_id: str) -> list[Json]:
        """`GET /accounts/{accountId}/balances`: one balance per currency."""
        ...

    async def connection_portal(self, reconnect: str | None = None) -> str:
        """A Connection Portal link: to connect a brokerage, or to reconnect one."""
        ...

    async def refresh(self, connection_id: str) -> str:
        """Ask SnapTrade to read a connection's brokerage again; its confirmation.

        Only a connection SnapTrade serves on a delay benefits, and each call
        may be charged. On a Real-time plan SnapTrade refuses one for a
        real-time connection: a VenueError whose `status` is 403."""
        ...


@dataclass(frozen=True)
class Snapshot:
    """One read of SnapTrade: everything the plugin knows until the next."""

    read_at: datetime
    connections: list[Json]
    accounts: list[Json]
    positions: dict[str, Json] = field(default_factory=dict)
    balances: dict[str, list[Json]] = field(default_factory=dict)
    # By SnapTrade account ID: why that account's positions or balances could
    # not be read this time. Its statement is not recorded.
    failures: dict[str, str] = field(default_factory=dict)


async def read(venue: Venue, now: Callable[[], datetime]) -> Snapshot:
    """Read every connection and account, then each account's positions and
    balances. A connection or account list that cannot be read fails the whole
    read; one account that cannot be read is noted and the rest go on."""
    read_at = now()
    connections = await venue.connections()
    accounts = await venue.accounts()
    positions: dict[str, Json] = {}
    balances: dict[str, list[Json]] = {}
    failures: dict[str, str] = {}
    for account in accounts:
        account_id = str(account.get("id", ""))
        try:
            positions[account_id] = await venue.positions(account_id)
            balances[account_id] = await venue.balances(account_id)
        except VenueError as failed:
            failures[account_id] = str(failed)
    return Snapshot(read_at, connections, accounts, positions, balances, failures)


def parse_exact(text: str | bytes) -> Any:
    """SnapTrade's JSON with every number that has a fraction or an exponent
    read as a Decimal from its text. Integers stay ints, which are exact."""
    return json.loads(text, parse_float=Decimal)


def exact_body(response: Any) -> Any:
    """The body of an SDK response, re-read exactly from the bytes received.

    The SDK decodes JSON numbers to floats. Its raw urllib3 response keeps the
    bytes, and those are what is parsed here. Should they be unavailable, the
    SDK's own body is returned, and a float in it is converted by the
    normaliser's single rule, Decimal(repr(value)).
    """
    raw = getattr(getattr(response, "response", None), "data", None)
    if isinstance(raw, (bytes, str)) and raw:
        return parse_exact(raw)
    return getattr(response, "body", None)


def _sdk_for(credentials: Credentials) -> Any:
    # Imported here so that nothing else in the plugin, synthetic mode
    # included, loads SnapTrade's SDK.
    from snaptrade_client import SnapTrade, SnapTradeAuth

    make = (
        SnapTradeAuth.personal_api_key
        if credentials.personal
        else (SnapTradeAuth.commercial_api_key)
    )
    return SnapTrade(
        auth=make(consumer_key=credentials.consumer_key, client_id=credentials.client_id)
    )


class SnapTradeVenue:
    """SnapTrade itself, through its official Python SDK.

    The SDK is synchronous; each call runs in a worker thread so the plugin's
    event loop, and its heartbeat, carry on meanwhile.
    """

    def __init__(self, credentials: Credentials, sdk: Any | None = None) -> None:
        self._credentials = credentials
        self._sdk = sdk if sdk is not None else _sdk_for(credentials)

    def __repr__(self) -> str:
        return "SnapTradeVenue(personal)" if self._credentials.personal else "SnapTradeVenue()"

    def _user(self) -> dict[str, str]:
        if self._credentials.personal:
            return {}
        return {
            "user_id": self._credentials.user_id,
            "user_secret": self._credentials.user_secret,
        }

    async def _call(self, asked: str, call: Callable[[], Any]) -> Any:
        try:
            response = await asyncio.to_thread(call)
        except Exception as failed:
            # `from None`: the SDK's exception, its message and its traceback
            # can each carry the request URL, and the URL the user secret.
            raise VenueError(asked, failed) from None
        return exact_body(response)

    async def users(self) -> list[str]:
        body = await self._call(
            "listing SnapTrade users", self._sdk.authentication.list_snap_trade_users
        )
        return [str(user) for user in body] if isinstance(body, list) else []

    async def connections(self) -> list[Json]:
        body = await self._call(
            "listing connections",
            lambda: self._sdk.connections.list_brokerage_authorizations(**self._user()),
        )
        return _objects(body)

    async def accounts(self) -> list[Json]:
        body = await self._call(
            "listing accounts",
            lambda: self._sdk.account_information.list_user_accounts(**self._user()),
        )
        return _objects(body)

    async def positions(self, account_id: str) -> Json:
        body = await self._call(
            "reading positions",
            lambda: self._sdk.account_information.get_all_account_positions(
                account_id=account_id, **self._user()
            ),
        )
        if not isinstance(body, dict):
            raise VenueError("reading positions")
        return body

    async def balances(self, account_id: str) -> list[Json]:
        body = await self._call(
            "reading balances",
            lambda: self._sdk.account_information.get_user_account_balance(
                account_id=account_id, **self._user()
            ),
        )
        return _objects(body)

    async def connection_portal(self, reconnect: str | None = None) -> str:
        body = await self._call(
            "opening the Connection Portal",
            lambda: self._sdk.authentication.login_snap_trade_user(
                # Read-only: this plugin is a custody connector and never trades.
                connection_type="read",
                **({"reconnect": reconnect} if reconnect else {}),
                **self._user(),
            ),
        )
        link = body.get("redirectURI") if isinstance(body, dict) else None
        if not isinstance(link, str) or not link:
            raise VenueError("opening the Connection Portal")
        return link

    async def refresh(self, connection_id: str) -> str:
        body = await self._call(
            "refreshing a connection",
            lambda: self._sdk.connections.refresh_brokerage_authorization(
                authorization_id=connection_id, **self._user()
            ),
        )
        detail = body.get("detail") if isinstance(body, dict) else None
        return detail if isinstance(detail, str) else "SnapTrade will read it again."


def _objects(body: Any) -> list[Json]:
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def utc_now() -> datetime:
    return datetime.now(UTC)
