"""What reaches the sidecar, through the SDK's typed operations.

Each part of the account-side contract (spec/the-account-side-fits-every-venue)
as the pinned SDK takes it: a holding's side, its settle-date quantity where
the venue gives one (SnapTrade gives none), a currency marked as assumed, a
market value left unset where none was reported, a fund marked as counted in
cash too, buying power on the statement, the sync state with holdings and
history freshness, and the accounts a connection reaches (W2.8).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import meridian

from .normalise import (
    SOURCE,
    ExternalAccount,
    Freshness,
    Holding,
    Side,
    Statement,
    SyncState,
    day_ns,
    ns,
)

log = logging.getLogger("snaptrade")

_SYNC_STATE: dict[SyncState, meridian.SyncState] = {
    SyncState.CURRENT: meridian.SyncState.SYNC_STATE_CURRENT,
    SyncState.STALE: meridian.SyncState.SYNC_STATE_STALE,
    SyncState.NEEDS_SIGN_IN: meridian.SyncState.SYNC_STATE_NEEDS_SIGN_IN,
    SyncState.DISABLED: meridian.SyncState.SYNC_STATE_DISABLED,
    SyncState.DELAYED_BY_DESIGN: meridian.SyncState.SYNC_STATE_DELAYED_BY_DESIGN,
}
_SIDE: dict[Side, meridian.HoldingSide] = {
    Side.LONG: meridian.HoldingSide.HOLDING_SIDE_LONG,
    Side.SHORT: meridian.HoldingSide.HOLDING_SIDE_SHORT,
}


def refused_unlinked(refused: meridian.MeridianError) -> bool:
    """Whether a row was refused because its external account is not linked
    (W4.8): by the refusal's code, which the SDK raises as `NotLinked`, never
    by its words."""
    return isinstance(refused, meridian.NotLinked)


@dataclass
class Outcome:
    """What recording one account's statement came to."""

    statement_id: str = ""
    rows: int = 0
    recorded: int = 0
    placeholders: int = 0
    ambiguous: int = 0
    already_recorded: bool = False
    # Why it stopped before every row was recorded, when it did.
    stopped: str = ""
    # Stopped because the account is not linked to one of the deployment's.
    unlinked: bool = False


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
        """W2.8: the accounts the connection reaches. The sidecar stamps the
        source and the time; the venue's own word for the account's kind is
        carried verbatim."""
        await self._plugin.report_external_accounts(
            accounts=[
                meridian.ExternalAccount(
                    external_account_id=account.external_account_id,
                    name=account.name,
                    venue_account_type=account.account_type,
                )
                for account in accounts
            ],
        )

    async def report_sync(
        self, account: ExternalAccount, fresh: Freshness, observed_at_ns: int
    ) -> None:
        """W2.1: how fresh the account's data is, and why when it is not."""
        holdings_as_of_ns = ns(fresh.holdings_as_of) if fresh.holdings_as_of else 0
        await self._plugin.report_sync_status(
            source=SOURCE,
            last_synced_at_ns=holdings_as_of_ns,
            connection_healthy=fresh.healthy,
            status_detail=fresh.detail,
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
        answer = await self._plugin.resolve_identifier(
            identifiers=identifiers,
            as_of_ns=as_of_ns,
            exchange_mic=holding.exchange_mic,
            currency=holding.currency,
        )
        if answer.instrument_id:
            # Found, or nothing matched and the deployment's placeholder
            # stands in (W3.7); the instrument store reports the latter.
            outcome.placeholders += 1 if answer.placeholder else 0
            return {"instrument_id": answer.instrument_id}
        # More than one matched. A fact, published once, and the row is still
        # recorded; nothing is retried or guessed.
        outcome.ambiguous += 1
        await self._plugin.report_missing_instrument(
            source=SOURCE,
            # An enum since SDK 0.9.0 (sdk-contract/asset-class-is-an-enum),
            # and still sent unset: SnapTrade's own kinds are not mapped to
            # the ruled classes yet, and no class is better than a guessed one.
            asset_class="",
            identifiers=identifiers,
            as_of_ns=as_of_ns,
            reason=answer.miss_reason,
            observed_at_ns=observed_at_ns,
        )
        return {"unresolved_identifiers": identifiers}

    def _row(self, holding: Holding) -> dict[str, Any]:
        return {
            "quantity": holding.quantity,
            "side": _SIDE[holding.side],
            # Unset where SnapTrade reported none; never made from a price.
            "market_value": holding.market_value,
            "currency_assumed": holding.currency_assumed,
            # A money-market fund the venue also counts in its cash figure:
            # recorded as a holding, and marked so nothing counts it twice.
            "also_counted_in_cash": holding.cash_equivalent,
            # None where the venue gives none, as SnapTrade never does.
            "settle_date_quantity": holding.settle_date_quantity,
        }

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
            opened = await self._plugin.record_holdings_statement(
                source=SOURCE,
                external_statement_id=statement.external_statement_id,
                as_of_date=statement.as_of_date,
                read_at_ns=statement.read_at_ns,
                expected_rows=len(statement.holdings),
                # One figure per statement in the contract; SnapTrade gives one
                # per currency, and several are not summed into one.
                buying_power=(
                    statement.buying_power[0] if len(statement.buying_power) == 1 else None
                ),
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
                    **self._row(holding),
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
