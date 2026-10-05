"""What reaches the sidecar, through the SDK's typed operations.

Each part of the account-side contract (spec/the-account-side-fits-every-venue)
as the pinned SDK takes it (contract v11): a holding's side, its settled
quantity and its pending quantities by value date, a market value left unset
where none was reported, each asset once -- the cash of a currency net of a
money market fund SnapTrade counts in it, and with the deposits sent as cash
-- its average cost and lots as
reported, the raw record it was converted from, and the provenance of each
value this plugin closed rather than read; a statement naming its external
account and institution, with its figures as the one set for the account as a
whole, and its raw record; the sync state with holdings and history
freshness, and a state closed by a rule saying so; and the accounts a
connection reaches with their kinds, SnapTrade's type as reported where it
says none (W2.8). A resolve states what SnapTrade says of the security -- its
asset class, its type where it is a money market fund, its currency where
stated, its description -- and is narrowed only by a currency SnapTrade
stated. And each of SnapTrade's activities on a linked account, as the
custodian states it (contract v14, W2.10, activities.py), its instrument
resolved as a holding's is, or by a person's plan-code link with that
person's name, or the code as reported; and the sync status carries the
first date SnapTrade's history of the account reaches (`history_from`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import meridian
from meridian.edge import as_reported, derived, reported, supplied

from .activities import SYMBOL_SCHEME, Converted, PlanCodeLink, link_for
from .normalise import (
    ACCOUNT_TYPE_SCHEME,
    SECURITY_TYPE_SCHEME,
    SOURCE,
    Closed,
    ExternalAccount,
    Freshness,
    Holding,
    Side,
    Statement,
    SyncState,
    day_ns,
    ns,
)
from .raw import record_key

log = logging.getLogger("snaptrade")

_SYNC_STATE: dict[SyncState, meridian.SyncState] = {
    SyncState.CURRENT: meridian.SyncState.SYNC_STATE_CURRENT,
    SyncState.STALE: meridian.SyncState.SYNC_STATE_STALE,
    SyncState.NEEDS_SIGN_IN: meridian.SyncState.SYNC_STATE_NEEDS_SIGN_IN,
    SyncState.DISABLED: meridian.SyncState.SYNC_STATE_DISABLED,
    SyncState.DELAYED_BY_DESIGN: meridian.SyncState.SYNC_STATE_DELAYED_BY_DESIGN,
    SyncState.HOLDINGS_UNAVAILABLE: meridian.SyncState.SYNC_STATE_HOLDINGS_UNAVAILABLE,
}
_SIDE: dict[Side, meridian.HoldingSide] = {
    Side.LONG: meridian.HoldingSide.HOLDING_SIDE_LONG,
    Side.SHORT: meridian.HoldingSide.HOLDING_SIDE_SHORT,
}
_KIND: dict[str, meridian.AccountKind] = {
    "cash": meridian.AccountKind.ACCOUNT_KIND_CASH,
    "margin": meridian.AccountKind.ACCOUNT_KIND_MARGIN,
    "retirement": meridian.AccountKind.ACCOUNT_KIND_RETIREMENT,
}


def refused_unlinked(refused: Exception) -> bool:
    """Whether a statement, or a row, was refused because its external account
    is not linked (W4.8): by the refusal's code, which the SDK raises as
    `NotLinked`, never by its words."""
    return isinstance(refused, meridian.NotLinked)


@dataclass
class Outcome:
    """What recording one account's statement came to."""

    statement_id: str = ""
    rows: int = 0
    recorded: int = 0
    # Records the deployment minted for identifiers nothing matched (W3.7).
    minted: int = 0
    ambiguous: int = 0
    already_recorded: bool = False
    # Why it stopped before every row was recorded, when it did.
    stopped: str = ""
    # Stopped because the account is not linked to one of the deployment's.
    unlinked: bool = False


@dataclass
class ActivityOutcome:
    """What reporting one account's activities came to, in one read."""

    # Sent to the street, and of them those it recorded for the first time.
    sent: int = 0
    recorded: int = 0
    # Not sent, each with why: SnapTrade gave too little, or inexactly.
    skipped: list[str] = field(default_factory=list)
    # Why it stopped before every activity was sent, when it did.
    stopped: str = ""
    unlinked: bool = False
    # Whether this was the account's backfill; whether it reached the end, or
    # stopped at the most it reads at once.
    backfill: bool = False
    complete: bool = False
    capped: bool = False


