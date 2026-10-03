"""The custody suite (contract v11): every case, from SnapTrade's own words.

The SDK's runner (meridian.suites) holds a custody plugin to its role's suite:
each case says, in the contract's words, what the source presents and what
the plugin sends. Here each case is mapped to what SnapTrade answers for it
-- its connections, accounts, positions, balances and activities, as JSON in
SnapTrade's own shapes -- and run through this plugin's own conversion
(normalise.py, contract.py, linking.py) against the runner's recorder, as a
read does. Nothing here is a stand-in for the conversion: only the sidecar is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import meridian
import pytest
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.suites import Recorder, run, suite
from meridian.testing import caller_header

from snaptrade.contract import Recorder as Contract
from snaptrade.declaration import seen
from snaptrade.linking import Links
from snaptrade.normalise import ns, views
from snaptrade.venue import Snapshot

from conftest import NOW

STALE_AFTER = timedelta(hours=36)
SYNCED = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
TOMORROW = (NOW + timedelta(days=1)).date().isoformat()
YESTERDAY = (NOW - timedelta(days=1)).date().isoformat()
# A plugin admin who is a deployment admin, on the Account links tab.
ADMIN = caller_header("admin", deployment_admin=True)
ACCOUNT_ID = "00000000-0000-4000-8000-0000000000a1"
CONNECTION_ID = "00000000-0000-4000-8000-0000000000c1"

Json = dict[str, Any]


def connection(**changes: Any) -> Json:
    base: Json = {
        "id": CONNECTION_ID,
        "brokerage": {"slug": "ALPACA", "name": "Alpaca", "display_name": "Alpaca"},
        "name": "Connection 1",
        "disabled": False,
    }
    return {**base, **changes}


def account(**changes: Any) -> Json:
    holdings = {"initial_sync_completed": True, "last_successful_sync": SYNCED}
    base: Json = {
        "id": ACCOUNT_ID,
        "brokerage_authorization": CONNECTION_ID,
        "name": "Margin",
        "number": "SYN001",
        "institution_name": "Alpaca",
        "institution_account_id": "INST-1",
        "raw_type": "margin",
        "sync_status": {
            "holdings": {**holdings, **changes.pop("holdings", {})},
            "transactions": {"initial_sync_completed": True, "last_successful_sync": YESTERDAY},
        },
    }
    return {**base, **changes}


def position(symbol: str, units: str, **changes: Any) -> Json:
    instrument = {
        "kind": "stock",
        "id": "00000000-0000-4000-8000-0000000000d1",
        "symbol": symbol,
        "raw_symbol": symbol,
        "currency": "USD",
        "exchange": "XNAS",
        "description": f"{symbol} Inc.",
    }
    return {
        "instrument": {**instrument, **changes.pop("instrument", {})},
        "units": units,
        **changes,
    }


def balance(code: str, cash: str) -> Json:
    return {"currency": {"code": code, "name": code}, "cash": cash, "buying_power": cash}


def buy(symbol: str, units: str, settles: str) -> Json:
    """One of SnapTrade's activities: a purchase traded today, settling later."""
    return {
        "type": "BUY",
        "symbol": {"symbol": symbol},
        "units": units,
        "amount": "-1000.00",
        "currency": {"code": "USD"},
        "trade_date": NOW.date().isoformat(),
        "settlement_date": settles,
    }


def snapshot(
    *,
    connection_: Json | None = None,
    account_: Json | None = None,
    positions: list[Json] | None = None,
    balances: list[Json] | None = None,
    activities: list[Json] | None = None,
) -> Snapshot:
    """One read of SnapTrade, for one account."""
    return Snapshot(
        read_at=NOW,
        connections=[connection_ or connection()],
        accounts=[account_ or account()],
        positions={ACCOUNT_ID: {"results": positions or []}},
        balances={ACCOUNT_ID: balances or []},
        activities={ACCOUNT_ID: activities or []},
    )


async def sync(recorder: Recorder, read: Snapshot) -> None:
    """What a read does with what SnapTrade answered (sync.py): the accounts
    reported, each one's sync state, and the statement of each that can be
    served clean."""
    contract = Contract(cast(meridian.Plugin, recorder))
    shown = [view for each in views(read, STALE_AFTER) for view in each.accounts]
    await contract.report_accounts([view.account for view in shown])
    for view in shown:
        await contract.report_sync(view.account, view.freshness, ns(read.read_at))
        if view.statement is not None:
            await contract.record(view.account, view.statement, ns(read.read_at))


