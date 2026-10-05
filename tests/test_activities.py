"""The custodian's activity (contract v14, W2.10): each of SnapTrade's
activities on a linked account reported as SnapTrade states it, a backfill
back to the account's history_from on its first read and each read's after,
recorded once however often it is sent; its kind converted, its signs the
platform's, its instrument resolved as a holding's is or by a person's
plan-code link, and its raw record its own, kept as long as the history it
reported."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import meridian
import pytest
from meridian.plugin.v1 import operations_pb2 as ops

from snaptrade import synthetic
from snaptrade.activities import (
    ACTIVITY_TYPE_SCHEME,
    KINDS,
    SYMBOL_SCHEME,
    Converted,
    PlanCodeLink,
    convert,
)
from snaptrade.contract import ActivityOutcome, Recorder
from snaptrade.declaration import DECLARATION, seen
from snaptrade.linking import Links
from snaptrade.normalise import ExternalAccount, Holding, Identifier, Side
from snaptrade.page import RAW, RAW_DOWNLOAD, hold, pages
from snaptrade.raw import (
    ACTIVITY_RETENTION_DAYS,
    RawStore,
    activity_key,
    parse_activity_key,
)
from snaptrade.settings import SYNTHETIC, config_from
from snaptrade.sync import Syncer
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import ActivityPage, Json, VenueError

from conftest import NOW, Sidecar, clock, found

K = meridian.ActivityKind
ALPACA = "ALPACA:SYN-ALP-1001"
IBKR = "INTERACTIVE-BROKERS-FLEX:SYN-IB-2002"
SCHWAB = f"snaptrade:{synthetic.SCHWAB_BROKERAGE}"
# Alpaca's history, on fixed dates from its first transaction (synthetic.py).
ALPACA_HISTORY = 16


def given(kind: str, symbol: str | None = None, **changes: Any) -> Json:
    """One of SnapTrade's activities, as its account activities answer it."""
    base: Json = {
        "id": f"A-{kind}",
        "type": kind,
        "symbol": {"symbol": symbol, "description": f"{symbol} Inc."} if symbol else None,
        "units": Decimal(0),
        "price": Decimal(0),
        "amount": Decimal("0.00"),
        "fee": Decimal(0),
        "currency": {"code": "USD"},
        "trade_date": "2026-09-15T00:00:00.000Z",
        "settlement_date": "2026-09-16T00:00:00.000Z",
        "description": f"{kind} {symbol or ''}".strip(),
    }
    return {**base, **changes}


def converted(kind: str, symbol: str | None = None, **changes: Any) -> Converted:
    made = convert(given(kind, symbol, **changes))
    assert isinstance(made, Converted), made
    return made


# ── Converting SnapTrade's activities ───────────────────────────────────────


@pytest.mark.parametrize(
    ("snaptrade_type", "kind"),
    [
        ("BUY", K.ACTIVITY_KIND_PURCHASE),
        ("SELL", K.ACTIVITY_KIND_SALE),
        ("REI", K.ACTIVITY_KIND_REINVESTMENT),
        ("DIVIDEND", K.ACTIVITY_KIND_DIVIDEND),
        ("INTEREST", K.ACTIVITY_KIND_INTEREST),
        ("FEE", K.ACTIVITY_KIND_FEE),
        ("TAX", K.ACTIVITY_KIND_TAX),
        ("SPLIT", K.ACTIVITY_KIND_SPLIT),
        ("STOCK_DIVIDEND", K.ACTIVITY_KIND_CORPORATE_ACTION),
        ("EXTERNAL_ASSET_TRANSFER_IN", K.ACTIVITY_KIND_TRANSFER_IN),
        ("INTERNAL_ASSET_TRANSFER_IN", K.ACTIVITY_KIND_TRANSFER_IN),
        ("INTERNAL_CASH_TRANSFER_IN", K.ACTIVITY_KIND_TRANSFER_IN),
        ("EXTERNAL_ASSET_TRANSFER_OUT", K.ACTIVITY_KIND_TRANSFER_OUT),
        ("INTERNAL_ASSET_TRANSFER_OUT", K.ACTIVITY_KIND_TRANSFER_OUT),
        ("INTERNAL_CASH_TRANSFER_OUT", K.ACTIVITY_KIND_TRANSFER_OUT),
        ("CONTRIBUTION", K.ACTIVITY_KIND_CONTRIBUTION),
        ("WITHDRAWAL", K.ACTIVITY_KIND_WITHDRAWAL),
        ("JOURNALED", K.ACTIVITY_KIND_JOURNAL),
    ],
)
def test_each_type_the_plan_lists_converts_to_its_kind(
    snaptrade_type: str, kind: meridian.ActivityKind
) -> None:
    made = converted(snaptrade_type)
    assert made.kind == kind and made.kind_as_reported is None
    assert KINDS[snaptrade_type] == kind