def _identifiers(holding: Holding) -> list[meridian.Identifier]:
    return [
        meridian.Identifier(scheme=held.scheme, value=held.value, source=held.source)
        for held in holding.identifiers
    ]


class Recorder:
    """Sends what the normaliser made through the SDK's typed operations."""

    def __init__(self, plugin: meridian.Plugin) -> None:
        self._plugin = plugin

    async def report_accounts(self, accounts: Sequence[ExternalAccount]) -> None:
        """W2.8: the accounts the connection reaches, each with its kind,
        converted from SnapTrade's type; where the type says none, the kind
        not known and the type beside it as reported (contract v11, Q13)."""
        await self._plugin.report_external_accounts(
            accounts=[
                meridian.ExternalAccount(
                    external_account_id=account.external_account_id,
                    name=account.name,
                    account_kind=_KIND.get(
                        account.kind, meridian.AccountKind.ACCOUNT_KIND_UNSPECIFIED
                    ),
                    account_kind_as_reported=(
                        None
                        if account.kind_as_reported is None
                        else as_reported(ACCOUNT_TYPE_SCHEME, *account.kind_as_reported)
                    ),
                )
                for account in accounts
            ],
        )

    async def report_sync(
        self,
        account: ExternalAccount,
        fresh: Freshness,
        observed_at_ns: int,
        history_from: str = "",
    ) -> None:
        """W2.1: how fresh the account's data is, and why when it is not: when
        SnapTrade last synced it, apart from when what it served is as of, and
        a state this plugin closed with the rule it was derived by, in the
        detail, since the event carries no provenance."""
        holdings_as_of_ns = ns(fresh.holdings_as_of) if fresh.holdings_as_of else 0
        await self._plugin.report_sync_status(
            # The first date SnapTrade's history of the account reaches, as
            # its sync status says (contract v14): "" where it says none.
            history_from=history_from,
            source=SOURCE,
            last_synced_at_ns=ns(fresh.last_synced) if fresh.last_synced else 0,
            connection_healthy=fresh.healthy,
            status_detail=fresh.status_detail,
            observed_at_ns=observed_at_ns,
            external_account_id=account.external_account_id,
            state=_SYNC_STATE[fresh.state],
            holdings_as_of_ns=holdings_as_of_ns,
            history_as_of_ns=day_ns(fresh.history_as_of) if fresh.history_as_of else 0,
        )

    async def _resolve(
        self, holding: Holding, as_of_ns: int, observed_at_ns: int, outcome: Outcome
    ) -> dict[str, Any]:
        """W3.1, and W3.2 for an ambiguous miss: the row's instrument, or the
        identifiers it could not be told apart by."""
        identifiers = _identifiers(holding)
        stated_currency = "" if holding.currency_assumed else holding.currency
        answer = await self._plugin.resolve_identifier(
            identifiers=identifiers,
            as_of_ns=as_of_ns,
            exchange_mic=holding.exchange_mic,
            # Narrowed only by a currency SnapTrade stated (contract v11).
            currency=stated_currency,
            # What SnapTrade says of the security, kept as offers for the
            # deployment admin to accept (W3.1): its class, a money market
            # fund's type where SnapTrade counts it as a cash equivalent, its
            # currency where stated, its description.
            stated_asset_class=holding.asset_class or None,
            stated_instrument_type=(
                "money_market_fund"
                if holding.cash_equivalent and holding.asset_class == "fund"
                else None
            ),
            stated_currency=stated_currency,
            stated_description=holding.description if holding.kind != "cash" else "",
        )
        if answer.instrument_id:
            # Found, or nothing matched and the deployment minted its record
            # for the identifiers (W3.7).
            outcome.minted += 1 if answer.minted else 0
            return {"instrument_id": answer.instrument_id}
        # More than one matched. A fact, published once, and the row is still
        # recorded; nothing is retried or guessed.
        outcome.ambiguous += 1
        await self._plugin.report_missing_instrument(
            source=SOURCE,
            # The class normalise.py maps SnapTrade's kind to, by its ruled
            # name; unset where it maps none, for a person to set, with
            # SnapTrade's kind beside it as reported (contract v11).
            asset_class=holding.asset_class,
            asset_class_as_reported=(
                as_reported(SECURITY_TYPE_SCHEME, holding.kind)
                if holding.kind and not holding.asset_class and holding.kind != "cash"
                else None
            ),
            identifiers=identifiers,
            as_of_ns=as_of_ns,
            reason=answer.miss_reason,
            observed_at_ns=observed_at_ns,
        )
        return {"unresolved_identifiers": identifiers}

    @staticmethod
    def _figures(statement: Statement) -> list[meridian.StatementFigures]:
        """The statement's figures: one set, with no segment, the account's as
        a whole, where SnapTrade reported any; none otherwise. SnapTrade
        names no margin segment, and a currency is not one: buying power
        given in several currencies is not summed, and none is sent."""
        buying_power = statement.buying_power[0] if len(statement.buying_power) == 1 else None
        if buying_power is None and statement.net_liquidation is None:
            return []
        return [
            meridian.StatementFigures(
                segment="",
                buying_power=buying_power,
                net_liquidation=statement.net_liquidation,
            )
        ]

    def _provenance(
        self, closed: Sequence[Closed], account: ExternalAccount, read_at_ns: int
    ) -> list[meridian.Provenance]:
        """Each value this plugin closed, as its provenance: derived by the
        rule it names, reported by SnapTrade in another of its raw records
        (the account's activities), or supplied by the person named (a
        position they listed as cash, in the plugin's settings)."""
        made: list[meridian.Provenance] = []
        for held in closed:
            if held.kind == "supplied":
                made.append(supplied(held.field, held.person))
            elif held.kind == "reported":
                made.append(
                    reported(
                        held.field,
                        self._plugin.raw_record(record_key(account, read_at_ns, held.call)),
                    )
                )
            else:
                made.append(derived(held.field, held.rule))
        return made

    def _row(
        self, holding: Holding, account: ExternalAccount, read_at_ns: int
    ) -> dict[str, Any]:
        return {
            "quantity": holding.quantity,
            "side": _SIDE[holding.side],
            # Unset where SnapTrade reported none; never made from a price.
            "market_value": holding.market_value,
            # The settled quantity and the pending by value date, closed from
            # SnapTrade's activities or by a rule, each with its provenance.
            "settle_date_quantity": holding.settle_date_quantity,
            "pending": [
                meridian.ReportedPending(value_date=held.value_date, quantity=held.quantity)
                for held in holding.pending
            ],
            # The raw record the row came from, in this plugin's storage.
            "raw_record": self._plugin.raw_record(
                record_key(account, read_at_ns, holding.call)
            ),
            "provenance": self._provenance(holding.closed, account, read_at_ns),
            # SnapTrade's average per unit, as reported; its total cost basis
            # is none it reports, so `cost_basis` is never sent.
            "average_cost": holding.average_cost,
            "lots": [
                meridian.ReportedLot(
                    quantity=lot.quantity, cost=lot.cost, acquired_date=lot.acquired_date
                )
                for lot in holding.lots
            ],
        }

    async def _activity_instrument(
        self,
        converted: Converted,
        account: ExternalAccount,
        holdings: Sequence[Holding],
        links: Sequence[PlanCodeLink],
        as_of_ns: int,
        resolved: dict[str, str],
    ) -> dict[str, Any]:
        """W2.10's instrument: a person's plan-code link, the instrument record
        they linked the code to, naming who and when; a security the account
        holds, by that holding's identifiers; else the code as SnapTrade
        names it. `resolved` holds this read's answers by symbol."""
        code = converted.code
        if not code:
            return {}
        link = link_for(links, account.external_account_id, code)
        if link is not None:
            # The record the person chose in the plugin's settings, by its ID:
            # nothing is resolved by symbol, and nothing is minted.
            return {
                "instrument_id": link.instrument_id,
                "provenance": [supplied("instrument_id", link.person)],
            }
        holding = next(
            (
                held
                for held in holdings
                if any(i.scheme == "symbol" and i.value == code for i in held.identifiers)
            ),
            None,
        )
        if holding is None:
            return {
                "instrument_as_reported": as_reported(SYMBOL_SCHEME, code, converted.code_text)
            }
        if code not in resolved:
            answer = await self._plugin.resolve_identifier(
                identifiers=_identifiers(holding),
                as_of_ns=as_of_ns,
                exchange_mic=holding.exchange_mic,
            )
            resolved[code] = answer.instrument_id
        if not resolved[code]:
            # More than one record matched: as a holding's, not guessed.
            return {
                "instrument_as_reported": as_reported(SYMBOL_SCHEME, code, converted.code_text)
            }
        return {"instrument_id": resolved[code]}

    async def record_activities(
        self,
        account: ExternalAccount,
        activities: Sequence[tuple[Converted, str]],
        holdings: Sequence[Holding],
        links: Sequence[PlanCodeLink],
        outcome: ActivityOutcome,
    ) -> None:
        """W2.10: each activity, with the raw record key it was kept under,
        sent to the street, one per call. A redelivery is answered already
        recorded. The account not linked stops them all; any other refusal is
        the one activity's, said, and the rest go on."""
        resolved: dict[str, str] = {}
        for converted, key in activities:
            as_of_ns = day_ns(date.fromisoformat(converted.trade_date))
            try:
                instrument = await self._activity_instrument(
                    converted, account, holdings, links, as_of_ns, resolved
                )
                answer = await self._plugin.record_activity(
                    external_account_id=account.external_account_id,
                    source=SOURCE,
                    activity=meridian.CustodialActivity(
                        external_activity_id=converted.external_activity_id,
                        kind=converted.kind,
                        kind_as_reported=converted.kind_as_reported,
                        trade_date=converted.trade_date,
                        settlement_date=converted.settlement_date,
                        units=converted.units,
                        price=converted.price,
                        amount=converted.amount,
                        description=converted.description,
                        raw_record=self._plugin.raw_record(key),
                        **instrument,
                    ),
                )
            except (meridian.MeridianError, ValueError) as refused:
                if refused_unlinked(refused) or isinstance(refused, meridian.NoSidecar):
                    outcome.stopped = str(refused)
                    outcome.unlinked = refused_unlinked(refused)
                    log.info(
                        "activities of %s stopped: %s", account.external_account_id, refused
                    )
                    return
                said = f"activity {converted.external_activity_id} was refused: {refused}"
                outcome.skipped.append(said)
                log.warning("%s: %s", account.external_account_id, said)
                continue
            outcome.sent += 1
            outcome.recorded += 0 if answer.already_recorded else 1

    async def record(
        self, account: ExternalAccount, statement: Statement, observed_at_ns: int
    ) -> Outcome:
        """W2.2 to W2.4: resolve every row, open the statement with its count,
        and record each row. A refusal stops the statement and is said; it is
        not retried, since every refusal is a statement about configuration."""
        outcome = Outcome(rows=len(statement.holdings))
        as_of_ns = day_ns(date.fromisoformat(statement.as_of_date))
        try:
            instruments = [
                await self._resolve(holding, as_of_ns, observed_at_ns, outcome)
                for holding in statement.holdings
            ]
            # A statement names its account, and one nothing links is refused
            # here, before any row (v7).
            opened = await self._plugin.record_holdings_statement(
                source=SOURCE,
                external_statement_id=statement.external_statement_id,
                external_account_id=account.external_account_id,
                institution=account.institution,
                as_of_date=statement.as_of_date,
                read_at_ns=statement.read_at_ns,
                expected_rows=len(statement.holdings),
                figures=self._figures(statement),
                raw_record=self._plugin.raw_record(
                    record_key(account, statement.read_at_ns, "balances")
                ),
                provenance=self._provenance(statement.closed, account, statement.read_at_ns),
            )
            outcome.statement_id = opened.statement_id
            if opened.already_recorded:
                outcome.already_recorded = True
                return outcome
            for holding, instrument in zip(statement.holdings, instruments, strict=True):
                await self._plugin.record_holding(
                    statement_id=opened.statement_id,
                    external_account_id=account.external_account_id,
                    **instrument,
                    **self._row(holding, account, statement.read_at_ns),
                )
                outcome.recorded += 1
        except meridian.MeridianError as refused:
            outcome.stopped = str(refused)
            outcome.unlinked = refused_unlinked(refused)
            log.warning(
                "statement for %s stopped after %d of %d rows: %s",
                account.external_account_id,
                outcome.recorded,
                outcome.rows,
                refused,
            )
        return outcome