def reading(**read: Any) -> Callable[[Recorder], Coroutine[Any, Any, None]]:
    async def produce(recorder: Recorder) -> None:
        await sync(recorder, snapshot(**read))

    return produce


def ambiguous_for(symbol: str) -> Callable[[Recorder], Coroutine[Any, Any, None]]:
    """A read where `symbol`'s identifiers meet more than one record."""

    async def produce(recorder: Recorder) -> None:
        recorder.answer(
            "ResolveIdentifier",
            lambda params: (
                ops.ResolveIdentifierResult(found=False, miss_reason=ops.MISS_REASON_AMBIGUOUS)
                if any(i.value == symbol for i in params.identifiers)
                else ops.ResolveIdentifierResult(found=True, instrument_id="INS-1")
            ),
        )
        await sync(
            recorder,
            snapshot(
                positions=[position(symbol, "3", instrument={"kind": "tokenized_asset"})],
                balances=[balance("USD", "100.00")],
            ),
        )

    return produce


async def read_for_linking(recorder: Recorder) -> None:
    await Links(cast(meridian.Plugin, recorder)).offered(ADMIN)


async def link_a_new_account(recorder: Recorder) -> None:
    """The Account links form for a margin account, the new account's type
    pre-filled from its kind (page.py) and its custodian from its connection."""
    (shown,) = views(snapshot(), STALE_AFTER)
    (view,) = shown.accounts
    await Links(cast(meridian.Plugin, recorder), settle_seconds=0).link(
        ADMIN,
        view.account.external_account_id,
        name="Alpaca margin",
        custodian=shown.institution,
        account_type=view.account.kind,
    )


FUND = position(
    "SYNXX",
    "500.00",
    price="1.00",
    cash_equivalent=True,
    instrument={"kind": "mutualfund", "description": "Synthetic money market fund"},
)

PRODUCERS: dict[str, Callable[[Recorder], Coroutine[Any, Any, None]]] = {
    "sync-current": reading(),
    "sync-stale": reading(
        account_=account(holdings={"last_successful_sync": "2026-09-20T00:00:00Z"})
    ),
    # SnapTrade says a connection's access lapsed only by disabling it, so a
    # disabled connection needs sign-in, derived by the rule that says so...
    "sync-needs-sign-in": reading(
        connection_=connection(disabled=True, disabled_date="2026-09-27T00:00:00Z")
    ),
    # ...unless SnapTrade has turned its brokerage off, which signing in
    # cannot mend: then it is disabled.
    "sync-disabled": reading(
        connection_=connection(
            disabled=True,
            disabled_date="2026-09-27T00:00:00Z",
            brokerage={"slug": "ALPACA", "name": "Alpaca", "enabled": False},
        )
    ),
    "sync-delayed-by-design": reading(
        connection_=connection(data_freshness_mode={"institution": "delayed"})
    ),
    "sync-holdings-unavailable": reading(
        account_=account(holdings={"holdings_unavailable": True})
    ),
    "account-kind-cash": reading(account_=account(raw_type="cash")),
    "account-kind-margin": reading(account_=account(raw_type="margin")),
    "account-kind-retirement": reading(account_=account(raw_type="Roth IRA")),
    "account-kind-not-known": reading(account_=account(raw_type="Joint")),
    "read-accounts-for-linking": read_for_linking,
    "link-a-new-account": link_a_new_account,
    "a-security-holding": reading(positions=[position("AAPL", "12.5")]),
    "institution-not-stated": reading(account_=account(institution_name=None)),
    "cash-net-of-a-fund-counted-in-cash": reading(
        positions=[FUND], balances=[balance("USD", "1523.45")]
    ),
    "fund-larger-than-cash": reading(positions=[FUND], balances=[balance("USD", "100.00")]),
    "currency-not-stated": reading(
        positions=[position("SYNX", "7", cost_basis="12.34", instrument={"currency": None})],
        balances=[balance("CAD", "10.00"), balance("USD", "10.00")],
    ),
    "settled-quantity-not-stated": reading(positions=[position("AAPL", "12.5")]),
    "pending-by-value-date": reading(
        positions=[position("SAP", "30")], activities=[buy("SAP", "5", TOMORROW)]
    ),
    "resolve-states-what-the-source-states": reading(positions=[position("AAPL", "12.5")]),
    "kind-not-converted-on-an-ambiguous-resolve": ambiguous_for("TOKN"),
}