@pytest.mark.parametrize(
    "snaptrade_type", ["OPTIONEXPIRATION", "ADJUSTMENT", "TRANSFER", "FXT"]
)
def test_any_other_type_is_sent_not_known_with_the_type_as_reported(
    snaptrade_type: str,
) -> None:
    made = converted(snaptrade_type)
    assert made.kind == K.ACTIVITY_KIND_UNSPECIFIED
    reported = made.kind_as_reported
    assert reported is not None
    assert (reported.scheme, reported.code, reported.text) == (
        ACTIVITY_TYPE_SCHEME,
        snaptrade_type,
        snaptrade_type,
    )


@pytest.mark.parametrize(
    ("snaptrade_type", "units", "sent"),
    [
        ("BUY", "-2.5", "2.5"),
        ("REI", "3.27", "3.27"),
        ("EXTERNAL_ASSET_TRANSFER_IN", "-40", "40"),
        ("SELL", "2.5", "-2.5"),
        ("SELL", "-2.5", "-2.5"),
        ("EXTERNAL_ASSET_TRANSFER_OUT", "81", "-81"),
        ("FEE", "0.5", "-0.5"),
        ("TAX", "0.1", "-0.1"),
        ("SPLIT", "40", "40"),
        ("SPLIT", "-20", "-20"),
    ],
)
def test_units_are_signed_by_what_they_did_to_the_account(
    snaptrade_type: str, units: str, sent: str
) -> None:
    assert converted(snaptrade_type, "X", units=Decimal(units)).units == Decimal(sent)


def test_the_amount_is_snaptrades_own_sign_and_its_currency() -> None:
    sale = converted(
        "SELL", "AAPL", units=Decimal("-2.5"), price=Decimal("225.00"), amount=Decimal("562.50")
    )
    assert sale.amount == meridian.Money(Decimal("562.50"), "USD")
    assert sale.price == meridian.Money(Decimal("225.00"), "USD")
    bought = converted("BUY", "AAPL", amount=Decimal("-586.25"))
    assert bought.amount == meridian.Money(Decimal("-586.25"), "USD")


def test_a_value_snaptrade_writes_as_zero_is_unset_and_no_price_is_worked_out() -> None:
    dividend = converted("DIVIDEND", "AAPL", amount=Decimal("3.25"))
    assert dividend.units is None and dividend.price is None
    split = converted("SPLIT", "TQQQ", units=40, amount=Decimal("0.00"))
    assert split.amount is None and split.price is None
    bought = converted("BUY", "AAPL", units=Decimal("2.5"), amount=Decimal("-586.25"))
    assert bought.price is None


def test_dates_are_the_date_part_as_written() -> None:
    made = converted("BUY", trade_date="2026-09-15T13:45:00.000Z", settlement_date=None)
    assert (made.trade_date, made.settlement_date) == ("2026-09-15", "")


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"id": ""}, "no identifier"),
        ({"id": "x" * 201}, "no identifier"),
        ({"trade_date": None}, "states no trade date"),
        ({"amount": Decimal("1e-19")}, "decimal places"),
        ({"amount": Decimal("3.25"), "currency": None}, "in no currency"),
    ],
)
def test_an_activity_that_cannot_be_sent_whole_is_not_sent_and_says_why(
    changes: dict[str, Any], why: str
) -> None:
    made = convert(given("DIVIDEND", "AAPL", **changes))
    assert isinstance(made, str) and why in made


