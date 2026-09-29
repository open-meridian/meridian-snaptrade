# meridian-snaptrade

An [Open Meridian](https://open-meridian.com) plugin that reads brokerage
accounts through [SnapTrade](https://snaptrade.com): their positions, their
cash in each currency, and how fresh SnapTrade's data about them is. It records
them in a deployment's street store as the custodian's view, following
workflow W2 (holdings ingestion). It holds the `custody` role.

It is built on the SDK it pins, `open-meridian==0.6.1`, which carries the
whole account-side contract (spec/the-account-side-fits-every-venue): a
holding's side, a market value left unset, a currency marked as assumed, a fund
marked as counted in cash too, buying power on the statement, the sync state
with holdings and history freshness, the accounts a connection reaches, and
linking them. The asset class on an ambiguous miss is sent empty until
sdk-contract/asset-class-is-an-enum rules its names.

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
| connection `data_freshness_mode.snaptrade` (`realtime` or `delayed`) | how SnapTrade serves the connection: whether the Connections tab offers Refresh (below); unknown when absent |
| (the read) | a statement per account, its ID `snaptrade:<account>:<read time in ns>`, as of `data_freshness.as_of` |

Sync state, first that applies:

| SnapTrade | State | What to do (shown on the admin pages) |
|---|---|---|
| connection `disabled` | `disabled` | reconnect through SnapTrade's Connection Portal; it serves its last data meanwhile |
| holdings `initial_sync_completed: false` | `stale` | wait; nothing is recorded until the first sync is done |
| holdings `holdings_unavailable: true` | `stale` | nothing is recorded: an empty list does not mean an empty account |
| connection `data_freshness_mode.institution: delayed` (Interactive Brokers) | `delayed_by_design` | nothing; `stale` if more than four days old |
| last successful holdings sync older than `stale_after_hours` | `stale` | usually SnapTrade's to recover; if it lasts, refresh where Refresh is offered, or reconnect |
| otherwise | `current` | nothing |

Holdings freshness is the last successful holdings sync; history freshness is
the last successful transactions sync (a date). SnapTrade surfaces no state
that means `needs_sign_in` apart from `disabled`, so this plugin never reports
it.

## Settings

Declared through the SDK and given by a deployment administrator in the
dashboard's settings form, which is built from the declarations: each field's
label, a default greyed in the empty field, a unit beside a number, and the
key type first, as a choice whose answer decides which fields follow
(kernel/a-plugins-admin-view, points 4 and 7). While a required setting that
applies is missing, the sidecar reports the plugin unhealthy, naming it, and
the plugin calls nothing but says which it is waiting for. Synthetic mode runs
without any of them.

| Setting | Label | Kind | Secret | Required | |
|---|---|---|---|---|---|
| `snaptrade_key_type` | Key type | choice | no | yes | `personal` (Personal key: belongs to one SnapTrade user, you) or `commercial` (Commercial key: registers SnapTrade users of its own, each with a secret); default `personal` |
| `snaptrade_client_id` | Client ID | text | yes | yes | from SnapTrade's API keys page |
| `snaptrade_consumer_key` | Consumer key | text | yes | yes | shown once, when SnapTrade issues the key |
| `snaptrade_user_id` | User ID | text | no | with a commercial key | the SnapTrade user whose connections it reads |
| `snaptrade_user_secret` | User secret | text | yes | with a commercial key | that user's secret |
| `poll_seconds` | Read every | number, seconds | no | no | how often to read; default 300, at least 60 |
| `stale_after_hours` | Stale after | number, hours | no | no | when a sync is stale; default 24 |
| `synthetic` | Synthetic mode | on/off, developer | no | no | serve built-in responses instead of calling SnapTrade; default off |
| `snaptrade_personal_key` | Personal key (the old way) | on/off, developer | no | no | 0.1.0's way of saying the key's kind; read only while the key type is unset |

A developer's setting is shown in the form only on a development deployment.

**From 0.1.0.** 0.1.0 said the key's kind with `snaptrade_personal_key`, on or
off. While `snaptrade_key_type` is unset, a saved `snaptrade_personal_key`
decides it (on: personal, off: commercial), and with neither saved it is the
default, personal; once the key type is saved, the old setting is not read.
The old setting stays declared, as a developer's setting, rather than being
dropped: the sidecar delivers a plugin only the settings it declares, so an
undeclared one's saved value would never reach it, and the form leaves a
setting it does not show as it is when it saves, so nothing saved is lost.
The key type being required, the dashboard names it as missing until it is
saved once, though the plugin reads with the old setting meanwhile.

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
negative cash), and Schwab (disabled, with no stable account ID). SnapTrade
serves Alpaca and Schwab in real time and Interactive Brokers on a delay, so
only Interactive Brokers is offered Refresh; refreshing it in synthetic mode
asks nothing of SnapTrade.

## Its pages

Ruled 2026-09-29 (meridian-design's
intent/a-custody-plugin-serves-its-statement-receivers, "Admin pages and user
pages", step 1): admin pages are setup, user pages are daily work. This is
step 1, with no contract change: SnapTrade's own page, at `/`, is its user
side, and the admin view keeps Connections and Account links. Step 2 has
plugins declare user pages as they declare admin pages, and decisions/027
makes the admin pages a plugin admin's; neither is built here.

### Statements, the user side

**Statements** (`/`) shows, for each account, its sync state (with its
holdings and history freshness), its last statement (as of when, and what
recording it came to) and its rows, as the last read found them, each in an
`om-grid`. Who sees what (decisions/026, read from the verified
`Meridian-Caller` header):

- **A person who may read** (`caller.read`, checked with `caller.may_read`)
  sees each account linked to one of the deployment's accounts they may read,
  and no other.
- **A deployment admin** sees every account SnapTrade reaches.
- **Somebody with nothing to read** is told plainly that there is nothing
  here for them, and nothing is read for them.

It has no action and no form. Its sections become tabs inside the page once
there is more than one (Unresolved positions, Discrepancies and Upload are
planned); today there is one, so it draws none. In synthetic mode it says
every figure is invented.

What cuts it to a reader is which of the deployment's accounts each external
account is linked to, and the contract gives a plugin no read of its links
(meridian-design's sdk-contract/a-plugin-reads-its-own-links). So a reader
sees an account whose link this plugin can name: one linked on the Account
links tab since the plugin started. Any other account, including every one
linked before a restart, is shown to deployment admins only, never guessed
at, until that read exists. Nothing is stored to remember a link: plugins are
ephemeral.

### The admin pages

Two admin pages, declared at registration (`Interface(admin_pages=...)`),
which the dashboard's admin view of the instance shows as tabs after its own
Overview, Settings and Access, each framing its path (ruled 2026-09-29, "tabs
at both levels"):

- **Connections** (`/admin/connections`): figures for the last read, each
  brokerage connection with its health, what to do about it, and refreshing
  or reconnecting it, connecting a brokerage through SnapTrade's Connection
  Portal (a link that opens outside the dashboard's frame), and the SnapTrade
  users under the key.
- **Account links** (`/admin/accounts`): each account the connections reach
  with its link to one of the deployment's accounts, and each one's sync
  state, what to do, its freshness and its last statement.

Refresh asks SnapTrade to read a connection's brokerage again
(`refresh_brokerage_authorization`). It is offered only where it means
something, by the connection's `data_freshness_mode.snaptrade`:

- `delayed`: SnapTrade serves the connection from its cache. Refresh is
  offered, with a hint that SnapTrade may charge for each refresh.
- `realtime`: SnapTrade reads the brokerage on every call, and on its
  Real-time plans (Personal, Pay as you go) it refuses a refresh with HTTP
  403. No Refresh; the connection says "Real-time: SnapTrade fetches fresh
  data from the brokerage on every read, so there is nothing to refresh." A
  refresh posted from an older page is answered with that line, and SnapTrade
  is not asked.
- absent: Refresh is offered. Should SnapTrade refuse it (HTTP 403), the
  page says plainly that SnapTrade doesn't allow a manual refresh for this
  connection on this plan, and that it reads fresh data on every sync. Any
  other failure is shown as every failed call is: what was asked, the
  exception's type and the HTTP status, never its text.

0.2.0's Holdings tab is gone: what the last read found is on Statements.
Each admin page has "Read now". Each is served to a caller whose verified
`deployment_admin` claim is true, and anybody else is told it is for the
deployment's administrators, with the way to Statements. `/admin` sends the
caller to Connections. No credential is entered or shown here: keys are the
dashboard's settings form, and granting access is the dashboard's too.

### Linking accounts

Each plugin links its own external accounts (kernel/a-plugins-admin-view,
point 8), and the link is its right to the account: a statement for an account
nothing links is refused. On the Account links tab an account not linked offers
**Link to an existing account**, a picker of the deployment's open accounts
read with `read_accounts_for_linking`, each shown with its custodian and type
beside its name where the deployment has them, or **Create a new account**,
named from the SnapTrade account, its custodian pre-filled from the
connection's brokerage and its type from the account type SnapTrade reports,
all three editable, which the conductor creates and links in one step (an
owner and a note are given on the dashboard's Accounts tab); a linked one
offers **Unlink**. Each is sent with
`link_external_account`, acting for the admin viewing the page (their
`Meridian-Caller` header as `acting_for`), and the sidecar refuses it for
anybody else; a refusal is shown as the sidecar worded it.

The contract gives a plugin no read of its own links, so the page says what it
knows and how: a link made or removed from this page since the plugin
started; otherwise what the last read showed, rows recorded (only a link
allows that) or refused because nothing links the account; otherwise "not
known", with both linking and unlinking offered.

### Forms and the kit

Every action is a plain POST whose form carries a CSRF token, checked before
anything is done; one without it, or with another's, is refused. The plugin
host has its own session cookie (decisions/021), so the dashboard's front alone
would not stop a page elsewhere posting here through an administrator's
browser. The token is an HMAC of the verified caller's subject under a random
secret the plugin makes at start and keeps only in memory, so a restart only
means reloading the page.

The pages are built on Open Meridian's plugin UI kit
([meridian-ui](https://github.com/open-meridian/meridian-ui);
spec/plugin-pages-share-one-kit), which the dashboard serves at
`/.meridian/ui/<version>/` on the plugin's own host: `page.py` links its
stylesheet and script, uses its classes and its `om-grid` for the accounts and
each statement's rows, and has no style, colour or theme of its own. The dashboard
draws the tabs, the plugin's name, the way back and the person, and hands the
kit the person's colour scheme and light or dark. Where the kit is not served
the pages still work, unstyled: each table is in the HTML until the kit's grid
replaces it, and every action is a plain form. Quantities are exact decimal
strings, as SnapTrade reported them. The kit has no account-mapping component
yet, so the Account links tab draws one from its classes (a list row per account,
a badge for its link, and two plain forms).

`make preview` writes each page on synthetic data to `preview/`: Connections,
Account links, and Statements as a reader sees it who may read two of the
three accounts. They link the kit at `/.meridian/ui/0.1.0/`, so serve them
beside the kit to see them styled; opened on their own they are the pages
without the kit.

## Depends on

The [Python SDK](https://github.com/open-meridian/meridian-python) and nothing
else from Open Meridian, plus SnapTrade's official Python SDK
(`snaptrade-python-sdk`, pinned exactly), which only `src/snaptrade/venue.py`
imports.

The SDK is pinned exactly, `open-meridian==0.6.1`, and the `Dockerfile` and
`Makefile` build on the base image of the same version, `plugin-python:0.6.1`.
To move to a new SDK release, change all three together and run
`make ci-local`; `tests/test_contract.py` fails on any operation or parameter
the new SDK has that this plugin does not know, naming it.

## Working on it

    make ci-local        # lint (ruff, mypy strict), tests, and the plugin's image
    make preview         # each page on synthetic data, in preview/, linking the kit
    make install-hooks   # once per clone, so git push runs ci-local first

Everything runs in containers. Put it in a deployment, once a session is open
with `meridian connect`, with `meridian plugin upload` and
`meridian plugin launch snaptrade 0.3.1 --instance snaptrade`; or develop it
live with `meridian plugin dev --instance snaptrade` and `synthetic` on (the
`develop-live` skill under `.claude/` walks that loop).

## Licence

Apache-2.0, like the SDK it is built on. It is a plugin other vendors will
copy, and it should model the promise that a vendor keeps their plugin.
