"""An account's history as SnapTrade reports it, and lots proposed from it
(history.py), each a page and a tool on the deployment's MCP surface; the
routes typed and offered as tools, or saying why not (contract v12). On the
synthetic venue: IBKR's SYNQ bought twice, SYNB transferred in, SYNV bought
and partly sold; Schwab's activities unreadable."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import meridian
import pytest
from meridian.testing import PageClient

from snaptrade import history, synthetic
from snaptrade.linking import Links
from snaptrade.normalise import Holding, Identifier, Lot, Side
from snaptrade.page import (
    ACCOUNTS,
    CONNECT,
    CONNECTIONS,
    HISTORY,
    LINK,
    LOTS,
    NO_PORTAL,
    RAW,
    RAW_DOWNLOAD,
    READ,
    READING,
    REAL_TIME,
    RECONNECT,
    REFRESH,
    STATEMENTS,
    hold,
    pages,
)
from snaptrade.raw import RawStore
from snaptrade.settings import SYNTHETIC, config_from
from snaptrade.sync import Syncer

from conftest import NOW, Sidecar, clock

ALPACA = "ALPACA:SYN-ALP-1001"
IBKR = "INTERACTIVE-BROKERS-FLEX:SYN-IB-2002"
SCHWAB = f"snaptrade:{synthetic.SCHWAB_BROKERAGE}"
LINKS = (
    meridian.LinkedExternalAccount(ALPACA, "ACC-1", "Household"),
    meridian.LinkedExternalAccount(IBKR, "ACC-3", "Spare"),
    meridian.LinkedExternalAccount(SCHWAB, "ACC-4", "Old Schwab"),
)
TODAY = NOW.date()
READ_AT = "2026-09-28 15:00 UTC"
AVERAGE = f"SnapTrade average purchase price, {READ_AT}"


def days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


@pytest.fixture
def held(tmp_path: Path) -> Iterator[tuple[Sidecar, Syncer, asyncio.Event]]:
    """The synthetic read held by the pages, its raw responses kept, with
    Alpaca linked to ACC-1, IBKR to ACC-3 and Schwab to ACC-4."""
    sidecar = Sidecar(links=LINKS)
    syncer = Syncer(sidecar.plugin(), now=clock(), raw=RawStore(tmp_path / "raw"))
    syncer.configure(config_from({SYNTHETIC: True}))
    asyncio.run(syncer.run_once())
    links = Links(sidecar.plugin(), settle_seconds=0.05)
    asyncio.run(links.hold(sidecar.scope()))
    woken = asyncio.Event()
    hold(syncer, links, woken)
    yield sidecar, syncer, woken


def reader(sidecar: Sidecar, *accounts: str) -> PageClient:
    return PageClient(
        pages, sidecar.plugin(), read=set(accounts or ("ACC-1", "ACC-3", "ACC-4"))
    )


# ── Every route a tool, or saying why not ───────────────────────────────────


def test_every_route_is_typed_and_offered_as_a_tool_or_says_why_not() -> None:
    offered = {tool.name: (tool.method, tool.path, tool.reads) for tool in pages.tools}
    assert offered == {
        "read_connections": ("GET", CONNECTIONS, True),
        "open_connection_portal": ("POST", CONNECT, False),
        "refresh_connection": ("POST", REFRESH, False),
        "reconnect_connection": ("POST", RECONNECT, False),
        "read_account_links": ("GET", ACCOUNTS, True),
        "link_account": ("POST", LINK, False),
        "read_statements": ("GET", STATEMENTS, True),
        "read_account_activities": ("GET", HISTORY, True),
        "read_proposed_lots": ("GET", LOTS, True),
        "read_snaptrade_now": ("POST", READ, False),
    }
    # The routes kept from agents, each with why: none of them changes anything.
    kept = {(each.method, each.path): each.why for each in pages.not_offered}
    assert set(kept) == {("GET", "/admin"), ("GET", RAW), ("GET", RAW_DOWNLOAD)}
    assert all(why for why in kept.values())
    # Each tool's levels are its route's, and every tool declares typed inputs.
    by_name = {tool.name: tool for tool in pages.tools}
    assert by_name["read_account_activities"].levels == by_name["read_statements"].levels
    for tool in pages.tools:
        schema = json.loads(tool.declared().input_schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False


def test_the_history_tools_declare_their_bounds() -> None:
    by_name = {tool.name: tool for tool in pages.tools}
    schema = json.loads(by_name["read_account_activities"].declared().input_schema)
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == history.MOST_LIMIT == 1000
    assert schema["properties"]["offset"]["minimum"] == 0
    assert schema["properties"]["account"]["maxLength"] == history.MOST_ACCOUNT
    answer = json.loads(by_name["read_proposed_lots"].declared().output_schema)
    lot = json.dumps(answer)
    for name in ("quantity", "cost", "currency", "acquired", "source"):
        assert f'"{name}"' in lot


# ── Activities over a range ─────────────────────────────────────────────────


def test_an_accounts_activities_over_a_range_as_snaptrade_reports_them(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, syncer, _ = held
    answer = reader(sidecar).call_tool(
        "read_account_activities",
        {"account": "ACC-3", "start": days_ago(365), "end": str(TODAY)},
    )
    assert answer.status == 200 and answer.outcome == "unchanged", answer.json
    data = answer.data
    assert data["external_account_id"] == IBKR
    # How far back SnapTrade holds the account's history, as it reports it.
    assert data["history_from"] == "2025-01-06"
    assert data["start"] == days_ago(365) and data["end"] == str(TODAY)
    assert data["total"] == 7 and data["offset"] == 0 and data["limit"] == 100
    assert [(a["type"], a["symbol"]) for a in data["activities"]] == [
        ("BUY", "SYNQ"),
        ("BUY", "SYNV"),
        ("EXTERNAL_ASSET_TRANSFER_IN", "SYNB"),
        ("BUY", "SYNQ"),
        ("SELL", "SYNV"),
        ("DIVIDEND", "SYNQ"),
        ("BUY", "SAP.DE"),
    ]
    first = data["activities"][0]
    # As written: its numbers exact, as strings; its dates as SnapTrade wrote them.
    assert first == {
        "id": "00000000-0000-4000-8000-00000000f003",
        "type": "BUY",
        "trade_date": f"{days_ago(300)}T00:00:00.000Z",
        "settlement_date": f"{days_ago(299)}T00:00:00.000Z",
        "symbol": "SYNQ",
        "option_symbol": "",
        "description": "Bought 20 SYNQ",
        "units": "20",
        "price": "38.00",
        "amount": "-760.00",
        "fee": "0",
        "currency": "USD",
    }
    # A transfer states no amount: none, never zero.
    assert data["activities"][2]["amount"] is None
    # The read is kept as the account's raw record, and the answer names it.
    assert syncer.raw is not None
    found = syncer.raw.find(data["raw_record"])
    assert found is not None
    record, call = found
    assert record.external_account_id == IBKR and call["call"] == "reading activities"
    assert call["note"] == f"traded from {days_ago(365)} to {TODAY}; 100 from 0"
    assert len(call["body"]["data"]) == 7


def test_a_narrower_range_reads_only_its_days_and_a_page_at_a_time(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    client = reader(sidecar)
    narrow = client.call_tool(
        "read_account_activities",
        {"account": "ACC-3", "start": days_ago(130), "end": days_ago(80)},
    )
    assert [a["id"][-4:] for a in narrow.data["activities"]] == ["f006", "f007"]
    paged = client.call_tool(
        "read_account_activities",
        {
            "account": "ACC-3",
            "start": days_ago(365),
            "end": str(TODAY),
            "offset": 2,
            "limit": 2,
        },
    )
    assert paged.data["total"] == 7
    assert [a["id"][-4:] for a in paged.data["activities"]] == ["f005", "f006"]
    # The 30 days to today where no range is given: yesterday's SAP.DE buy.
    recent = client.call_tool("read_account_activities", {"account": "ACC-3"})
    assert recent.data["start"] == days_ago(30) and recent.data["end"] == str(TODAY)
    assert [a["symbol"] for a in recent.data["activities"]] == ["SAP.DE"]


@pytest.mark.parametrize(
    ("arguments", "paths"),
    [
        ({"end": (TODAY + timedelta(days=1)).isoformat()}, ["end"]),
        ({"start": days_ago(5), "end": days_ago(10)}, ["start"]),
        ({"start": days_ago(366), "end": str(TODAY)}, ["start"]),
        ({"limit": 0}, ["limit"]),
        ({"limit": 1001}, ["limit"]),
        ({"offset": -1}, ["offset"]),
        ({"start": "2026-13-01"}, ["start"]),
        ({"limit": "100"}, ["limit"]),
        ({"since": "2026-01-01"}, ["since"]),
    ],
)
def test_a_range_or_page_out_of_bounds_is_refused_by_path(
    held: tuple[Sidecar, Syncer, asyncio.Event], arguments: dict[str, Any], paths: list[str]
) -> None:
    sidecar, _, _ = held
    answer = reader(sidecar).call_tool(
        "read_account_activities", {"account": "ACC-3", **arguments}
    )
    assert answer.outcome == "refused", answer.json
    assert answer.paths == paths


def test_an_account_not_the_persons_or_not_linked_is_refused_by_path(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    client = PageClient(pages, sidecar.plugin(), read={"ACC-1", "ACC-9"})
    for account, reason in (
        ("ACC-3", "permission_denied"),
        ("ACC-2", "permission_denied"),
        ("ACC-9", "not_linked"),
        ("", "refused"),
    ):
        for tool in ("read_account_activities", "read_proposed_lots"):
            answer = client.call_tool(tool, {"account": account})
            assert answer.outcome == "refused", (tool, account)
            assert answer.paths == ["account"]
            assert answer.json["reason"] == reason


def test_activities_snaptrade_cannot_read_are_refused_as_unavailable(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    answer = reader(sidecar).call_tool("read_account_activities", {"account": "ACC-4"})
    assert answer.outcome == "refused" and answer.json["reason"] == "unavailable"
    assert answer.json["detail"] == "reading activities failed: no answer"


# ── Lots proposed, never confirmed ──────────────────────────────────────────


def proposed(sidecar: Sidecar, account: str) -> dict[str, dict[str, Any]]:
    answer = reader(sidecar).call_tool("read_proposed_lots", {"account": account})
    assert answer.status == 200 and answer.outcome == "unchanged", answer.json
    return {each["instrument"]: each for each in answer.data["positions"]}


def test_lots_are_proposed_from_the_buys_that_account_for_a_position(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, syncer, _ = held
    answer = reader(sidecar).call_tool("read_proposed_lots", {"account": "ACC-3"})
    data = answer.data
    assert data["external_account_id"] == IBKR
    assert data["history_from"] == "2025-01-06"
    assert data["history_read"] == f"7 activities read, traded from 2025-01-06 to {TODAY}"
    # Cash has no lots, and is not listed.
    positions = {each["instrument"]: each for each in data["positions"]}
    assert set(positions) == {"SAP.DE", "SYNX", "SYNQ", "SYNB", "SYNV"}
    synq = positions["SYNQ"]
    assert synq["proposed_from"] == "activities"
    assert synq["average_purchase_price"] == "41.25"
    # One lot per buy: its units, the amount paid, its trade date; each naming
    # its source. The dividend between moves no shares.
    assert synq["proposed"] == [
        {
            "quantity": "20",
            "cost": "760.00",
            "currency": "USD",
            "acquired": days_ago(300),
            "source": f"SnapTrade activities, BUY on {days_ago(300)}, "
            "00000000-0000-4000-8000-00000000f003",
        },
        {
            "quantity": "10",
            "cost": "477.50",
            "currency": "USD",
            "acquired": days_ago(120),
            "source": f"SnapTrade activities, BUY on {days_ago(120)}, "
            "00000000-0000-4000-8000-00000000f006",
        },
    ]
    # SnapTrade's own lots are the statement's: nothing is proposed beside them.
    assert positions["SAP.DE"]["lots_reported"] == 2 and positions["SAP.DE"]["proposed"] == []
    # The whole history read is kept, and named.
    assert syncer.raw is not None and syncer.raw.find(data["raw_record"]) is not None


def test_a_position_arrived_by_transfer_gets_no_lot_from_history_and_says_so(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    synb = proposed(sidecar, "ACC-3")["SYNB"]
    assert synb["said"][0] == (
        "No lot from its history: it arrived or left by transfer (EXTERNAL_ASSET_TRANSFER_IN "
        f"on {days_ago(200)}, 00000000-0000-4000-8000-00000000f005), for which SnapTrade "
        "states no cost or acquisition date."
    )
    # Failing its history, its average purchase price: the quantity at that
    # price, the acquisition date left for the person.
    assert synb["proposed_from"] == "average purchase price"
    assert synb["average_purchase_price"] == "18.40"
    assert synb["proposed"] == [
        {
            "quantity": "100",
            "cost": "1840.00",
            "currency": "USD",
            "acquired": None,
            "source": AVERAGE,
        }
    ]


def test_a_position_sold_from_is_not_split_by_fifo(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    synv = proposed(sidecar, "ACC-3")["SYNV"]
    assert "no FIFO" in synv["said"][0] and "1 sale" in synv["said"][0]
    assert synv["proposed"] == [
        {
            "quantity": "30",
            "cost": "300.00",
            "currency": "USD",
            "acquired": None,
            "source": AVERAGE,
        }
    ]
    # With neither a purchase nor an average, nothing, and why.
    synx = proposed(sidecar, "ACC-3")["SYNX"]
    assert synx["proposed"] == [] and synx["proposed_from"] == ""
    assert synx["said"][-1].startswith("SnapTrade reports no average purchase price")


def test_no_lot_for_an_option_or_a_short_and_a_fund_from_its_average(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    alpaca = proposed(sidecar, "ACC-1")
    option = alpaca["AAPL  261218C00250000"]
    assert option["proposed"] == [] and option["said"][0].startswith("An option:")
    # ZZTOP is short, but SnapTrade lists its lot: the statement's.
    assert alpaca["ZZTOP"]["lots_reported"] == 1 and alpaca["ZZTOP"]["proposed"] == []
    assert alpaca["SYNXX"]["proposed"] == [
        {
            "quantity": "500.00",
            "cost": "500.0000",
            "currency": "USD",
            "acquired": None,
            "source": AVERAGE,
        }
    ]


def test_history_that_cannot_be_read_still_proposes_from_the_average(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    answer = reader(sidecar).call_tool("read_proposed_lots", {"account": "ACC-4"})
    assert answer.data["history_read"].startswith("not read whole: SnapTrade's activities")
    vti = {each["instrument"]: each for each in answer.data["positions"]}["VTI"]
    assert vti["said"][0] == (
        "No lot from its history: SnapTrade's activities could not be read: reading "
        "activities failed: no answer."
    )
    assert vti["proposed"] == [
        {
            "quantity": "15",
            "cost": "3600.00",
            "currency": "USD",
            "acquired": None,
            "source": AVERAGE,
        }
    ]


# ── The rules, one position at a time ───────────────────────────────────────


def position(
    quantity: str = "10",
    average: str | None = "12.00",
    *,
    side: Side = Side.LONG,
    kind: str = "stock",
    lots: tuple[Lot, ...] = (),
    unread: bool = False,
    assumed: bool = False,
) -> Holding:
    return Holding(
        identifiers=(Identifier("symbol", "XYZ", "snaptrade"),),
        description="XYZ Corp",
        kind=kind,
        side=side,
        quantity=Decimal(quantity),
        currency="USD",
        currency_assumed=assumed,
        average_cost=meridian.Money(Decimal(average), "USD") if average else None,
        lots=lots,
        lots_unread=unread,
    )


def act(
    kind: str, units: Any, amount: Any = None, day: str = "2026-01-05", n: int = 1
) -> history.Activity:
    return history.activity(
        {
            "id": f"A{n}",
            "type": kind,
            "symbol": {"symbol": "XYZ"},
            "units": units,
            "amount": amount,
            "currency": {"code": "USD"},
            "trade_date": f"{day}T00:00:00.000Z",
        }
    )


def test_a_reinvested_dividend_is_a_purchase_with_its_own_lot() -> None:
    made = history.propose(
        position("10"),
        [act("BUY", 8, Decimal("-96.00")), act("REI", Decimal("2"), Decimal("-25.00"), n=2)],
        "",
        "2025-01-01",
        NOW,
    )
    assert made.proposed_from == "activities"
    assert [(str(lot.quantity), str(lot.cost), lot.source) for lot in made.proposed] == [
        ("8", "96.00", "SnapTrade activities, BUY on 2026-01-05, A1"),
        ("2", "25.00", "SnapTrade activities, REI on 2026-01-05, A2"),
    ]


@pytest.mark.parametrize(
    ("activities", "why"),
    [
        ([act("BUY", 6, Decimal("-60"))], "come to 6, and it holds 10"),
        (
            [act("SPLIT", 5), act("BUY", 5, Decimal("-50"))],
            "a corporate action changed it (SPLIT",
        ),
        ([act("TRANSFER", 10)], "it arrived or left by transfer (TRANSFER"),
        ([act("BUY", 10, None)], "states no amount paid"),
        ([act("BUY", 10, Decimal("-100")), act("SELL", -2, Decimal("30"))], "no FIFO"),
        ([], "shows no purchase of XYZ"),
    ],
)
def test_history_that_does_not_state_the_lots_proposes_from_the_average(
    activities: list[history.Activity], why: str
) -> None:
    made = history.propose(position("10"), activities, "", "2025-01-01", NOW)
    assert why in made.said[0]
    assert made.proposed_from == "average purchase price"
    assert [(str(lot.cost), lot.acquired) for lot in made.proposed] == [("120.00", None)]


def test_nothing_proposed_beside_lots_reported_unread_or_for_a_short() -> None:
    reported = history.propose(
        position(lots=(Lot(Decimal("10")),)), [act("BUY", 10, Decimal("-1"))], "", "", NOW
    )
    assert reported.proposed == [] and reported.lots_reported == 1
    unread = history.propose(position(unread=True), [], "", "", NOW)
    assert unread.proposed == [] and "could not be read exactly" in unread.said[0]
    short = history.propose(position("-10", side=Side.SHORT), [], "", "", NOW)
    assert short.proposed == [] and short.said[0].startswith("A short position")


def test_an_average_in_a_currency_derived_says_so() -> None:
    made = history.propose(position(assumed=True), None, "SnapTrade is not being read", "", NOW)
    assert made.said[0] == "No lot from its history: SnapTrade is not being read."
    assert made.said[-1].startswith("SnapTrade stated no currency for it; USD is derived")


# ── The pages a person reads ────────────────────────────────────────────────


def test_the_history_tab_reads_a_range_and_offers_the_proposals(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    client = reader(sidecar)
    empty = client.get(HISTORY, "read")
    assert empty.status == 200 and "Choose an account" in empty.text
    assert "om-grid" not in empty.text
    shown = client.get(HISTORY, "read", account="ACC-3", start=days_ago(365), end=str(TODAY))
    assert shown.status == 200
    assert "<strong>2025-01-06</strong>, the first transaction SnapTrade holds" in shown.text
    assert 'id="activities"' in shown.text and "EXTERNAL_ASSET_TRANSFER_IN" in shown.text
    assert f"{LOTS}?account=ACC-3" in shown.text
    refused = client.get(HISTORY, "read", account="ACC-3", end="2099-01-01")
    assert refused.status == 200 and "The range ends after today." in refused.text
    lots = client.get(LOTS, "read", account="ACC-3")
    assert lots.status == 200
    assert f"SnapTrade activities, BUY on {days_ago(300)}" in lots.text
    assert AVERAGE in lots.text and "for the person to supply" in lots.text


def test_statements_show_and_answer_the_average_purchase_price(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    answer = reader(sidecar, "ACC-3").call_tool("read_statements")
    assert answer.outcome == "unchanged"
    (ibkr,) = answer.data["statements"]
    assert ibkr["account"] == "ACC-3" and ibkr["history_from"] == "2025-01-06"
    rows = {row["instrument"]: row for row in ibkr["rows"]}
    assert rows["SYNQ"]["average_purchase_price"] == "41.25" and rows["SYNQ"]["lots"] == []
    assert rows["SAP.DE"]["lots"][0] == {
        "quantity": "20",
        "cost": "3500.00",
        "acquired": "2023-05-15",
    }
    page = reader(sidecar, "ACC-3").get(STATEMENTS, "read").text
    assert "Average price" in page and "41.25" in page


# ── Under Manage, as tools ──────────────────────────────────────────────────


def test_manage_reads_connections_and_links_and_no_accounts_data(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, _ = held
    admin = PageClient(pages, sidecar.plugin())
    connections = admin.call_tool("read_connections")
    assert connections.level == "admin" and connections.outcome == "unchanged"
    serving = {c["institution"]: c["serving"] for c in connections.data["connections"]}
    assert serving == {
        "Alpaca": "real_time",
        "Interactive Brokers": "delayed",
        "Schwab": "real_time",
    }
    links = admin.call_tool("read_account_links").data["accounts"]
    assert {(each["external_account_id"], each["account_id"]) for each in links} == {
        (ALPACA, "ACC-1"),
        (IBKR, "ACC-3"),
        (SCHWAB, "ACC-4"),
    }
    # Identities and links only: no holding, quantity or cash of an account.
    said = json.dumps([connections.json, links])
    assert "AAPL" not in said and "SYNQ" not in said and "1023.45" not in said


def test_manage_acts_through_tools_refused_by_path(
    held: tuple[Sidecar, Syncer, asyncio.Event],
) -> None:
    sidecar, _, woken = held
    admin = PageClient(pages, sidecar.plugin())
    real_time = admin.call_tool("refresh_connection", {"connection_id": synthetic.ALPACA})
    assert real_time.outcome == "unchanged" and real_time.data["said"] == REAL_TIME
    delayed = admin.call_tool("refresh_connection", {"connection_id": synthetic.IBKR})
    assert delayed.outcome == "made" and delayed.data["connection_id"] == synthetic.IBKR
    for tool in ("refresh_connection", "reconnect_connection"):
        unknown = admin.call_tool(tool, {"connection_id": "nope"})
        assert unknown.outcome == "refused" and unknown.paths == ["connection_id"]
    portal = admin.call_tool("open_connection_portal")
    assert portal.outcome == "made" and portal.data == {"link": "", "said": NO_PORTAL}
    unlinked = admin.call_tool(
        "link_account", {"intent": "unlink", "external_account_id": IBKR}
    )
    assert unlinked.outcome == "made", unlinked.json
    assert (
        unlinked.data["account_id"] == ""
        and unlinked.data["said"] == "Unlinked IBKR Individual."
    )
    assert [p.external_account_id for p in sidecar.sent("LinkExternalAccount")] == [IBKR]
    for arguments, paths, reason in (
        ({"intent": "link", "external_account_id": IBKR}, ["account_id"], "refused"),
        (
            {"intent": "link", "external_account_id": "nope", "account_id": "ACC-3"},
            ["external_account_id"],
            "not_found",
        ),
        (
            {"intent": "create", "external_account_id": IBKR, "new_account_name": "New"},
            ["intent"],
            "permission_denied",
        ),
        ({"intent": "merge", "external_account_id": IBKR}, ["intent"], "invalid_arguments"),
    ):
        refused = admin.call_tool("link_account", arguments)
        assert refused.outcome == "refused", arguments
        assert refused.paths == paths and refused.json["reason"] == reason
    woken.clear()
    writer = PageClient(pages, sidecar.plugin(), write={"ACC-3"})
    started = writer.call_tool("read_snaptrade_now")
    assert started.level == "write" and started.outcome == "made"
    assert started.data == {"said": READING} and woken.is_set()