def test_a_long_description_is_cut_to_the_bound() -> None:
    made = converted("BUY", description="d" * 600)
    assert len(made.description) == 500 and made.description.endswith("\N{HORIZONTAL ELLIPSIS}")


# ── The instrument ──────────────────────────────────────────────────────────


VIGIX = Holding(
    identifiers=(Identifier("symbol", "VIGIX", "snaptrade"),),
    description="Vanguard Growth Index Institutional",
    kind="mutualfund",
    side=Side.LONG,
    quantity=Decimal("120.5"),
    currency="USD",
)
ACCOUNT_OF_A_PLAN = ExternalAccount(
    external_account_id="FIDELITY:401K-1",
    stable=True,
    name="401(k)",
    account_type="401k",
    institution="Fidelity",
    connection_id="c-1",
    snaptrade_account_id="s-1",
    kind="retirement",
)


def record(
    sidecar: Sidecar,
    *made: Converted,
    links: tuple[PlanCodeLink, ...] = (),
    holdings: tuple[Holding, ...] = (VIGIX,),
) -> ActivityOutcome:
    outcome = ActivityOutcome()
    ready = [
        (each, activity_key(ACCOUNT_OF_A_PLAN, each.external_activity_id)) for each in made
    ]
    asyncio.run(
        Recorder(sidecar.plugin()).record_activities(
            ACCOUNT_OF_A_PLAN,
            ready,
            holdings,
            links,
            outcome,
        )
    )
    return outcome


def test_a_security_the_account_holds_resolves_by_that_holdings_identifiers() -> None:
    sidecar = Sidecar(resolve=lambda params: found("INS-VIGIX"))
    record(sidecar, converted("REI", "VIGIX", units=Decimal("0.4")))
    (resolve,) = sidecar.sent("ResolveIdentifier")
    assert [(i.scheme, i.value) for i in resolve.identifiers] == [("symbol", "VIGIX")]
    (sent,) = sidecar.sent("RecordActivity")
    assert sent.activity.instrument_id == "INS-VIGIX"
    assert not sent.activity.HasField("instrument_as_reported")
    assert list(sent.activity.provenance) == []


def test_a_plans_code_nobody_linked_travels_as_reported_and_resolves_nothing() -> None:
    sidecar = Sidecar()
    record(sidecar, converted("REI", "OQKR", units=Decimal("0.412")))
    assert sidecar.sent("ResolveIdentifier") == []
    (sent,) = sidecar.sent("RecordActivity")
    assert sent.activity.instrument_id == ""
    reported = sent.activity.instrument_as_reported
    assert (reported.scheme, reported.code, reported.text) == (
        SYMBOL_SCHEME,
        "OQKR",
        "OQKR Inc.",
    )


def test_a_plans_code_a_person_linked_resolves_as_the_symbol_with_their_name() -> None:
    sidecar = Sidecar(resolve=lambda params: found("INS-VIGIX"))
    link = PlanCodeLink("FIDELITY:401K-1", "OQKR", "VIGIX", "Pat Admin")
    record(sidecar, converted("REI", "OQKR", units=Decimal("0.412")), links=(link,))
    (resolve,) = sidecar.sent("ResolveIdentifier")
    assert [i.value for i in resolve.identifiers] == ["VIGIX"]
    (sent,) = sidecar.sent("RecordActivity")
    assert sent.activity.instrument_id == "INS-VIGIX"
    assert not sent.activity.HasField("instrument_as_reported")
    (said,) = sent.activity.provenance
    assert (said.field, said.kind, said.person) == (
        "instrument_id",
        ops.PROVENANCE_KIND_SUPPLIED,
        "Pat Admin",
    )


def test_a_link_on_another_account_does_not_apply() -> None:
    sidecar = Sidecar()
    link = PlanCodeLink("FIDELITY:OTHER", "OQKR", "VIGIX", "Pat Admin")
    record(sidecar, converted("REI", "OQKR"), links=(link,))
    (sent,) = sidecar.sent("RecordActivity")
    assert (
        sent.activity.instrument_id == ""
        and sent.activity.instrument_as_reported.code == "OQKR"
    )