def test_every_case_of_the_custody_suite_passes() -> None:
    """Every case, none declared not presented: the canonical role is the
    requirement (the product owner, 2026-10-02 and 2026-10-03)."""
    report = run("custody", PRODUCERS, instance_id="snaptrade")
    assert report.passed, report.failures
    assert not report.not_presented
    assert set(report.passed_cases) == {case.name for case in suite("custody").cases}


def sent_sync(read: Callable[[Recorder], Coroutine[Any, Any, None]]) -> Any:
    recorder = Recorder("snaptrade")
    asyncio.run(read(recorder))
    (sent,) = recorder.on("ReportSyncStatus")
    return sent


def test_needing_sign_in_says_the_rule_it_was_derived_by() -> None:
    """The state SnapTrade did not say crosses with its provenance: the rule,
    in the detail, since the sync status carries no provenance field."""
    sent = sent_sync(PRODUCERS["sync-needs-sign-in"])
    assert sent.state == meridian.SyncState.SYNC_STATE_NEEDS_SIGN_IN
    assert not sent.connection_healthy
    assert 'derived by the rule "SnapTrade disabled the connection"' in sent.status_detail
    assert "2026-09-27" in sent.status_detail


def test_disabled_with_its_brokerage_off_says_its_rule() -> None:
    sent = sent_sync(PRODUCERS["sync-disabled"])
    assert sent.state == meridian.SyncState.SYNC_STATE_DISABLED
    assert (
        'derived by the rule "SnapTrade disabled the connection and turned its brokerage off"'
        in sent.status_detail
    )


def test_a_cached_read_older_than_allowed_is_stale_as_of_its_own_time() -> None:
    """SnapTrade's account says it synced minutes ago, but the positions it
    served were fetched from the brokerage three days ago: the sync status is
    stale as of then, and the statement is as of that day, not the read's."""
    cached = (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")

    async def produce(recorder: Recorder) -> None:
        read = snapshot(positions=[position("AAPL", "12.5")], balances=[balance("USD", "1")])
        read.positions[ACCOUNT_ID]["data_freshness"] = {"as_of": cached}
        await sync(recorder, read)

    recorder = Recorder("snaptrade")
    asyncio.run(produce(recorder))
    (sent,) = recorder.on("ReportSyncStatus")
    assert sent.state == meridian.SyncState.SYNC_STATE_STALE
    assert sent.holdings_as_of_ns == ns(NOW - timedelta(days=3))
    assert sent.last_synced_at_ns == ns(NOW - timedelta(minutes=5))
    (opened,) = recorder.on("RecordHoldingsStatement")
    assert opened.as_of_date == (NOW - timedelta(days=3)).date().isoformat()
    assert not [p for p in opened.provenance if p.field == "as_of_date"]


def test_the_cash_sent_net_of_the_fund_is_the_cash_less_the_fund() -> None:
    recorder = Recorder("snaptrade")
    asyncio.run(PRODUCERS["cash-net-of-a-fund-counted-in-cash"](recorder))
    quantities = sorted(
        meridian.as_decimal(row.quantity) for row in recorder.on("RecordHolding")
    )
    assert quantities == [Decimal("500.00"), Decimal("1023.45")]


def test_a_read_counts_what_it_does_not_carry_by_name() -> None:
    lots = [{"lot_id": "L-1", "quantity": "12.5"}]
    read = snapshot(positions=[position("AAPL", "12.5", price="231.40", tax_lots=lots)])
    assert seen(read) == [("snaptrade:position", "price"), ("snaptrade:tax-lot", "lot_id")]


def test_a_mutation_sending_stale_for_holdings_unavailable_fails_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suite tests the outcome: a conversion that says stale where the
    brokerage gives no holdings fails the case that says so, and only it."""
    from snaptrade import contract
    from snaptrade.normalise import SyncState

    mutated = {**contract._SYNC_STATE}
    mutated[SyncState.HOLDINGS_UNAVAILABLE] = meridian.SyncState.SYNC_STATE_STALE
    monkeypatch.setattr(contract, "_SYNC_STATE", mutated)
    report = run("custody", PRODUCERS, instance_id="snaptrade")
    assert set(report.failures) == {"sync-holdings-unavailable"}
