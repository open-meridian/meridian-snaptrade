"""A custodian's deposit sent as cash (the product owner, 2026-10-05;
meridian-design tasks/sdk-contract/snaptrade-counts-a-core-deposit-as-cash).

Fidelity's Traditional IRA holds its core position as an FDIC-insured bank
deposit, FDIC99532, which SnapTrade reports as a position of kind `other`,
2.99 at a price of 1, beside USD cash of 0.00. SnapTrade's own flag decides
first: a position it marks a cash equivalent that is no fund is cash it
already counts in its cash, so the cash row stands for it. Where SnapTrade
does not mark it, an admin of the plugin lists it in the table setting
`counted_as_cash`, and it is added to the cash of the row's currency, naming
who listed it and when. Never from what a symbol looks like; a fund stays a
fund.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import meridian
import pytest
from meridian.plugin.v1 import operations_pb2 as ops
from meridian.v1 import sidecar_pb2

from snaptrade.contract import Recorder
from snaptrade.counted_as_cash import CountedAsCash, row_for, rows_of
from snaptrade.normalise import (
    POSITIONS_CALL,
    RULE_DEPOSIT_FLAGGED,
    RULE_DEPOSIT_LISTED,
    RULE_NET_CASH,
    Withheld,
    external_account,
    freshness,
    ns,
    statement,
    views,
)
from snaptrade.page import _holding_row
from snaptrade.settings import (
    COUNTED_AS_CASH,
    DECLARED,
    PLAN_CODE_LINKS,
    SYNTHETIC,
    config_from,
)
from snaptrade.sync import Syncer
from snaptrade.synthetic import SyntheticVenue
from snaptrade.venue import read
from test_normalise import STALE_AFTER, account, balance, connection, stock

from conftest import NOW, Sidecar, clock

IRA = "ALPACA:INST-1"
FDIC = "FDIC INSURED DEPOSIT CITIZENS BK NA IRA NOT COVERD BY SIPC"
WHO = "local|ada"
WHEN = "2026-10-05T12:00:00.000000Z"
ROW = {"symbol": "FDIC99532", "currency": "USD", "account": IRA,
       "changed_by": WHO, "changed_at": WHEN}  # fmt: skip
LISTED = CountedAsCash("FDIC99532", "USD", IRA, changed_by=WHO, changed_at=WHEN)


def deposit(flagged: bool | None = None, **changes: Any) -> dict[str, Any]:
    """FDIC99532 as SnapTrade reported it in the IRA: kind `other`, 2.99 at 1."""
    said: dict[str, Any] = {"price": "1", "cost_basis": "1", "currency": "USD"}
    if flagged is not None:
        said["cash_equivalent"] = flagged
    instrument = {"kind": "other", "description": FDIC, "exchange": ""}
    instrument.update(changes.pop("instrument", {}))
    return stock("FDIC99532", "2.99", instrument=instrument, **{**said, **changes})


def made(
    positions: list[dict[str, Any]],
    balances: list[dict[str, Any]],
    listed: tuple[CountedAsCash, ...] = (),
) -> Any:
    acct = external_account(account(), connection())
    fresh = freshness(account(), connection(), NOW, STALE_AFTER)
    return statement(
        acct,
        {"results": positions, "data_freshness": {"as_of": "2026-09-28T14:55:00Z"}},
        balances,
        NOW,
        fresh,
        None,
        [],
        listed,
    )


def symbols(result: Any) -> list[str]:
    return [row.identifiers[-1].value for row in result.holdings]


def usd(result: Any) -> Any:
    return next(row for row in result.holdings if row.kind == "cash" and row.currency == "USD")


# ── The setting ──────────────────────────────────────────────────────────────


def test_the_setting_is_a_table_of_an_account_or_none_a_symbol_and_a_currency() -> None:
    (declared,) = [s for s in DECLARED if s.name == COUNTED_AS_CASH]
    sent = declared._declared()
    assert sent.type == sidecar_pb2.SETTING_TYPE_TABLE
    assert sent.label == "Cash links", "the tab under Manage is titled by the label"
    assert [(c.name, c.type, c.required) for c in sent.columns] == [
        ("account", sidecar_pb2.SETTING_COLUMN_TYPE_EXTERNAL_ACCOUNT, False),
        ("symbol", sidecar_pb2.SETTING_COLUMN_TYPE_TEXT, True),
        ("currency", sidecar_pb2.SETTING_COLUMN_TYPE_TEXT, True),
    ]
    assert "every account" in sent.columns[0].description, "a blank account is every one"
    assert sent.most_rows == 200 and not sent.secret


def test_cash_links_and_plan_code_links_follow_one_column_order() -> None:
    """Account | Plan code / Symbol | Instrument / Currency (the product
    owner, 2026-10-05)."""
    tables = {s.name: s._declared() for s in DECLARED}
    plan_codes, cash = tables[PLAN_CODE_LINKS], tables[COUNTED_AS_CASH]
    assert [c.label for c in plan_codes.columns] == ["Account", "Plan code", "Instrument"]
    assert [c.label for c in cash.columns] == ["Account", "Symbol", "Currency"]
    assert (plan_codes.label, cash.label) == ("Plan-code links", "Cash links")


def test_a_row_saved_in_the_old_column_order_reads_the_same() -> None:
    """0.11.0 declared symbol, currency, account; a row is keyed by column
    name, so one saved then reads as it did."""
    old = {"symbol": "FDIC99532", "currency": "USD", "account": IRA,
           "changed_by": WHO, "changed_at": WHEN}  # fmt: skip
    new = {"account": IRA, "symbol": "FDIC99532", "currency": "USD",
           "changed_by": WHO, "changed_at": WHEN}  # fmt: skip
    assert list(old)[:3] == ["symbol", "currency", "account"]
    assert rows_of([old]) == rows_of([new]) == (LISTED,)
    assert config_from({COUNTED_AS_CASH: [old]}).counted_as_cash == (LISTED,)


def test_the_rows_delivered_are_read_with_who_and_when() -> None:
    every = {"symbol": " SYNFD ", "currency": "usd", "account": ""}
    bad = {"symbol": "SYNFD", "currency": "dollars"}
    rows = rows_of([ROW, every, bad, {"symbol": "", "currency": "USD"}, "x"])
    assert rows == (
        LISTED,
        CountedAsCash("SYNFD", "USD"),
        CountedAsCash("SYNFD", "", given="dollars"),
    ), "a currency read in capitals; one that is no code kept to say so; no symbol, no row"
    assert LISTED.person == f"{WHO}, {WHEN}"
    assert rows_of("not rows") == ()
    assert config_from({COUNTED_AS_CASH: [ROW]}).counted_as_cash == (LISTED,)


def test_a_row_naming_the_account_comes_before_one_naming_none() -> None:
    anywhere = CountedAsCash("FDIC99532", "USD", changed_by="local|bo")
    assert row_for((anywhere, LISTED), IRA, "FDIC99532") == LISTED
    assert row_for((anywhere, LISTED), "OTHER:1", "FDIC99532") == anywhere
    assert row_for((LISTED,), "OTHER:1", "FDIC99532") is None
    assert row_for((LISTED,), IRA, "FDIC99533") is None


# ── SnapTrade's own flag first ───────────────────────────────────────────────


def test_a_deposit_snaptrade_marks_a_cash_equivalent_is_the_cash_it_counts_it_in() -> None:
    """SnapTrade's cash counts what it marks a cash equivalent ("also counted
    in account cash balance"): the cash row stands for the deposit, never
    adding it twice, and names it by the rule with its description."""
    result, problems = made([deposit(flagged=True)], [balance("USD", Decimal("10.00"))])
    assert problems == ()
    assert symbols(result) == ["USD"], "no FDIC99532 row"
    cash = usd(result)
    assert cash.quantity == Decimal("10.00")
    assert cash.market_value == meridian.Money(Decimal("10.00"), "USD")
    rule = f'{RULE_DEPOSIT_FLAGGED}: FDIC99532, as reported "{FDIC}"'
    assert [(c.field, c.kind, c.rule) for c in cash.closed][:2] == [
        ("quantity", "derived", rule),
        ("market_value", "derived", rule),
    ]
    (counted,) = result.as_cash
    assert (counted.symbol, counted.by, counted.amount) == (
        "FDIC99532",
        "flag",
        Decimal("2.99"),
    )
    assert result.netted == ()


def test_a_marked_deposit_worth_more_than_its_cash_or_with_none_withholds() -> None:
    with pytest.raises(Withheld, match="worth more than that cash"):
        made([deposit(flagged=True)], [balance("USD", Decimal("0.00"))])
    with pytest.raises(Withheld, match="reports no USD cash"):
        made([deposit(flagged=True)], [])
    with pytest.raises(Withheld, match="no price for FDIC99532"):
        made([deposit(flagged=True, price=None)], [balance("USD", Decimal("10.00"))])


def test_a_fund_snaptrade_marks_stays_a_fund_and_the_cash_net_of_it() -> None:
    fund = stock(
        "SYNXX", "500.00", price="1.00", cash_equivalent=True, instrument={"kind": "mutualfund"}
    )
    result, _ = made([fund, deposit(flagged=True)], [balance("USD", Decimal("1523.45"))])
    assert symbols(result) == ["SYNXX", "USD"]
    cash = usd(result)
    # 1523.45 counts the fund's 500.00, net of it, and the deposit's 2.99,
    # which is cash: 1023.45.
    assert cash.quantity == Decimal("1023.45")
    assert [c.rule for c in cash.closed][:2] == [RULE_NET_CASH, RULE_NET_CASH]
    assert result.netted[0].net == Decimal("1023.45")


def test_the_flag_comes_before_the_setting() -> None:
    result, _ = made([deposit(flagged=True)], [balance("USD", Decimal("10.00"))], (LISTED,))
    assert usd(result).quantity == Decimal("10.00"), "never added as well"
    assert [d.by for d in result.as_cash] == ["flag"]


# ── The admin's setting as the fallback ──────────────────────────────────────


def test_the_iras_deposit_listed_is_sent_as_its_cash_naming_who_listed_it() -> None:
    """The IRA as SnapTrade read it, 2026-10-05: FDIC99532 2.99 unmarked,
    beside USD cash of 0.00; listed, USD cash 2.99 and no FDIC99532."""
    result, problems = made([deposit()], [balance("USD", Decimal("0.00"))], (LISTED,))
    assert problems == ()
    assert symbols(result) == ["USD"]
    cash = usd(result)
    assert cash.quantity == Decimal("2.99") and str(cash.quantity) == "2.99"
    assert cash.market_value == meridian.Money(Decimal("2.99"), "USD")
    rule = f'{RULE_DEPOSIT_LISTED}: FDIC99532, as reported "{FDIC}"'
    assert [(c.field, c.kind, c.rule, c.person) for c in cash.closed][:4] == [
        ("quantity", "derived", rule, ""),
        ("quantity", "supplied", "", f"{WHO}, {WHEN}"),
        ("market_value", "derived", rule, ""),
        ("market_value", "supplied", "", f"{WHO}, {WHEN}"),
    ]
    (counted,) = result.as_cash
    assert (counted.by, counted.person) == ("setting", f"{WHO}, {WHEN}")


def test_unlisted_it_is_the_holding_snaptrade_reported() -> None:
    result, _ = made([deposit(flagged=False)], [balance("USD", Decimal("0.00"))])
    assert symbols(result) == ["FDIC99532", "USD"]
    assert usd(result).quantity == Decimal("0.00") and result.as_cash == ()


def test_listed_where_snaptrade_reports_no_cash_it_is_a_cash_row_of_its_own() -> None:
    result, _ = made([deposit()], [], (LISTED,))
    (cash,) = result.holdings
    assert (cash.kind, cash.currency, cash.quantity) == ("cash", "USD", Decimal("2.99"))
    assert cash.call == POSITIONS_CALL, "converted from the positions it was listed in"


def test_a_row_for_every_account_counts_and_one_for_another_does_not() -> None:
    anywhere = CountedAsCash("FDIC99532", "USD", changed_by=WHO)
    result, _ = made([deposit()], [balance("USD", Decimal("0.00"))], (anywhere,))
    assert usd(result).quantity == Decimal("2.99")
    elsewhere = CountedAsCash("FDIC99532", "USD", "FIDELITY:OTHER")
    result, _ = made([deposit()], [balance("USD", Decimal("0.00"))], (elsewhere,))
    assert symbols(result) == ["FDIC99532", "USD"]


def test_a_listed_position_that_cannot_be_counted_is_kept_and_said() -> None:
    fund = stock("SYNXX", "5", price="1.00", instrument={"kind": "mutualfund"})
    cases = [
        (fund, CountedAsCash("SYNXX", "USD"), "a fund stays a fund"),
        (deposit(), CountedAsCash("FDIC99532", "", given="dollars"), "'dollars', which is no"),
        (deposit(), CountedAsCash("FDIC99532", "EUR"), "SnapTrade states it in USD"),
        (deposit(price=None), LISTED, "no price for it"),
    ]
    for position, row, said in cases:
        result, problems = made([position], [balance("USD", Decimal("0.00"))], (row,))
        assert len(result.holdings) == 2 and result.as_cash == (), said
        (problem,) = problems
        assert said in problem


def test_a_listed_deposit_with_no_currency_stated_is_in_the_rows() -> None:
    unstated = deposit(currency=None, instrument={"currency": None})
    result, _ = made([unstated], [], (CountedAsCash("FDIC99532", "EUR"),))
    (cash,) = result.holdings
    assert (cash.currency, cash.quantity) == ("EUR", Decimal("2.99"))


# ── What reaches the sidecar, and the page ───────────────────────────────────


async def test_the_cash_is_recorded_with_the_rule_and_who_listed_it() -> None:
    result, _ = made([deposit()], [balance("USD", Decimal("0.00"))], (LISTED,))
    sidecar = Sidecar()
    acct = external_account(account(), connection())
    await Recorder(sidecar.plugin()).record(acct, result, ns(NOW))
    (row,) = sidecar.sent("RecordHolding")
    assert meridian.as_decimal(row.quantity) == Decimal("2.99")
    said = [
        (p.field, ops.ProvenanceKind.Name(p.kind), p.rule, p.person) for p in row.provenance
    ]
    rule = f'{RULE_DEPOSIT_LISTED}: FDIC99532, as reported "{FDIC}"'
    assert ("quantity", "PROVENANCE_KIND_DERIVED", rule, "") in said
    assert ("quantity", "PROVENANCE_KIND_SUPPLIED", "", f"{WHO}, {WHEN}") in said
    assert ("market_value", "PROVENANCE_KIND_SUPPLIED", "", f"{WHO}, {WHEN}") in said
    (resolved,) = sidecar.sent("ResolveIdentifier")
    assert [(i.scheme, i.value) for i in resolved.identifiers] == [("iso4217", "USD")]


def test_a_long_description_is_cut_to_the_rules_bound() -> None:
    long = deposit(instrument={"kind": "other", "description": "D" * 400})
    result, _ = made([long], [balance("USD", Decimal("0.00"))], (LISTED,))
    rules = [c.rule for c in usd(result).closed if c.rule.startswith(RULE_DEPOSIT_LISTED)]
    assert len(rules) == 2 and all(len(r) == 200 and r.endswith('..."') for r in rules)


async def test_the_synthetic_ira_marked_then_listed() -> None:
    snapshot = await read(SyntheticVenue(clock()), clock())
    fidelity = "FIDELITY:SYN-FID-4004"

    def ira(listed: tuple[CountedAsCash, ...] = ()) -> Any:
        return next(
            view
            for each in views(snapshot, STALE_AFTER, listed)
            for view in each.accounts
            if view.account.external_account_id == fidelity
        )

    before = ira()
    assert before.account.kind == "retirement" and before.statement is not None
    assert symbols(before.statement) == ["SYNFD", "SYNI", "USD"], "SYNDP is the cash"
    assert usd(before.statement).quantity == Decimal("10.00")
    after = ira((CountedAsCash("SYNFD", "USD", fidelity, changed_by=WHO, changed_at=WHEN),))
    assert after.statement is not None
    assert symbols(after.statement) == ["SYNI", "USD"]
    assert usd(after.statement).quantity == Decimal("12.99")
    notes = _holding_row(after, usd(after.statement))["notes"]
    assert "SYNDP" in notes and "marks a cash equivalent" in notes
    assert "SYNFD" in notes and f"listed as cash in the plugin's settings by {WHO}" in notes


def test_a_read_counts_what_the_settings_list(tmp_path: Any) -> None:
    sidecar = Sidecar()
    syncer = Syncer(sidecar.plugin(), now=clock())
    row = {**ROW, "symbol": "SYNFD", "account": "FIDELITY:SYN-FID-4004"}
    syncer.configure(config_from({SYNTHETIC: True, COUNTED_AS_CASH: [row]}))
    asyncio.run(syncer.run_once())
    outcome = syncer.status.outcomes["FIDELITY:SYN-FID-4004"]
    assert outcome.rows == 2 and outcome.recorded == 2
    supplied = [
        p.person
        for held in sidecar.sent("RecordHolding")
        for p in held.provenance
        if p.kind == ops.PROVENANCE_KIND_SUPPLIED
    ]
    assert supplied == [f"{WHO}, {WHEN}"] * 2