def test_an_ambiguous_resolve_travels_as_reported_and_is_asked_once_a_read() -> None:
    sidecar = Sidecar(
        resolve=lambda params: ops.ResolveIdentifierResult(
            found=False, miss_reason=ops.MISS_REASON_AMBIGUOUS
        )
    )
    record(sidecar, converted("REI", "VIGIX"), converted("DIVIDEND", "VIGIX", amount=1))
    assert len(sidecar.sent("ResolveIdentifier")) == 1
    for sent in sidecar.sent("RecordActivity"):
        assert sent.activity.instrument_id == ""
        assert sent.activity.instrument_as_reported.code == "VIGIX"


def test_cash_alone_names_no_instrument() -> None:
    sidecar = Sidecar()
    record(sidecar, converted("CONTRIBUTION", amount=Decimal("5000.00")))
    (sent,) = sidecar.sent("RecordActivity")
    assert sent.activity.instrument_id == ""
    assert not sent.activity.HasField("instrument_as_reported")


def test_a_refusal_of_one_activity_is_said_and_the_rest_go_on() -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        if name == "RecordActivity" and params.activity.external_activity_id == "A-FEE":
            return meridian.CallFailed("RecordActivity", "invalid argument", "no")
        return None

    sidecar = Sidecar(refuse=refuse)
    outcome = record(sidecar, converted("FEE", amount=-1), converted("INTEREST", amount=1))
    assert outcome.sent == 1 and len(outcome.skipped) == 1 and "A-FEE" in outcome.skipped[0]
    assert not outcome.stopped


def test_an_unlinked_account_stops_its_activities() -> None:
    def refuse(name: str, params: Any) -> Exception | None:
        return meridian.NotLinked("RecordActivity", "not linked")

    outcome = record(
        Sidecar(refuse=refuse), converted("FEE", amount=-1), converted("INTEREST", amount=1)
    )
    assert outcome.unlinked and outcome.sent == 0 and outcome.skipped == []


# ── The backfill, then each read ────────────────────────────────────────────


def syncer_for(
    sidecar: Sidecar,
    tmp_path: Path,
    moment: datetime = NOW,
    linked: frozenset[str] = frozenset({ALPACA}),
    venue: Any = None,
) -> Syncer:
    syncer = Syncer(
        sidecar.plugin(),
        now=clock(moment),
        make_venue=(lambda config: venue) if venue is not None else None,
        raw=RawStore(tmp_path / "raw"),
        linked=lambda external_account_id: external_account_id in linked,
    )
    syncer.configure(config_from({SYNTHETIC: True}))
    return syncer


def sent_for(sidecar: Sidecar, external_account_id: str) -> list[Any]:
    return [
        params.activity
        for params in sidecar.sent("RecordActivity")
        if params.external_account_id == external_account_id
    ]


def test_the_first_read_backfills_every_activity_back_to_history_from(tmp_path: Path) -> None:
    sidecar = Sidecar()
    syncer = syncer_for(sidecar, tmp_path)
    status = asyncio.run(syncer.run_once())
    sent = sent_for(sidecar, ALPACA)
    assert len(sent) == ALPACA_HISTORY == len(sidecar.activities)
    assert {a.kind for a in sent} == set(KINDS.values()) | {K.ACTIVITY_KIND_UNSPECIFIED}
    assert min(a.trade_date for a in sent) == "2025-06-02"
    outcome = status.activities[ALPACA]
    assert outcome.backfill and outcome.complete
    assert (outcome.sent, outcome.recorded) == (ALPACA_HISTORY, ALPACA_HISTORY)
    # Each names the source, the external account, and its own raw record.
    for params in sidecar.sent("RecordActivity"):
        assert (params.source, params.external_account_id) == ("snaptrade", ALPACA)
        assert params.activity.raw_record.key == (
            f"activities/{ALPACA}/{params.activity.external_activity_id}"
        )
        assert params.activity.raw_record.instance_id == "snaptrade"


