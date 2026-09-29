# meridian-snaptrade

An [Open Meridian](https://open-meridian.com) plugin that reads brokerage
accounts through [SnapTrade](https://snaptrade.com): their positions, their
cash in each currency, and how fresh SnapTrade's data about them is. It records
them in a deployment's street store as the custodian's view, following
workflow W2 (holdings ingestion). It holds the `custody` role.

**Built offline, not yet run on a deployment.** It is written against the
SDK's operations that exist today, with everything the account-side contract
adds isolated until the SDK carries it (below). It has not yet read a real
SnapTrade account: no key is held yet.

## What it does

On start, on every settings change, and every `poll_seconds`, it:

1. reads the configured SnapTrade user's brokerage connections, their
   accounts, and each account's positions (`/accounts/{id}/positions/all`) and
   balances (`/accounts/{id}/balances`);
2. turns them into the platform's convention (`src/snaptrade/normalise.py`);
3. for each account, reports its sync status (W2.1) and, when there is
   something to record, opens a holdings statement with its row count (W2.2)
   and records each row (W2.3), resolving each instrument first (W3.1). A row
   nothing matches is recorded against the deployment's placeholder (W3.7); an
   ambiguous one is recorded unresolved and its miss published once (W3.2).
   A refusal stops that statement and is shown, never retried.

It holds nothing between reads. A restart reads again.

### How SnapTrade's shapes become the platform's

Per spec/the-account-side-fits-every-venue and decisions/023, in
meridian-design.

| SnapTrade | Recorded as |
|---|---|
| account `institution_account_id` | the stable account ID, `<brokerage slug>:<institution_account_id>`: the same when one real account is reached through two connections, and unchanged by a reconnect |
| no `institution_account_id` | `snaptrade:<account id>`, marked unstable: a reconnect makes a new SnapTrade ID, and the account appears as new, to be linked again |
| account `name`, `raw_type` | the custodian's name for it, and the venue's own account type verbatim (display only) |
| balance `cash` per `currency.code` | a holding of that currency's cash instrument, `{scheme: iso4217, value: <code>}`, one row per currency; negative cash is a short row |
| position `units` | the trade-date quantity, signed; negative is a short row, positive a long one |
| (none) | no settle-date quantity: SnapTrade reports none |
| position `price` | nothing. SnapTrade reports no market value, and one is never made from a price; a cash row's value is its amount |
| position `currency` | the row's currency; where SnapTrade states none for the position, the listing's, or the account's only cash currency, or USD, marked as assumed |
| `instrument.figi_instrument.figi_code` | identifier `{scheme: figi}` |
| `instrument.symbol` (a ticker, or an option's OCC symbol) | identifier `{scheme: symbol, source: snaptrade}` |
| `instrument.exchange`, when a MIC | the MIC resolution is qualified by |
| balance `buying_power` per currency | the statement's buying power, as reported, never derived |
| `cash_equivalent: true` (money-market funds) | kept as a position and marked; SnapTrade counts it in cash too |
| every JSON number | a `Decimal` read from its text; a float anywhere is `Decimal(repr(x))` |
| (the read) | a statement per account, its ID `snaptrade:<account>:<read time in ns>`, as of `data_freshness.as_of` |

Sync state, first that applies:

| SnapTrade | State | What to do (shown on the admin page) |
|---|---|---|
| connection `disabled` | `disabled` | reconnect through SnapTrade's Connection Portal; it serves its last data meanwhile |
| holdings `initial_sync_completed: false` | `stale` | wait; nothing is recorded until the first sync is done |
| holdings `holdings_unavailable: true` | `stale` | nothing is recorded: an empty list does not mean an empty account |
| connection `data_freshness_mode.institution: delayed` (Interactive Brokers) | `delayed_by_design` | nothing; `stale` if more than four days old |
| last successful holdings sync older than `stale_after_hours` | `stale` | usually SnapTrade's to recover; refresh if it lasts |
| otherwise | `current` | nothing |

Holdings freshness is the last successful holdings sync; history freshness is
the last successful transactions sync (a date). SnapTrade surfaces no state
that means `needs_sign_in` apart from `disabled`, so this plugin never reports
it.

## Settings

Declared through the SDK and given by a deployment administrator in the
dashboard's settings form. None is required, so synthetic mode runs before any
key exists; without it, the plugin reports itself unhealthy, naming the
settings it is waiting for, and calls nothing.

| Setting | Kind | Secret | |
|---|---|---|---|
| `snaptrade_client_id` | text | yes | SnapTrade client ID |
| `snaptrade_consumer_key` | text | yes | SnapTrade consumer key |
| `snaptrade_user_id` | text | no | the SnapTrade user whose connections it reads; not with a personal key |
| `snaptrade_user_secret` | text | yes | that user's secret; not with a personal key |
| `snaptrade_personal_key` | on/off | no | the key is a SnapTrade personal key (off: a commercial key) |
| `synthetic` | on/off | no | serve built-in responses instead of calling SnapTrade (off by default) |
| `poll_seconds` | number | no | how often to read; default 900, at least 60 |
| `stale_after_hours` | number | no | when a sync is stale; default 36 |

The credentials reach the plugin as settings and nowhere else. They are never
logged, shown or bundled: they are kept out of every repr, and a failed call
to SnapTrade is described by what was asked, the exception's type and the
HTTP status only, because SnapTrade's SDK sends the user secret in the query
string and its exceptions can carry it. The tests hold all three to that.

## Synthetic mode

With `synthetic` on, the plugin serves built-in responses shaped like
SnapTrade's documented API (`src/snaptrade/synthetic.py`; the tests validate
each against SnapTrade's own models). It records them through the sidecar like
real ones, so it can be developed live on a deployment before any key exists.
Every value is invented. Three connections, chosen so each rule has something
to act on: Alpaca (current; long and short stock, an option, a money-market
fund, crypto to nine decimals, cash in two currencies), Interactive Brokers
(delayed by design; a euro listing, a position with no stated currency,
negative cash), and Schwab (disabled, with no stable account ID).

## The admin page

Ruled 2026-09-28: an admin portal only; people see accounts and holdings
through a reporting plugin, not through custody. `/admin` shows the SnapTrade
users under the key, each brokerage connection with its health, each account
with its sync state, what to do, its freshness and its last statement, and
offers connecting a brokerage through SnapTrade's Connection Portal (a link
that opens outside the dashboard's frame), reconnecting and refreshing a
connection, and reading now. `/` sends an administrator there and tells anyone
else the plugin has no page for them. No credential is entered or shown here:
keys are the dashboard's settings form, and linking accounts and granting
access are the dashboard's too.

Every action is a POST whose form carries a CSRF token, checked before
anything is done; one without it, or with another's, is refused. The plugin
host has its own session cookie (decisions/021), so the dashboard's front alone
would not stop a page elsewhere posting here through an administrator's
browser. The token is an HMAC of the verified caller's subject under a random
secret the plugin makes at start and keeps only in memory, so a restart only
means reloading the page.

Markup (`page.py`) and style (`static/page.css`, Open Meridian's design tokens
as custom properties, light and dark) are kept apart, to move onto the shared
plugin kit (spec/plugin-pages-share-one-kit). `make preview` writes the page on
synthetic data to `preview.html`.

## Waiting for the contract

The account-side contract (spec/the-account-side-fits-every-venue, accepted
2026-09-28) is being built and is not in the SDK this plugin pins. Each part is
isolated in `src/snaptrade/contract.py` and switches on by itself when the
installed SDK's operations carry it, detected by parameter name:

- **A holding's side** (`record_holding(side=...)`). Until then the quantity's
  sign says it.
- **The settle-date quantity** (`settle_date_quantity`). SnapTrade reports
  none, so nothing changes for it.
- **A currency marked as assumed** (`currency_assumed`). Until then the
  assumption goes unsaid.
- **A market value left unset** (`market_value=None` accepted). Until then a
  value SnapTrade did not report is sent as zero in the row's currency, which
  is what the street store reads an unset value as today.
- **Buying power on the statement** (`record_holdings_statement(buying_power=...)`).
  One figure per statement; sent only when SnapTrade reports buying power in
  exactly one currency.
- **Sync state and holdings and history freshness**
  (`report_sync_status(state=..., holdings_as_of_ns=..., history_as_of_ns=...)`).
  Until then `connection_healthy` (true for `current` and `delayed_by_design`)
  and the state at the head of the detail text.
- **The accounts a connection reaches** (`report_external_accounts`, on
  `platform.custody.{instance}.event.external-accounts`). Until then the
  dashboard learns an account when its rows are refused unlinked (W4.8).
- **Who administers the deployment**, on the verified caller
  (kernel/a-plugins-admin-view). Until then the admin page is served to
  nobody.
- **The asset class on an ambiguous miss** (sdk-contract/asset-class-is-an-enum).
  Sent empty until the names are ruled.

The names are those in the contract's in-progress protos. A name that
differs leaves its part off, which is safe, and it does not go unnoticed:
`tests/test_contract.py` fails when the pinned SDK's operations have a
parameter or an operation this plugin does not know, naming it.

## Depends on

The [Python SDK](https://github.com/open-meridian/meridian-python) and nothing
else from Open Meridian, plus SnapTrade's official Python SDK
(`snaptrade-python-sdk`, pinned exactly), which only `src/snaptrade/venue.py`
imports.

The SDK is pinned exactly, `open-meridian==0.4.0`, and the `Dockerfile` builds
on the base image of the same version, `plugin-python:0.4.0`. To move to a new
SDK release, change both together and run `make ci-local`.

## Working on it

    make ci-local        # lint (ruff, mypy strict), tests, and the plugin's image
    make preview         # the admin page on synthetic data, as preview.html
    make install-hooks   # once per clone, so git push runs ci-local first

Everything runs in containers. Put it in a deployment, once a session is open
with `meridian connect`, with `meridian plugin upload` and
`meridian plugin launch snaptrade 0.1.0 --instance snaptrade`; or develop it
live with `meridian plugin dev --instance snaptrade` and `synthetic` on (the
`develop-live` skill under `.claude/` walks that loop).

## Licence

Apache-2.0, like the SDK it is built on. It is a plugin other vendors will
copy, and it should model the promise that a vendor keeps their plugin.
