"""What reaches the sidecar, through the typed operations the SDK has.

WAITING FOR THE CONTRACT. The account-side contract
(spec/the-account-side-fits-every-venue, accepted 2026-09-28) is being built
and is not in the SDK this plugin pins yet. Everything it adds is isolated
here, behind `Contract`, which reads the installed SDK's operations and turns
each part on when that SDK carries it, with no other change to the plugin:

Each part, what switches it on, and what happens until then:

- Holding side: `record_holding(side=...)`. Until then the quantity's sign
  says it (negative is short).
- Settle-date quantity: `record_holding(settle_date_quantity=...)`. Nothing
  until then, and SnapTrade gives none anyway.
- Currency assumed: `record_holding(currency_assumed=...)`. Until then the
  assumption goes unsaid.
- Market value unset: `record_holding(market_value=None)` accepted. Until
  then zero, which the street store already reads an unset value as.
- Statement figures: `record_holdings_statement(buying_power=...)`. Until
  then buying power is not sent.
- Sync state and freshness: `report_sync_status(state=...)`. Until then
  `connection_healthy`, and the state at the head of the detail text.
- External accounts: `report_external_accounts`. Until then the dashboard
  learns of an account when its rows are refused unlinked (W4.8).
- Administrator: `meridian.Caller.administrator`. Until then nobody is served
  the admin page.

The parameter names are those the contract's in-progress protos give
(meridian-core holdings.proto, uncommitted on 2026-09-28): `side`,
`settle_date_quantity`, `currency_assumed`, `buying_power`, `state`,
`holdings_as_of_ns`, `history_as_of_ns`; and the operation the matrix row
ReportExternalAccounts generates. Confirm each against the SDK when it lands:
a name that differs leaves its part off, which is safe, and
tests/test_contract.py fails on any parameter or operation it does not know.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import meridian

from .normalise import (
    SOURCE,
    ExternalAccount,
    Freshness,
    Holding,
    Side,
    Statement,
    day_ns,
    ns,
)

log = logging.getLogger("snaptrade")

# The contract's names for what it adds, in one place.
SIDE = "side"
SETTLE_DATE_QUANTITY = "settle_date_quantity"
CURRENCY_ASSUMED = "currency_assumed"
ALSO_COUNTED_IN_CASH = "also_counted_in_cash"
MARKET_VALUE = "market_value"
BUYING_POWER = "buying_power"
SYNC_STATE = "state"
HOLDINGS_AS_OF = "holdings_as_of_ns"
HISTORY_AS_OF = "history_as_of_ns"
REPORT_EXTERNAL_ACCOUNTS = "report_external_accounts"
ADMINISTRATOR = "administrator"


def _parameters(plugin: object, operation: str) -> dict[str, inspect.Parameter]:
    method = getattr(plugin, operation, None)
    if method is None:
        return {}
    try:
        return dict(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class Contract:
    """Which parts of the account-side contract the installed SDK carries."""

    holding_side: bool = False
    settle_date_quantity: bool = False
    currency_assumed: bool = False
    optional_market_value: bool = False
    statement_figures: bool = False
    sync_state: bool = False
    external_accounts: bool = False
    also_counted_in_cash: bool = False

    @classmethod
    def of(cls, plugin: object) -> Contract:
        holding = _parameters(plugin, "record_holding")
        opening = _parameters(plugin, "record_holdings_statement")
        sync = _parameters(plugin, "report_sync_status")
        value = holding.get(MARKET_VALUE)
        return cls(
            holding_side=SIDE in holding,
            settle_date_quantity=SETTLE_DATE_QUANTITY in holding,
            currency_assumed=CURRENCY_ASSUMED in holding,
            optional_market_value=value is not None and value.default is None,
            statement_figures=BUYING_POWER in opening,
            sync_state=SYNC_STATE in sync and HOLDINGS_AS_OF in sync,
            external_accounts=callable(getattr(plugin, REPORT_EXTERNAL_ACCOUNTS, None)),
            also_counted_in_cash=ALSO_COUNTED_IN_CASH in holding,
        )

    def waiting(self) -> tuple[str, ...]:
        """What is still waiting for the SDK, in words for a log line or a page."""
        parts = {
            "a holding's side": self.holding_side,
            "the settle-date quantity": self.settle_date_quantity,
            "marking a currency as assumed": self.currency_assumed,
            "leaving a market value unset": self.optional_market_value,
            "buying power on the statement": self.statement_figures,
            "the sync state and its freshness": self.sync_state,
            "reporting the accounts a connection reaches": self.external_accounts,
            "marking a fund counted in cash": self.also_counted_in_cash,
        }
        return tuple(part for part, carried in parts.items() if not carried)


def is_administrator(caller: meridian.Caller) -> bool:
    """Whether the verified caller is a deployment administrator.

    WAITING FOR THE CONTRACT: the caller's claims say who a person is and
    what they hold on this plugin, and not yet whether they administer the
    deployment (kernel/a-plugins-admin-view). Until they do, this is False
    for everybody, so the admin page is served to nobody rather than to
    somebody it should not be.
    """
    return getattr(caller, ADMINISTRATOR, None) is True


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


def _identifiers(holding: Holding) -> list[meridian.Identifier]:
    return [
        meridian.Identifier(scheme=held.scheme, value=held.value, source=held.source)
        for held in holding.identifiers
    ]


class Recorder:
    """Sends what the normaliser made through the SDK's typed operations,
    each part in the form the installed contract takes."""

    def __init__(self, plugin: meridian.Plugin, contract: Contract) -> None:
        self._plugin = plugin
        self._contract = contract

    async def report_accounts(
        self, accounts: Sequence[ExternalAccount], observed_at_ns: int
    ) -> bool:
        """The accounts the connection reaches (W2.8), when the SDK can say so.
        Returns whether they were sent."""
        if not self._contract.external_accounts:
            return False
        report: Any = getattr(self._plugin, REPORT_EXTERNAL_ACCOUNTS)
        # The sidecar stamps the source and the time (W2.8); the venue's own
        # word for the account's kind is carried verbatim.
        await report(
            accounts=[
                meridian.ExternalAccount(
                    external_account_id=account.external_account_id,
                    name=account.name,
                    venue_account_type=account.account_type,
                )
                for account in accounts
            ],
        )
        return True

    async def report_sync(
        self, account: ExternalAccount, fresh: Freshness, observed_at_ns: int
    ) -> None:
        """W2.1: how fresh the account's data is, and why when it is not."""
        call: dict[str, Any] = {
            "source": SOURCE,
            "last_synced_at_ns": ns(fresh.holdings_as_of) if fresh.holdings_as_of else 0,
            "connection_healthy": fresh.healthy,
            "observed_at_ns": observed_at_ns,
            "external_account_id": account.external_account_id,
        }
        if self._contract.sync_state:
            call[SYNC_STATE] = f"SYNC_STATE_{fresh.state.name}"
            call[HOLDINGS_AS_OF] = call["last_synced_at_ns"]
            call[HISTORY_AS_OF] = day_ns(fresh.history_as_of) if fresh.history_as_of else 0
            call["status_detail"] = fresh.detail
        else:
            # The state, where only free text can carry it.
            state = fresh.state.value
            call["status_detail"] = f"{state}: {fresh.detail}" if fresh.detail else state
        await self._plugin.report_sync_status(**call)

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
            # WAITING FOR THE CONTRACT: asset classes are being made an enum
            # (sdk-contract/asset-class-is-an-enum); unset until its names are.
            asset_class="",
            identifiers=identifiers,
            as_of_ns=as_of_ns,
            reason=answer.miss_reason,
            observed_at_ns=observed_at_ns,
        )
        return {"unresolved_identifiers": identifiers}

    def _row(self, holding: Holding) -> dict[str, Any]:
        row: dict[str, Any] = {"quantity": holding.quantity}
        if self._contract.optional_market_value:
            row[MARKET_VALUE] = holding.market_value
        else:
            # WAITING FOR THE CONTRACT: today's SDK requires a value. Zero in
            # the row's currency is what the street store reads an unset one
            # as, so it says nothing the store would not already assume.
            row[MARKET_VALUE] = holding.market_value or meridian.Money(
                Decimal(0), holding.currency
            )
        if self._contract.holding_side:
            row[SIDE] = (
                "HOLDING_SIDE_SHORT" if holding.side is Side.SHORT else ("HOLDING_SIDE_LONG")
            )
        if self._contract.settle_date_quantity and holding.settle_date_quantity is not None:
            row[SETTLE_DATE_QUANTITY] = holding.settle_date_quantity
        if self._contract.currency_assumed:
            row[CURRENCY_ASSUMED] = holding.currency_assumed
        if self._contract.also_counted_in_cash:
            # A money-market fund the venue also counts in its cash figure:
            # recorded as a holding, and marked so nothing counts it twice.
            row[ALSO_COUNTED_IN_CASH] = holding.cash_equivalent
        return row

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
            opening: dict[str, Any] = {
                "source": SOURCE,
                "external_statement_id": statement.external_statement_id,
                "as_of_date": statement.as_of_date,
                "read_at_ns": statement.read_at_ns,
                "expected_rows": len(statement.holdings),
            }
            if self._contract.statement_figures and len(statement.buying_power) == 1:
                # One figure per statement in the contract; SnapTrade gives one
                # per currency, and several are not summed into one.
                opening[BUYING_POWER] = statement.buying_power[0]
            opened = await self._plugin.record_holdings_statement(**opening)
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
            log.warning(
                "statement for %s stopped after %d of %d rows: %s",
                account.external_account_id,
                outcome.recorded,
                outcome.rows,
                refused,
            )
        return outcome