def test_the_backfill_asks_snaptrade_from_history_from_page_by_page(tmp_path: Path) -> None:
    asked: list[tuple[date | None, int, int]] = []

    class Paged(SyntheticVenue):
        async def activity_page(
            self, account_id: str, start: date | None, end: date, offset: int, limit: int
        ) -> ActivityPage:
            asked.append((start, offset, limit))
            return await super().activity_page(account_id, start, end, offset, limit)

    sidecar = Sidecar()
    asyncio.run(syncer_for(sidecar, tmp_path, venue=Paged(clock())).run_once())
    assert asked == [(date(2025, 6, 2), 0, 1000)]


def test_the_sync_status_carries_history_from(tmp_path: Path) -> None:
    sidecar = Sidecar()
    asyncio.run(syncer_for(sidecar, tmp_path).run_once())
    stated = {s.external_account_id: s.history_from for s in sidecar.sent("ReportSyncStatus")}
    assert stated == {ALPACA: "2025-06-02", IBKR: "2025-01-06", SCHWAB: "2026-03-03"}


def test_after_the_backfill_each_read_reports_what_it_fetched(tmp_path: Path) -> None:
    sidecar = Sidecar()
    linked = frozenset({ALPACA, IBKR})
    syncer = syncer_for(sidecar, tmp_path, linked=linked)
    asyncio.run(syncer.run_once())
    first = len(sidecar.sent("RecordActivity"))
    asyncio.run(syncer.run_once())
    again = sidecar.sent("RecordActivity")[first:]
    # Alpaca's history is older than a read's ten days; IBKR's SAP.DE bought
    # yesterday is in them, sent again and answered as already recorded.
    assert [a.activity.external_activity_id[-4:] for a in again] == ["f002"]
    status = syncer.status.activities
    assert not status[IBKR].backfill and status[IBKR].sent == 1
    assert status[IBKR].recorded == 0


def test_a_restart_backfills_again_and_nothing_is_recorded_twice(tmp_path: Path) -> None:
    sidecar = Sidecar()
    asyncio.run(syncer_for(sidecar, tmp_path).run_once())
    recorded = dict(sidecar.activities)
    status = asyncio.run(syncer_for(sidecar, tmp_path).run_once())
    assert sidecar.activities == recorded
    assert len(sent_for(sidecar, ALPACA)) == 2 * ALPACA_HISTORY
    assert status.activities[ALPACA].recorded == 0


def test_an_account_nothing_links_reports_none_until_it_is_linked(tmp_path: Path) -> None:
    sidecar = Sidecar()
    linked: set[str] = set()
    syncer = Syncer(
        sidecar.plugin(),
        now=clock(),
        raw=RawStore(tmp_path / "raw"),
        linked=lambda external_account_id: external_account_id in linked,
    )
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    assert sidecar.sent("RecordActivity") == []
    linked.add(ALPACA)
    status = asyncio.run(syncer.run_once())
    assert len(sent_for(sidecar, ALPACA)) == ALPACA_HISTORY
    assert status.activities[ALPACA].backfill


def test_a_backfill_snaptrade_did_not_answer_is_tried_again_on_the_next_read(
    tmp_path: Path,
) -> None:
    class Failing(SyntheticVenue):
        failing = True

        async def activity_page(
            self, account_id: str, start: date | None, end: date, offset: int, limit: int
        ) -> ActivityPage:
            if self.failing:
                raise VenueError("reading activities")
            return await super().activity_page(account_id, start, end, offset, limit)

    venue = Failing(clock())
    sidecar = Sidecar()
    syncer = syncer_for(sidecar, tmp_path, venue=venue)
    status = asyncio.run(syncer.run_once())
    assert status.activities[ALPACA].stopped.startswith("reading activities failed")
    assert sidecar.sent("RecordActivity") == []
    venue.failing = False
    status = asyncio.run(syncer.run_once())
    assert status.activities[ALPACA].backfill and status.activities[ALPACA].complete
    assert len(sent_for(sidecar, ALPACA)) == ALPACA_HISTORY


def test_the_read_and_its_health_are_as_they_were(tmp_path: Path) -> None:
    sidecar = Sidecar()
    asyncio.run(syncer_for(sidecar, tmp_path).run_once())
    assert sidecar.reports[-1] == (
        True,
        "read 3 accounts through 3 connections (synthetic)",
    )


# ── Each activity's raw record ──────────────────────────────────────────────


def test_each_activity_names_a_record_the_store_keeps_as_snaptrade_sent_it(
    tmp_path: Path,
) -> None:
    sidecar = Sidecar()
    syncer = syncer_for(sidecar, tmp_path)
    asyncio.run(syncer.run_once())
    store = syncer.raw
    assert store is not None
    for params in sidecar.sent("RecordActivity"):
        key = params.activity.raw_record.key
        assert parse_activity_key(key) == (ALPACA, params.activity.external_activity_id)
        held = store.find(key)
        assert held is not None
        kept, call = held
        assert call["body"]["id"] == params.activity.external_activity_id
        assert call["call"] == "reading activities"
        assert "the backfill to 2025-06-02" in call["note"]
        assert kept.read_at == NOW


def test_an_activity_record_is_kept_past_the_read_retention_and_written_once(
    tmp_path: Path,
) -> None:
    sidecar = Sidecar()
    asyncio.run(syncer_for(sidecar, tmp_path, NOW - timedelta(days=400)).run_once())
    later = syncer_for(sidecar, tmp_path)
    asyncio.run(later.run_once())
    store = later.raw
    assert store is not None
    # The reads 400 days ago are pruned; the activities they reported are not,
    # and still name the read that first reported them.
    assert [r.read_at for r in store.reads(ALPACA)] == [NOW]
    key = sidecar.sent("RecordActivity")[0].activity.raw_record.key
    held = store.find(key)
    assert held is not None and held[0].read_at == NOW - timedelta(days=400)
    # Past the activity retention, they go too.
    store.prune(NOW + timedelta(days=ACTIVITY_RETENTION_DAYS - 399))
    assert store.find(key) is None


def test_the_declaration_asks_storage_for_the_history_it_reports() -> None:
    assert DECLARATION.storage is not None
    assert DECLARATION.storage.retention_days == ACTIVITY_RETENTION_DAYS


def test_a_fee_or_exchange_rate_an_activity_states_is_counted_not_carried() -> None:
    from snaptrade.venue import Snapshot

    read = Snapshot(
        read_at=NOW,
        connections=[],
        accounts=[],
        activities={
            "s-1": [
                given("BUY", "AAPL", fee=Decimal("1.00"), fx_rate=Decimal("1.35")),
                given("SELL", "AAPL"),
            ]
        },
    )
    assert seen(read) == [("snaptrade:activity", "fee"), ("snaptrade:activity", "fx_rate")]
    declared = {(n.scheme, n.name) for n in DECLARATION.not_carried}
    assert {("snaptrade:activity", "fee"), ("snaptrade:activity", "fx_rate")} <= declared


# ── The Raw responses tab follows an activity's reference ───────────────────


def test_the_raw_responses_tab_resolves_an_activitys_reference(tmp_path: Path) -> None:
    from meridian.testing import PageClient

    sidecar = Sidecar()
    syncer = syncer_for(sidecar, tmp_path)
    asyncio.run(syncer.run_once())
    held = Links(sidecar.plugin())
    asyncio.run(
        held.hold(
            meridian.AccountScope(links=(meridian.LinkedExternalAccount(ALPACA, "ACC-1", "H"),))
        )
    )
    hold(syncer, held, asyncio.Event())
    client = PageClient(pages, Sidecar().plugin(), read={"ACC-1"})
    key = f"activities/{ALPACA}/00000000-0000-4000-8000-00000000f107"
    answer = client.get(RAW, "read", ref=key)
    assert answer.status == 200
    assert "The record an activity references" in answer.text
    assert "REINVESTMENT SYNTHETIC TREASURY MONEY FUND (SYNXX)" in answer.text
    download = client.get(RAW_DOWNLOAD, "read", account=ALPACA, read=key)
    assert download.status == 200 and "f107" in download.text
    # Not one the person may read: the same answer as no such record.
    other = PageClient(pages, Sidecar().plugin(), read={"ACC-3"})
    assert other.get(RAW, "read", ref=key).status == 404
    # One no longer kept is said to be gone, the account's reads beside it.
    gone = client.get(RAW, "read", ref=f"activities/{ALPACA}/no-such")
    assert gone.status == 200 and "f107" not in gone.text
