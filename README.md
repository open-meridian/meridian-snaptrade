# meridian-snaptrade

An [Open Meridian](https://open-meridian.com) plugin that reads brokerage
accounts through [SnapTrade](https://snaptrade.com): their positions, their
cash in each currency, and how fresh SnapTrade's data about them is. It records
them in a deployment's street store as the custodian's view, following
workflow W2 (holdings ingestion), and each account's activity as the
custodian states it. It holds the `custody` role. This is release 0.13.0
(CHANGELOG.md says what each release changed). How to set it up and use it,
for its admin and its readers, is in [docs/](docs/README.md). How a plugin
like it is built is documented at [open-meridian.dev](https://open-meridian.dev).

It is built on the SDK it pins, `open-meridian==0.21.0`, which declares
contract v16, an edge plugin's older records move to the archive
(meridian-design tasks/sdk-contract/an-edge-plugins-older-records-move-to-the-
archive): its two kinds of raw record, reported activity and raw responses,
are declared with their windows, which became the SDK's window settings with
0.12.0's values carried over; past its window a day of raw responses or a
month of activity records is archived, kept or deleted as an admin of the
plugin chose, each move recorded first and a deletion inside the
deployment's hold refused; what storage holds goes on the heartbeat; and the
Raw responses tab lists what moved and restores it ("The archive", below).
Since contract v15, access per role (tasks/sdk-contract/access-is-granted-
per-role): a plugin holding one role, as this one does, names none on its
pages and settings, and nothing it serves changed; and an activity recorded with its instrument unresolved is
re-resolved once it resolves (tasks/sdk-contract/an-activity-is-re-resolved-
when-its-instrument-resolves): an activity the street holds under a plan's
own code a person links later is re-resolved through the link, the activity
kept as first recorded and the re-resolution beside it ("The custodian's
activity", below). Since contract v14, the custodian's activity explains a
break (tasks/sdk-contract/the-custodians-activity-contract): each activity on a
linked account is reported to the street as SnapTrade states it, a backfill
back to the first date SnapTrade's history of the account reaches and then
each read's, its kind converted from SnapTrade's type, and the sync status
says from when the history can be read ("The custodian's activity", below).
Since contract v12, the deployment serves its MCP (meridian-design
tasks/sdk-contract/a-deployment-serves-its-mcp-contract): every route is typed
and offered to agents as a tool, or says why not ("Offered to agents",
below). Since contract v11, the edge keeps its own (meridian-design
tasks/sdk-contract/the-edge-keeps-its-own), and it carries the whole
account-side contract (spec/the-account-side-fits-every-venue): a holding's
side, a market value left unset, each asset counted once (a fund SnapTrade
counts in cash too is kept, and the cash sent net of it), the settled
quantity and the pending by value date, every row and statement naming the
raw response it was converted from and every value closed rather than read
carrying its provenance, an account's kind converted from SnapTrade's type
(or not known, with the type as reported), the version's declaration and the
storage it asks for, a holding's average cost and lots as the venue reports them, a
statement naming its external account and institution with its figures for
the account as a whole (buying power and net liquidation), the sync state
with holdings and history freshness, the accounts a connection reaches,
linking them, one external account to each of the deployment's accounts,
and reading its own links; and the access-by-button model (sdk-contract/a-plugin-has-admins):
its pages, each declared with the levels it serves, opened by the dashboard's
Manage, Open and View; and the figures a plugin reports on the Summary core
draws (sdk-contract/a-plugin-reports-its-figures). An ambiguous miss carries
the asset class SnapTrade's instrument kind maps to, or none where it maps to
none.

## What it does

On start, on every settings change, and every `poll_seconds`, it:

1. reads the configured SnapTrade user's brokerage connections, their
   accounts, and each account's positions (`/accounts/{id}/positions/all`),
   balances (`/accounts/{id}/balances`) and the last ten days of its
   activities (`/accounts/{id}/activities`), for what is not yet settled;
2. turns them into the platform's convention (`src/snaptrade/normalise.py`);
3. for each account, reports its sync status (W2.1) and, when there is
   something to record, opens a holdings statement with its row count (W2.2)
   and records each row (W2.3), resolving each instrument first (W3.1), and
   stating what SnapTrade says of it: its asset class, its currency, its
   description, and for a money market fund its instrument type. A row
   nothing matches is recorded against the record the deployment mints for it
   (W3.7); an ambiguous one is recorded unresolved and its miss published once
   (W3.2), with the instrument's asset class where its kind gives one, and
   SnapTrade's kind as reported where it gives none. The statement
   names the external account it was read for and its institution; one for an
   account nothing links is refused before any row.
   A refusal stops that statement and is shown, never retried. A statement
   this plugin cannot serve clean -- a fund counted in cash with no price, or
   worth more than the cash -- is withheld, and the Statements tab says why;
4. for each account linked to one of the deployment's, reports its
   activities (W2.10, contract v14): the first time this process reads it,
   every activity SnapTrade holds back to its `history_from` (a backfill),
   and after that the activities each read fetched. The street keeps each
   once ("The custodian's activity", below).

It holds nothing between reads that it needs: a restart reads again. What
SnapTrade answered each read is kept beside, per account, for the Raw
responses tab ("Raw responses: what SnapTrade said", below), and past its
window archived, kept or deleted as an admin of the plugin chose ("The
archive", below); nothing is ever read back from it into what is recorded.

When a person or an agent asks, it also reads an account's history from
SnapTrade -- its activities over a range, back to the first transaction
SnapTrade holds -- and proposes lots for the positions SnapTrade lists no
tax lots for (the History tab, below). Neither is sent to the street from
there: the activities reach it by the reads above, and the proposals are for
the person to carry into an opening balance and answer for there.

### The custodian's activity

Contract v14 (meridian-design plans/the-custodians-activity-explains-a-break,
"Design", SnapTrade). Activity is evidence that explains a break, never a
source: the street keeps it as reported and derives no position, lot or
figure from it, and the sample operations plugin matches a break to the
activity that explains it and proposes the adjustment for a person to
confirm. Nothing is netted or merged: a sweep fund's purchases are sent as
SnapTrade lists them.

- **A backfill, then each read.** The first read of a linked account in a
  process reads its whole history from SnapTrade, from the first date its
  history reaches (`sync_status.transactions.first_transaction_date`, sent as
  the sync status's `history_from`), page by page, up to 50 pages of 1,000,
  and reports each activity; each read after reports the activities it
  fetched, the last ten days. The street keeps an activity once, by the
  source, the account and SnapTrade's activity ID, and answers it already
  recorded when it comes again, so a restart's backfill records nothing
  twice, and the plugin holds nothing to remember it by. The street stamps
  each with its own record time, never back-dated; its trade date is the
  event's. An account nothing links reports none, and its backfill when it is
  linked. A backfill SnapTrade did not answer is tried again on the next
  read.
- **The kinds.** `BUY` purchase, `SELL` sale, `REI` reinvestment,
  `DIVIDEND`, `INTEREST`, `FEE`, `TAX`, `SPLIT`, `STOCK_DIVIDEND` a corporate
  action, the transfers in and out (`EXTERNAL_ASSET_`, `INTERNAL_ASSET_` and
  `INTERNAL_CASH_TRANSFER_IN` and `_OUT`), `CONTRIBUTION`, `WITHDRAWAL`,
  `JOURNALED` journal. Any other type (an option's expiry, an adjustment, a
  `TRANSFER` naming no direction) is sent not known with SnapTrade's type as
  reported (`snaptrade:activity-type`), for a person to map.
- **Signs and values.** Units added to the account are positive and units
  removed negative, whichever sign SnapTrade wrote: a purchase, a
  reinvestment and a transfer in add; a sale, a transfer out, and a fee or
  tax taken in units remove; a split's units are as stated. The amount is
  SnapTrade's own sign, cash in positive and cash out negative. A value
  SnapTrade writes as 0 where none applies (a dividend's units, a split's
  amount) is sent unset, never zero, and no price is worked out.
- **The instrument.** An activity naming a security the account holds is
  resolved by that holding's identifiers, to the holding's instrument. A
  plan's own fund code (Fidelity's `OQKR` for the plan's VIGIX) that a person
  linked to a symbol is resolved as that symbol is, with that person's name
  as its provenance. Any other code travels as reported (`snaptrade:symbol`),
  the instrument empty: resolving a symbol only an old activity names would
  mint a record for the deployment's admin to complete for every security
  the account ever traded. Cash alone names none.
- **Not sent, and why.** An activity with no ID, no trade date, an amount in
  no currency, or a number that would not cross the wire exactly is not
  sent, and the plugin logs why; any other refusal of one activity is said
  and the rest go on, and the account not linked stops them.
- **Its raw record is its own**, `activities/<external account>/<activity
  ID>`: SnapTrade's entry for it as the read that first reported it received
  it, in storage for its kind's window from when it was received (seven
  years by default, `activity_window_days`; an admin may set it longer, up to
  36,500 days), then archived, kept or deleted, and never deleted within the
  history SnapTrade reported, so each activity the street holds can have its
  record read back, or restored and read back, on the Raw responses tab
  (`/raw?ref=<key>`).
- **The plan-code link** (question 4; the product owner's option A,
  2026-10-05): the table setting `plan_code_links`, entered by an admin of
  the plugin in the dashboard's Settings form as an editable typed table,
  each row an external account this plugin reported, the plan's own code as
  SnapTrade names it, and a deployment instrument record chosen by search.
  The plugin only reads it: an activity under a linked code is that record,
  its provenance who added or last changed the row and when, as the
  conductor stamped them. No plugin storage holds it, and no page of the
  plugin sets it.
- **A link set later re-resolves what the street holds** (contract v15,
  W2.15; the product owner, 2026-10-05: "Re-resolve the earlier OQKR
  activities through the link"). An activity reported before its code was
  linked kept the code as reported, and a redelivery never changes it. When
  a settings delivery adds a `plan_code_links` row or changes one, the
  account it names is backfilled again on the read the delivery wakes, and
  each activity the street answers already recorded under a linked code is
  re-resolved through the link (`ReResolveActivity`): the record chosen, who
  added or last changed the row, and when the row says (its `changed_at`,
  never when the plugin read it). The street keeps the activity as first
  recorded and each re-resolution beside it, its own record, and answers one
  naming what the activity's latest resolution already names as already
  recorded: an activity first recorded through the same link, or
  re-resolved through it before. So a settings delivery again, a restart's
  backfill, and the start's first backfill (which re-resolves whatever a
  current link now resolves) record nothing twice, and the plugin holds
  nothing to remember a re-resolution by. A row that says no time it was
  changed re-resolves nothing, and the read says why. A row removed is not
  sent: the activities it re-resolved keep their latest resolution.

### How SnapTrade's shapes become the platform's

Per spec/the-account-side-fits-every-venue and decisions/023, in
meridian-design.

| SnapTrade | Recorded as |
|---|---|
| account `institution_account_id` | the stable account ID, `<brokerage slug>:<institution_account_id>`: the same when one real account is reached through two connections, and unchanged by a reconnect |
| no `institution_account_id` | `snaptrade:<account id>`, marked unstable: a reconnect makes a new SnapTrade ID, and the account appears as new, to be linked again |
| account `name` | the custodian's name for it |
| account `raw_type` | the account's kind (contract v11, Q13): one saying only margin is `margin`, only cash is `cash`, and any naming a retirement account (IRA, Roth, 401k, 403b, 457b, RRSP, RRIF, LIRA, SEP, SIMPLE or pension) is `retirement`; any other, Joint say, is not known, with `raw_type` sent beside it as reported (`snaptrade:account-type`). It pre-fills a new account's type on Account links, and is never sent as the venue's type |
| balance `cash` per `currency.code` | a holding of that currency's cash instrument, `{scheme: iso4217, value: <code>}`, one row per currency; negative cash is a short row |
| position `units` | the trade-date quantity, signed; negative is a short row, positive a long one |
| (none: SnapTrade states no settled quantity) | the settled quantity, derived from the account's activities: the quantity less each `BUY` or `SELL` traded on or before the as-of date and settling after it, each of those pending on its `settlement_date`, both with their provenance (derived by that rule; the pending reported in the activities call). Where the activities cannot be read, the settled quantity is the quantity, by a rule that says so, and the statement's problems say it |
| position `price` | nothing. SnapTrade reports no market value, and one is never made from a price; a cash row's value is its amount |
| position `cost_basis` | the holding's average cost, per unit (per share for an option), as reported, in the row's currency (the product owner, 2026-10-01); never multiplied out, so the holding's total cost basis is left unset: SnapTrade reports none. Two rows merged into one send none |
| position `tax_lots[]` | the holding's lots, in SnapTrade's order: `quantity` signed as the holding (a short holding's lots are short), `cost_basis` as the lot's cost, the whole lot's, sign as reported, and the date part of `original_purchase_date` as written. None where SnapTrade lists none (lots come only on some brokerages, and with SnapTrade's paid add-on), never a made-up one; and none for a holding any of whose lots cannot be read exactly (no quantity, a side contradicting the holding's, a number past 18 places, a date that is not one), said in the log. `purchased_price`, `current_value` and `lot_id` are not sent |
| position `currency` | the row's currency; where SnapTrade states none for the position, the listing's, which SnapTrade states too; where it states neither, the account's only cash currency, or USD, each amount in it (an average cost, a lot's cost) carrying its provenance, derived by that rule |
| `instrument.figi_instrument.figi_code` | identifier `{scheme: figi}` |
| `instrument.symbol` (a ticker, or an option's OCC symbol) | identifier `{scheme: symbol, source: snaptrade}` |
| `instrument.exchange`, when a MIC | the MIC resolution is qualified by |
| `instrument.kind` | the asset class an ambiguous miss is reported with (the product owner, 2026-10-01): `stock` is `equity`; `etf` and `mutualfund` are `fund`; `bond` is `debt`; `option` is `derivative`; `crypto` is `crypto_asset`; `adr` is `equity`; `cef` is `fund`; `future`, `future_option` and `cfd` are `derivative`. `tokenized_asset` (its class is what the token stands for), `other`, and any kind SnapTrade adds are sent with no class, for a person to set on the platform; a cash row is `cash` |
| balance `buying_power` per currency | the statement's buying power, as reported, never derived, where exactly one currency reports it: buying power in several currencies is not summed, and a currency is not a margin segment |
| account `balance.total` | the statement's net liquidation, the account's total value as the brokerage gave it (the product owner, 2026-10-01); none where SnapTrade gives none |
| (the statement's figures) | one set with no segment, the account's as a whole, holding the two above; none where neither is reported. SnapTrade reports no margin requirement, maintenance excess, initial or variation margin, or collateral, so none is sent |
| `cash_equivalent: true` (money-market funds) | kept as a position, stated a money market fund on its resolve, and the cash of its currency sent net of it, units times SnapTrade's `price`, with that provenance (the street counts each asset once). The Statements tab shows the gross cash, the funds and the net. With no price, or a fund worth more than the cash, the statement is withheld rather than a double count |
| `cash_equivalent: true` on a position that is no fund (an `instrument.kind` not mapped to `fund`: a bank deposit, say, of kind `other`) | the cash of its currency, not a holding (the product owner, 2026-10-05). SnapTrade defines the flag as a position "also counted in account cash balance", so the cash row it reports stands for the deposit, never adding it twice; its quantity and market value carry the provenance, derived by the rule "the cash SnapTrade reports, counting a deposit it marks a cash equivalent that is no fund", naming the deposit's symbol and SnapTrade's description as reported. With no price, no cash in its currency, or a deposit worth more than that cash, the statement is withheld, as for a fund |
| a position SnapTrade does not mark, listed in `counted_as_cash` (below) | the cash of the row's currency, not a holding: its units at SnapTrade's `price` added to that currency's cash row (one made where SnapTrade reports no cash in it), derived by the rule "the cash plus a deposit an admin of the plugin counts as cash", naming its symbol and description as reported, and supplied by the person who changed the row, when (its `changed_by`, `changed_at`). A fund stays a fund, whatever lists it; a row whose currency is no ISO 4217 code, or differs from one SnapTrade states for the position, or a position with no price, counts nothing and is said. Never decided from what a symbol looks like |
| every JSON number | a `Decimal` read from its text; a float anywhere is `Decimal(repr(x))` |
| connection `data_freshness_mode.snaptrade` (`realtime` or `delayed`) | how SnapTrade serves the connection: whether the Connections tab offers Refresh (below); unknown when absent |
| (the read) | a statement per account, its ID `snaptrade:<account>:<read time in ns>`, as of `data_freshness.as_of` |

Sync state, first that applies:

| SnapTrade | State | What to do (shown on Connections) |
|---|---|---|
| connection `disabled`, its brokerage `enabled: false` | `disabled`, derived by the rule "SnapTrade disabled the connection and turned its brokerage off" | SnapTrade's to mend: signing in cannot while it has the brokerage off; it serves its last data meanwhile |
| connection `disabled` | `needs_sign_in`, derived by the rule "SnapTrade disabled the connection" | reconnect through SnapTrade's Connection Portal, signing in to the brokerage again; it serves its last data meanwhile |
| holdings `initial_sync_completed: false` | `stale` | wait; nothing is recorded until the first sync is done |
| holdings `holdings_unavailable: true` | `holdings_unavailable` | connect the account another way, or through another venue: holdings will not arrive through this connection, and waiting changes nothing (the product owner, 2026-09-28). Nothing is recorded: an empty list does not mean an empty account |
| connection `data_freshness_mode.institution: delayed` (Interactive Brokers) | `delayed_by_design` | nothing; `stale` if more than four days old |
| holdings older than `stale_after_hours`: the positions' `data_freshness.as_of`, or where SnapTrade gives none, the last successful holdings sync | `stale` | usually SnapTrade's to recover; if it lasts, refresh where Refresh is offered, or reconnect |
| otherwise | `current` | nothing |

SnapTrade is a cache of the brokerage, and this plugin resolves it (the
product owner, 2026-10-03). SnapTrade says a connection's access to the
brokerage has lapsed only by disabling it: its connection carries `disabled`
and `disabled_date`, and no status, reason or code says why (its guide to
fixing one: reconnect through the Connection Portal; the 402 a refresh of a
disabled connection answers carries a `code` and `detail` SnapTrade does not
document). So a disabled connection needs sign-in, by the rule above, unless
SnapTrade has turned the brokerage itself off, the one more specific thing it
says. A state derived by a rule says so in the sync status's detail
(`state derived by the rule "..."`), since the sync status carries no
provenance field.

Holdings freshness is what the read returned is as of: the positions'
`data_freshness.as_of`, when SnapTrade fetched them from the brokerage, or
where it gives none, the last successful holdings sync; the sync status
carries the last successful sync apart, as `last_synced_at_ns`. The
statement is as of the same moment's date, so data SnapTrade serves from its
cache is never presented as the read's. History freshness is the last
successful transactions sync (a date), and how far back the history reaches,
the account's first transaction SnapTrade holds, is sent as `history_from`
(contract v14).

## Settings

Declared through the SDK and given by an admin of the plugin in the
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
| `activity_window_days` | Reported activity: window | number, days | no | no | the SDK's (below): how long the record of each activity SnapTrade reported stays in this plugin's storage, from when it was received; default 2555 (seven years), 1 to 36500, never below the deployment's hold |
| `activity_past_window` | Reported activity: past the window | choice | no | no | the SDK's: `archived`, `kept` or `deleted`; default `archived` where the deployment gives the instance an archive, else `kept`. Never deleted within the history SnapTrade reported, nor inside the hold |
| `responses_window_days` | Raw responses: window | number, days | no | no | the SDK's: how long SnapTrade's responses to each read stay in storage for the Raw responses tab; default 30, 1 to 36500, never below the hold |
| `responses_past_window` | Raw responses: past the window | choice | no | no | the SDK's: `archived`, `kept` or `deleted`, defaulting as above |
| `plan_code_links` | Plan-code links | table: account, plan code, instrument | no | no | each plan's own fund code on an account, linked to the instrument record it is; at most 200 rows. Each row arrives with who changed it and when |
| `counted_as_cash` | Cash links | table: account (external account, optional), symbol (text), currency (text, an ISO 4217 code), the order of Plan-code links' columns | no | no | positions a custodian holds as cash that SnapTrade does not mark a cash equivalent, such as Fidelity's FDIC-insured deposit as an IRA's core position (`FDIC99532`): each is sent as the cash of its currency, naming who listed it and when (above). The symbol as SnapTrade names it; a blank account is every account holding it, and a row naming the account comes first; at most 200 rows. A fund stays a fund. Where SnapTrade marks the position itself, its own flag decides and the row is not read |
| `synthetic` | Synthetic mode | on/off, developer | no | no | serve built-in responses instead of calling SnapTrade; default off |
| `raw_retention_days` | Keep raw responses for (before 0.13.0) | number, days, developer | no | no | 0.12.0's, carried into `responses_window_days` (below) |
| `activity_retention_days` | Keep activity records for (before 0.13.0) | number, days, developer | no | no | 0.12.0's, carried into `activity_window_days` (below) |
| `snaptrade_personal_key` | Personal key (the old way) | on/off, developer | no | no | 0.1.0's way of saying the key's kind; read only while the key type is unset |

A developer's setting is shown in the form only on a development deployment.
The dashboard draws each table setting as its own tab under Manage, titled by
its label: Plan-code links and Cash links, their columns in one order
(Account | Plan code / Symbol | Instrument / Currency; the product owner,
2026-10-05). A row is kept by column name, so one saved before 0.11.1, when
Cash links' account came last, reads the same.

**The windows** (contract v16) are not this plugin's own: the SDK declares
two settings for each kind of raw record a version declares
(`src/snaptrade/declaration.py`: Reported activity and Raw responses), the
same for every edge plugin, named from the kind, and the deployment refuses a
window below its hold and `archived` where a deployment admin has allowed no
archive. Every setting is set in the dashboard's Settings form; no page of
the plugin sets one.

**From 0.12.0.** 0.12.0 kept a read's responses for `raw_retention_days` and
an activity's record for `activity_retention_days`, then deleted them. 0.13.0
carries both into the windows: they stay declared, as developer settings with
no default, so a value a deployment saved under either is still delivered,
and it is the kind's window while the window setting holds its default. The
SDK fills a declared default into what it delivers, so a window holding its
default reads as one never set; set to anything else, the window counts and
the old value does not. A deployment that saved neither, or saved the
default, has nothing to carry: 30 days and 2555 days are the windows'
defaults too. To take a window's default exactly where an old value is
saved, clear the old setting (shown on a development deployment). What 0.12.0
deleted past its retention is not brought back; from 0.13.0 nothing is
deleted unless an admin chooses `deleted`.

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
Every value is invented. Four connections, chosen so each rule has something
to act on: Alpaca (current; long and short stock, an option, a money-market
fund, crypto to nine decimals, cash in two currencies; tax lots on Apple,
the short and Bitcoin, one with no cost or date), Interactive Brokers
(delayed by design; a euro listing whose lots add up to less than it, a
position with no stated currency, negative cash), and Schwab (disabled, so
needing sign-in, with no stable account ID, and no lots), and Fidelity (a
Traditional IRA shaped like the one whose core position is an FDIC-insured
deposit: SYNDP, a deposit SnapTrade marks a cash equivalent and counts in the
USD cash of 10.00, sent as that cash; and SYNFD, 2.99 at a price of 1,
unmarked, sent as a holding until listed in `counted_as_cash`, then added to
the cash, 12.99). SnapTrade serves Alpaca, Schwab and Fidelity in real time
and Interactive Brokers on a delay, so only Interactive Brokers is offered
Refresh; refreshing it in synthetic mode asks
nothing of SnapTrade.

## Its pages

A person opens the plugin from the dashboard's home by a button per level they
hold on it (meridian-design sdk-contract/a-plugin-has-admins, the rulings of
2026-09-30): **Manage** at `admin`, **Open** at `write`, **View** at `read`. A
person may hold `admin` and one of the others; each session carries the one
level chosen. Each page is declared once, with the levels it serves, on the
SDK's `meridian.Pages` (`src/snaptrade/page.py`): the dashboard shows it under
those buttons, and the SDK answers 403 to a session at any other level before
the page's code runs.

| Page | Path | Levels | Button |
|---|---|---|---|
| Connections | `/admin/connections` | `admin` | Manage |
| Account links | `/admin/accounts` | `admin` | Manage |
| Statements | `/` | `write`, `read` | Open, View |
| History | `/history` | `write`, `read` | Open, View |
| Raw responses | `/raw` | `write`, `read` | Open, View |

Setup is under Manage, and daily work under Open and View
(intent/a-custody-plugin-serves-its-statement-receivers, "Admin pages and user
pages"). Being a deployment admin opens no page and reaches no account: a
deployment admin is admin on the plugin through All plugins (admin) or
another grant, and reads an account only through a `read` or `write` grant,
like anybody.

### Statements, under Open and View

**Statements** (`/`) shows, for each account, its sync state (with its
holdings and history freshness), its last statement (as of when, and what
recording it came to) and its rows, as the last read found them, each in an
`om-grid`. It is one page for both buttons, adapting by the session's level
(read from the verified `Meridian-Caller` header):

- It shows each account linked to one of the deployment's accounts the
  person may read (`caller.read`, checked with `caller.may_read`; under Open
  it holds the accounts they may write too), and no other.
- **Under Open** it has **Refresh**, which reads SnapTrade now, and answers
  with Statements saying so. **Under View** it acts on nothing: no form, no
  button.
- **Somebody who may read nothing here** is told plainly that there is
  nothing here for them, and nothing is read for them.

Its head's status dot says how the plugin is reading, as on the pages under
Manage, except that while settings are missing it says SnapTrade is not being
read yet rather than naming them: a reader does not give them. In synthetic
mode it says every figure is invented. Unresolved positions, Discrepancies
and Upload are planned beside it.

What cuts it to a reader is which of the deployment's accounts each external
account is linked to. The plugin reads its links beside its account scope
(`plugin.account_scope()`, W4.11): the first delivery comes at once, and is
held before the pages are served, and another comes whenever a link is made
or removed, or a linked account is renamed or closed. So a reader sees every
account linked to one they may read, before a restart and after it. Nothing
is stored to remember a link: the plugin holds the latest delivery, and
plugins are ephemeral.

Each row shows SnapTrade's average purchase price per unit, as reported
(its `cost_basis` on a position, sent as the holding's `average_cost`), or
"not reported". A cash row standing for a deposit says which, beside it: its
symbol, SnapTrade's description, its units and price, and whether SnapTrade
marks it (counted in the cash already) or a person listed it (added, naming
them and when).

### History, under Open and View

**History** (`/history`) reads an account's activities from SnapTrade when
asked -- buys, sells, transfers, dividends, each with its type, dates,
symbol, units, price, amount and currency as SnapTrade wrote them -- traded
from a start to an end date (at most 366 days at a time; the 30 days to
today by default), a page of at most 1,000 at a time, for an account linked
to one the person may read, chosen by the deployment's account. It says how
far back the account's history goes: the first transaction SnapTrade holds
for it (`first_transaction_date` in its sync status, sent to the street as
`history_from`). Each read is kept with the account's raw responses. What it
shows is SnapTrade's answer to that read; what the street holds of the same
activities is what the reads reported ("The custodian's activity", above).

**Propose lots** (`/history/lots?account=<id>`) reads the account's whole
history (up to 10,000 activities) and, for each position of its last
statement that SnapTrade lists no tax lots for, proposes lots, each naming
its source, never confirmed. A lot is specific (the product owner,
2026-10-04): one purchase as the activity states it.

- **From its purchases** (`BUY`, or `REI`, a reinvested dividend): one lot
  per purchase, its units, the amount paid and its trade date ("SnapTrade
  activities, BUY on 2025-12-02, <activity id>"), where those purchases are
  all of the position and nothing else in its history moved it.
- **Nothing it does not state**: a position its history shows sold from is
  not split into lots by FIFO or any other order; one that arrived by
  transfer, or that a corporate action changed (a split, a stock dividend,
  an adjustment, an option exercised or assigned), gets no lot from its
  history, and says so; nor does one whose purchases do not add up to what
  it holds. A short position and an option get none, and a position
  SnapTrade lists lots for gets nothing proposed beside them.
- **Never from the average purchase price**, which is shown beside each
  position, as reported, for a person typing a cost themselves: nothing
  multiplies a per-unit average by a quantity.

Each proposed lot carries the fields an opening balance's lot takes
(quantity, cost, currency, acquired, source), for the person, or an agent
acting for them, to carry into the opening balance, where they check it and
answer for it. Nothing here sends a lot anywhere.

### Raw responses, under Open and View

**Raw responses** (`/raw`) shows what SnapTrade answered, as received, for
each account Statements shows the person: linked to one of the deployment's
accounts they may read, and no other (the same `caller.may_read`, cut by the
plugin's links rather than by what the last read reached, so a read kept
before a restart shows before the next one). It fits one screen, at 1440×900
and at 390×844 (kit 0.10.0): one account at a time, chosen above (the first
by default), and three views:

- **Read**: the account's latest read, or the one asked for
  (`/raw?account=<id>&read=<read>`): when it was read, and each call it made
  for the account on one line, by name and request (`reading positions`,
  `GET /accounts/{accountId}/positions/all`) and how many entries SnapTrade
  answered, or why the call failed; a click on the line opens SnapTrade's
  JSON body, formatted, every number as SnapTrade wrote it. **Download JSON**
  saves the read as it is kept (`/raw/download?account=<id>&read=<read>`).
- **Kept reads** (`view=kept`): the account's reads in storage and restored
  from the archive, newest first, as many a page as fit (the kit's
  `om-pager`), each opened on the tab or downloaded.
- **Archive** (`/raw/archive`): every unit this plugin moved past its window
  for the accounts the person may read ("The archive", below) -- an account's
  day of raw responses, or its month of reported activity -- each one line:
  the account, the kind, when its records were received, how many, and where
  it stands: in the archive, with **Restore** under Open; restored, readable
  on Read and Kept reads until the date given; or deleted past its window.
  Restore posts to the SDK's route on the plugin's host, `POST
  /archive/restore`, with the kind and the unit, for the person; the
  restored records are read here as any kept read is.

An account the person may not read is answered 404, the same as no such
account, on the tab, the archive and the download alike. A row's reference
to a record that moved (`/raw?ref=<key>`) resolves to where it stands: "in
the archive, and restorable", with Restore under Open; restored; or deleted
past its window. Never to nothing.

Raw responses are an account's data, so the tab and its download are never
served at `admin`: Manage shows no account's data, and the tests hold every
Manage page to that with `PageClient.assert_no_account_data`. Under Open the
head has Refresh, which reads SnapTrade now and answers with this tab; under
View it acts on nothing. Somebody who may read nothing is told so. In
synthetic mode each read says it is synthetic, not SnapTrade.

### Connections and Account links, under Manage

**A plugin admin is account agnostic** (the product owner, 2026-09-30): a
Manage session holds no account's data, and these pages show none of what the
plugin read for an account, no holdings, rows, statements or sync state per
account; only external identities, their links, and how each connection is.
The tests hold every page at `admin` to that with the SDK's
`PageClient.assert_no_account_data`, for a plugin admin and for a deployment
admin (`PageClient(..., deployment_admin=True)`).

- **Summary**, which core draws first under Manage, not a page of the
  plugin's: core's status (the health each read reports, and why), then the
  three figures each read reports with it, which the SDK sends on every
  heartbeat until the next (the product owner, 2026-10-01). **Connections**,
  the count, marked warn when any needs attention, with why (how many are
  stale, needing sign-in, disabled and with holdings unavailable, as the
  Connections page reads each); **Accounts reached**, the count; **Last
  read**, its time, marked an error with why when it failed ("Not yet" before
  any).
- **Connections** (`/admin/connections`): each brokerage connection with its
  health, what to do about it, and refreshing or reconnecting it, and the
  SnapTrade users under the key. **+ Add**
  (named "Add a brokerage connection" for assistive technology), in the
  Connections card's header, connects one through SnapTrade's Connection
  Portal (a link that opens outside the dashboard's frame); **Refresh** sits
  beside it there (below).
- **Account links** (`/admin/accounts`): each account the connections reach,
  who it is (its name, brokerage, type, number and SnapTrade's ID) and its
  link to one of the deployment's accounts, in one account map (below); and
  one line saying how many plan-code links and cash links the plugin's
  settings hold and how long each activity's record is kept, as set and as
  kept (never shorter than the history SnapTrade reported). Each is set in
  the dashboard's Settings form, never here: the tab only shows them.

`/admin` sends a Manage session to Connections. Each page's head shows how the
plugin is reading as a status dot (kit 0.6.0's `om-status`), its note on
hover, focus or a tap: green when the last read succeeded, with when; amber
while a read is under way; red when the last read failed, with its message
(what was asked, the exception's type and the HTTP status, never its text),
or while settings are missing, naming them. Without the kit's script its
words show beside the dot as plain text. Marked for the dashboard
(`data-om-header`, kit 0.7.0), the dashboard draws it beside the plugin's
name; with the heading and any header Refresh drawn there too, the head has
nothing left, so each page starts right under the dashboard's tabs.

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

The page-level Refresh reads SnapTrade now (`/read`). On Connections it sits
beside + Add in the Connections card's header, the kit's plain button beside
its primary one, and is that page's only page-level Refresh (each delayed
connection's own Refresh, above, is another action). Account links has none
(the product owner, 2026-09-30: reading SnapTrade now is Connections'). On
Statements under Open it is in the head, marked as a header action
(`data-om-action="refresh"`, kit 0.4.0) and as the kit's refresh icon
(`data-om-icon="refresh"`, kit 0.8.0: a circular arrow, its words "Refresh"
its name and tooltip): where the dashboard frames the page, it draws Refresh
in its own header as that icon, beside the status dot after the plugin's name
(the product owner, 2026-10-01), and the kit
drops the page's copy; pressing it there presses the page's, so the form still
posts from the page, with its token. Opened on its own, the page shows it in
its head. No credential is entered or shown here: keys are the dashboard's
settings form, and granting access is the dashboard's too.

### Linking accounts

Each plugin links its own external accounts (kernel/a-plugins-admin-view,
point 8), and the link is its right to the account: a statement for an account
nothing links is refused, which the SDK raises as `meridian.NotLinked`, by the
refusal's code and never its words. The Account links tab draws the kit's
`om-account-map` (kit 0.5.0), built for an industrial deployment's hundreds or
thousands of accounts: a dense table, one row per SnapTrade account with its
link, searched by name, ID, account number, custodian and type, filtered to
Unlinked (where it opens, while any are), Linked or All, grouped by
connection, and paged, so the page holds a page of rows however many there
are. It is given no account's status or values (kit 0.6.0's Status column),
which would be an account's data under Manage. Each row stacks into one
column where the map is narrow, as on a phone. Without the kit, each
account's plain row says who it is and its link.

A row's choices open under that row alone. An account not linked is linked
to **an existing account**, found by typing among the deployment's open
accounts read with `read_accounts_for_linking` (identities only, which a
Manage session may read: each with its custodian and type beside its name
where the deployment has them), or, by a deployment admin, to **a new
account** (W6.4: a plugin admin links to any existing account; only a
deployment admin names a new one), named from the SnapTrade account, its
custodian pre-filled from the connection's brokerage and its type from the
account type SnapTrade reports, all three editable, which the conductor
creates and links in one step (an owner and a note are given on the
dashboard's Accounts tab). For anybody else the map is `no-new-account` (kit
0.7.1), offering only existing accounts, and the page draws the plain form to
create one only for a deployment admin; a create sent by anybody else is
answered that only a deployment admin names a new account, and nothing is
sent. A linked
one names the account it is linked to, from the link itself, and offers
**Unlink**, or another account: the conductor keeps one link per external
account, so a new link replaces the one standing.

Each of the deployment's accounts takes one external account (contract v7):
a second link to an account is refused by the conductor, with its reason,
which the page shows as worded ("ACC-3 already has external account ...
linked"). An account one of this plugin's links already names is offered for
no other, in the map or without it; one another plugin links is not known
here, and its refusal is what says so.

Where an unlinked account's name, or its account number, matches exactly one
open account of the deployment's that nothing else is linked to, the map
suggests it in the row; **Link** takes it. The plugin gives the map
SnapTrade's account number where SnapTrade gives it whole (a masked one,
`****3003`, matches nothing, and is left out); the deployment keeps no number
for an account, so a number matches an account named by it. **Link N
suggested…** lists every suggestion the search finds, each to be left out or
kept, and sends them in one form.

Every form posts to one route, `/admin/accounts/link`, at `admin`, saying
what it means in its `intent` field: `link`, `create`, `unlink`, or
`link-several`. A form with any other intent, or none, is refused and nothing
is sent. Each link is sent with `link_external_account`, acting for the admin
viewing the page (their `Meridian-Caller` header as `acting_for`), and the
sidecar refuses it outside a Manage session; a refusal is shown as the sidecar
worded it. The page answering the form waits a few seconds at most for the
account scope to show the new link, so it shows the link as it now stands.

`link-several` carries the token once and then `external_account_id` and
`account_id` repeated, one pair per link, in order. The route refuses the
whole form (400, nothing sent) if the two lists differ in length, name an
account twice or hold an empty ID, and otherwise links each pair as its own
`link_external_account`, a few at a time: one refused leaves the others as
they went, and an account the last read did not reach is not sent. The page
answering says how many were linked, lists each one not linked and why, and
folds every result below; it waits for the account scope to show every link
made, a few seconds at most, and for the links themselves at most 55 seconds
(the SDK's server gives a page 60), after which it says they are still being
sent and finishes them. It takes a body of up to 1 MiB, some thousands of
pairs, the pages' own ceiling (`Pages(max_body=...)`), over which the SDK
answers 413 before any view runs; every other form here takes 8 KB, and a
larger one is refused (413) before anything is done.

Whether an account is linked, and to what, is only what the account scope
says: linked, naming the account, or not linked. There is no third state, and
recorded rows are not taken for a link.

### Forms and the kit

Every action is a plain POST, declared with `@pages.route` at the levels it
serves: reading now (`/read`) at `admin` and `write`; connecting
(`/admin/connect`), refreshing and reconnecting a connection
(`/admin/connections/refresh` and `/admin/connections/reconnect`, the
connection named in the form's `connection_id`) and linking
(`/admin/accounts/link`) at `admin`. Each carries the SDK's CSRF token,
`{{ csrf_input }}` in its form and the account map's `token` attribute, and
the SDK refuses one without it, or with another person's or another
session's, before the view runs. The plugin host has its own session cookie
(decisions/021) on the same site as every plugin's, so without it a page
elsewhere could have a person's browser post here. The token is an HMAC of
the verified caller's subject and the session's level under a random secret
the plugin makes at start and keeps only in memory, so a restart only means
reloading the page.

The pages are built on Open Meridian's plugin UI kit
([meridian-ui](https://github.com/open-meridian/meridian-ui);
spec/plugin-pages-share-one-kit), which the dashboard serves at
`/.meridian/ui/<version>/` on the plugin's own host. Each page is a view in
`page.py` and a Jinja2 template under `src/snaptrade/templates/`, rendered by
the SDK's `pages.render` on the kit's base template, `meridian/base.html`,
which links the kit at 0.8.0 and draws the page's heading and the tab row of
the session's level (dropped when the dashboard frames the page). The
templates use the kit's classes, its `om-grid` for each statement's rows (as
cards where the frame is narrow, each column's hint and blank words declared
as plain JSON, kit 0.3.0's rich cells), its `om-account-map` for linking, its
`om-status` for the head's dot and a `<time>` for each moment, as the kit's
patterns write one, and have no style, colour, theme or script of their own:
each component reads the JSON declared inside it. At a phone's width (390 px)
every page is one column with no sideways scroll: a connection's or an
account's actions drop below its text (`.list-row`), the map's rows stack,
and a statement's rows are cards. The dashboard draws the plugin's name, the
way back and the person, and hands the kit the person's colour scheme and
light or dark. Where the kit is not served the pages still work, unstyled:
each table, and the account map's plain forms, are in the HTML inside the
component that replaces them, and every action is a plain form. Without the
kit the map is a list row per account (who it is, its link, and Unlink on a
linked one) and the forms under them, one to link any account to any open
account and, for a deployment admin, one to create an account for any, so the
page grows with the accounts rather than with the accounts times the
deployment's. Quantities are exact decimal strings, as SnapTrade reported
them.

`make preview` writes each page on synthetic data to `preview/`, served by its
own view: Connections and Account links as a deployment admin sees them under
Manage, and Statements and Raw responses as a reader sees them under View who
may read two of the three accounts (Raw responses with the synthetic read kept
twice, an hour apart, in a temporary directory; its kept reads forty times;
and its archive under Open, thirty days and two months an account moved). They link the kit at `/.meridian/ui/0.10.0/`, so serve them
beside the kit to see them styled; opened on their own they are the pages
without the kit.

## Offered to agents

On SDK 0.17.0 (contract v12) each route declares its inputs as one typed
record (`src/snaptrade/records.py`, `src/snaptrade/history.py`), so the SDK
derives a tool from it on the deployment's MCP surface, at the route's
levels, and `meridian plugin check --verified` holds every route that
changes something to being one:

| Tool | Route | Levels | Does |
|---|---|---|---|
| `read_connections` | `GET /admin/connections` | `admin` | each connection's state, what to do, how SnapTrade serves it |
| `open_connection_portal` | `POST /admin/connect` | `admin` | a Connection Portal link, to connect a brokerage |
| `refresh_connection` | `POST /admin/connections/refresh` | `admin` | ask SnapTrade to read a delayed connection again |
| `reconnect_connection` | `POST /admin/connections/reconnect` | `admin` | a Connection Portal link, to sign in again |
| `read_account_links` | `GET /admin/accounts` | `admin` | each external account, by identity, and its link |
| `link_account` | `POST /admin/accounts/link` | `admin` | link, create and link, or unlink one account |
| `read_statements` | `GET /` | `write`, `read` | each readable account's statement, average price and lots |
| `read_account_activities` | `GET /history` | `write`, `read` | an account's activities over a range |
| `read_proposed_lots` | `GET /history/lots` | `write`, `read` | lots proposed for positions with none |
| `read_snaptrade_now` | `POST /read` | `admin`, `write` | read SnapTrade now |
| `read_archived_units` | `GET /raw/archive` | `write`, `read` | the units moved past their window, each archived, restored or deleted |
| `restore_unit` | `POST /archive/restore` | `write` | the SDK's, on every edge plugin's host: restore an archived unit, by its kind and unit |

`link_account` replaces the tool the Account links form's route would give
(`@pages.tool(replaces=...)`), since the map's form also posts several links
at once. A refusal names each field by its path (`start`, `limit`,
`connection_id`). Kept from agents, each saying why: `/admin`, a browser's
redirect to Connections; Raw responses and its download, SnapTrade's
answers as received, which no typed record holds.

## Raw responses: what SnapTrade said

The product owner, debugging duplicate positions (2026-10-02), asked to see
SnapTrade's raw response on a refresh. Where it lives follows the ruled
separation of duties (meridian-design spec/vendor-differences-have-a-place-
in-the-contract, "Separation of duties"): the sidecar is the normalisation
boundary, core is plugin-agnostic, and an edge plugin keeps the vendor's raw
inputs in its own storage (decisions/028), for recordkeeping, for debugging
a discrepancy, for backfill when the contract gains a field ("Backfill from
raw records"), and as evidence the model lacks something. Core never reads
them, and no other plugin does.

**What is kept.** Each read, for each account it reached, one record: the
read's time; each call made for the account, by the plugin's name for it and
the request it was (the path alone: never a query string, which carries the
user secret, a header or a credential); and SnapTrade's JSON body, read
exactly (every number as written, never a float), or why the call failed.
The connections and accounts lists, answered for every account at once, are
kept as this account's own entry of each, so a record holds one account's
data and no other's. SnapTrade's list of users under the key is no account's
data and is not kept; a read that fails before the accounts are listed keeps
nothing (its error is on the status dot). Before anything is written, a
field named as a credential (a secret, a token, a password, an
authorization, a signature, a client ID or a consumer key) is replaced with
`[redacted]`, and so is the client ID, consumer key or user secret wherever
one appears in a body. The tests hold a hostile body to that, and every kept
byte to having no credential in it.

**Where, and for how long.** In the storage the deployment grants an edge
plugin (decisions/028): the version declares it asks for it, with its two
kinds of raw record and the longest a window may be
(`src/snaptrade/declaration.py`), and the launcher, the
chart and the plugin harness mount a volume for this instance alone at the
path `MERIDIAN_STORAGE_DIR` names, kept across restarts and new versions,
never deleted by the deployment, and reached by no other plugin. The store
(`src/snaptrade/raw.py`) keeps its records under `raw-responses/` there;
where no storage is mounted (a test, a deployment from before it), in
`/tmp/snaptrade/raw-responses`, which lasts as long as the pod and from which
nothing moves. Each record is a gzipped JSON file, `<root>/a-<hash of the
external account ID>/<day>/<read time>.json.gz`, written whole or not at
all; each day is a unit the archive moves.
A store that cannot be written leaves the read as it was; the tab says the
responses were not kept. At the default poll of five minutes that is 288
records an account a day, a few kilobytes each compressed.

**A reported activity's record** (contract v14) is its own: SnapTrade's entry
for the activity, as the read that first reported it received it, with that
read's time and why it was made (the backfill to the account's
`history_from`, or a read), under `<root>/a-<hash>/activities/<month it was
received>/<hash of the activity ID>.json.gz`, written once: reported again,
in storage, restored or moved, it is not written again. It stays in storage
for `activity_window_days` from when it was received (seven years by
default, up to 36,500 days, set in the dashboard's Settings form), and is
never deleted within the history SnapTrade reported: the most days any kept
record's activity was traded before its record was received (the plan's
question 5, and the product owner, 2026-10-05). A few hundred bytes an
activity.

**The archive** (contract v16; meridian-design spec/an-edge-plugins-older-
records-move-to-the-archive). A unit is an account's day of reads or its month
of activity records, moved whole once its day or month has ended and its last
record is past its kind's window, as the kind's past-the-window setting says
(`src/snaptrade/archive.py`), when the settings arrive (at start and on every
change) and after each read:

- **archived**, through the SDK's `archive_unit`: written to the archive a
  deployment admin allowed the instance (mounted at `MERIDIAN_ARCHIVE_DIR`,
  or a cloud's bucket), checked to have landed, the move recorded through the
  sidecar naming the window (`responses_window_days 30`), and only then
  removed from storage. Where the deployment gives no archive, or has not
  allowed one, the unit is kept and the move tried again on the next pass;
- **kept**, left in storage;
- **deleted**, through `delete_unit`, the deletion recorded first, so a
  refusal keeps the unit: inside the deployment's hold the sidecar refuses it
  (REFUSAL_REASON_WITHIN_HOLD), and the plugin says once that the unit is
  kept inside the hold and tries again on each pass, recording nothing.

Each unit is noted first in its account's ledger, `moved.json` beside its
units, so the Archive view lists it and a row's key to a record in it is
found again whatever the move came to; the SDK's index says where it stands.
A restored unit is read from the SDK's restore area in storage for seven
days, after which the SDK returns it, recorded. A unit the index says moved
before is moved again only where it holds the very files the ledger noted
(one recorded before a restart could remove it); otherwise it is kept, with
a warning. A restart's pass finds archived units gone from storage and
refused deletions refused again: nothing is recorded twice. Each pass ends
by saying what storage holds of each kind on the heartbeat (`plugin.stored`),
which the plugin's Summary shows beside what the archive holds. 0.12.0's
flat records are settled into their units, keeping their times, on the first
pass.

**A raw-record reference on every row.** Contract v11 puts on every holding
and statement the raw record it was converted from (`RawRecordRef`: this
instance, which the sidecar fills or checks, and the plugin's own key, opaque
past it). The key here is the external account, the read and the call
(`<external account>/<read>/<positions|balances>`), and an activity's is
`activities/<external account>/<activity ID>`; the Raw responses tab opens
the record and the call a key names (`/raw?ref=<key>`). Nothing is
read back from the records into what is recorded, and a backfill from them
(the contract's for a field a revision adds) is not sent by this version.

**What it receives and does not carry.** By name only, declared with the
version and counted on its heartbeat as each read sees them (the counts stay
in the deployment, on the plugin's Summary): a position's `price` and
`open_pnl`, a lot's `lot_id`, an account's `is_paper`, and an activity's
`fee` and `fx_rate` where it states one, none of which has a meaning in the
contract yet.

## Depends on

The [Python SDK](https://github.com/open-meridian/meridian-python) and nothing
else from Open Meridian, plus SnapTrade's official Python SDK
(`snaptrade-python-sdk`, pinned exactly), which only `src/snaptrade/venue.py`
imports.

The SDK is pinned exactly, `open-meridian==0.21.0`, and the `Dockerfile` and
`Makefile` build on the base image of the same version, `plugin-python:0.21.0`.
0.21.0 is not on PyPI yet: until it is released, `make ci-local` builds it
from the sibling checkout, and CI, which has none, cannot install it.
Where the sibling `meridian-python` checkout carries exactly that version,
one not yet published, the `Makefile` builds from it (`SDK_REPO`): the tests'
and the check's containers install it from source, and the image is built on
a base made from it.
Moving to 0.10.0 from 0.8.0 ran the SDK's migration (`python -m
meridian.migrations --from 0.8.0 --to 0.10.0`, what `meridian plugin migrate`
runs), which rewrote `admin_pages=` to `pages=`; the rest, the pages on
`meridian.Pages` with their levels and templates, was done by hand. 0.10.1
changed nothing the plugin calls; from it, the pages' body ceiling is
`Pages(max_body=...)` and the tests ask as a deployment admin with
`PageClient(..., deployment_admin=True)`. 0.11.0 changed nothing the plugin
calls; from it, each read reports its figures (`plugin.report(...,
figures=...)`), which the tests read as the heartbeat the sidecar receives
(`meridian.testing.heartbeat`), and the health a read reports stands until
the next. Moving to 0.12.0 (contract v7) ran `meridian plugin migrate`, which
moved the pins and rewrote the statement's flat `buying_power=` into
`figures=[StatementFigures(segment="", ...)]`; the statement's
`external_account_id` and `institution`, the net liquidation, a holding's
average cost and lots, and one external account per account on Account links
were done by hand. Moving to 0.13.0 (contract v8, the book of record) ran
`meridian plugin migrate`, which moved only the pins: nothing the plugin
calls changed, and `tests/test_contract.py` now knows what v8 adds, none of
which it sends: the book's operations, reads and deliveries are other roles',
and SnapTrade states no holding's available split or encumbrances and no
statement's security interest. Moving to 0.16.0 (contract v11) from 0.13.0
went through 0.14.0 (the pins only), 0.15.0 (a resolve's `.placeholder`
became `.minted`, the deployment minting its own records; what SnapTrade
states of a security is offered on the resolve) and 0.16.0, whose migration
rewrites nothing: the account's kind in place of its type, the cash sent net
of a fund in place of the "also counted in cash" mark, the provenance of a
currency in place of the "assumed" flag, the settled and pending quantities
from the activities, the raw-record references, the declaration and the
custody suite (`tests/test_suite.py`, every case from SnapTrade's own words,
none declared not presented since 0.9.1) were done by hand.
`meridian.figures.LONGEST_WHY` is `meridian.bounds.PLUGIN_FIGURE_WHY_LENGTH`
now. Moving to 0.17.0 (contract v12) ran `meridian plugin migrate`, which
moved the pins and rewrote nothing, finding the five routes that change
something with no typed record; each was typed by hand, the reads given
typed answers, and `check.yaml` holds the plugin to `meridian` 0.1.30's
`plugin check --verified`. Moving to 0.19.0 (contract v14) from 0.17.0:
the migrations through 0.18.0 (contract v13, tickets) and 0.19.0 move only
the pins, moved by hand; the sync status's `history_from`, the activities
(`plugin.record_activity`), their raw records and the custody suite's
activity cases were done by hand, and `tests/test_contract.py` knows what v13
and v14 add that this plugin does not call (`list_activities`,
`list_sync_statuses`, `receive(activity_recorded=, sync_status_recorded=)`).
Moving to 0.20.0 (contract v15) moved only the pins: the plugin holds one
role, so its pages and settings name none and serve as before (0.11.1's
image ran its `make e2e` unchanged on core 7b0b2a8's v15 runtime and
harness first). 0.20.0's custody suite adds the case a re-resolution answers,
which `plugin.re_resolve_activity` does (above, by hand), and
`tests/test_contract.py` knows `receive(activity_re_resolved=)`, which
custody does not hear. Moving to 0.21.0 (contract v16) moved only the pins:
the two kinds in the declaration, the windows carried over, the moves past
them (`archive.py`) and the Raw responses tab's archive were done by hand.
To move to a new SDK release, change all three together and run
`make ci-local`; `tests/test_contract.py` fails on any operation or parameter
the new SDK has that this plugin does not know, naming it.

`make e2e` also pins a released runtime, `meridian-runtime` by tag and
digest in the `Makefile` (`RUNTIME_IMAGE`), and the plugin harness published
with it (`HARNESS_IMAGE`), which it runs on; below.

## Proven against a released runtime

`make e2e` runs the plugin as it runs in a deployment: its own image, beside
a sidecar, with a broker, the street store and a dashboard, all from the
released `meridian-runtime` image the `Makefile` pins
(`RUNTIME_IMAGE`, `<commit>@sha256:<digest>`; now core's `25fbf37`, contract v16).
That deployment is core's **plugin harness**, published beside the runtime
as its own image of files, `meridian-harness`, at the same commit's tag
(`HARNESS_IMAGE`, pinned by digest too; `/harness`, with its own README).
The target copies it out into `.e2e/`, has the harness write its plugins
from a one-entry list (this plugin as instance `snaptrade`, with the roles
`pyproject.toml` declares, given the harness's archive), and runs it as the compose project
`snaptrade-e2e`, with no published port, so it runs beside anything else.
The harness draws the run's keys and passwords itself, and starts the plugin
again when it stops with an error, as a deployment's pod would: started
before the conductor answers, its first call is refused and it exits. In
synthetic mode and with no key, as an admin would through the dashboard and
this plugin's own page, it:

1. waits until the plugin has registered;
2. turns `synthetic` on in its settings form;
3. opens Account links under Manage until it lists Alpaca's account
   (`SYN-ALP-1001`), which the read on that settings change reached;
4. posts that page's form to create **E2E Alpaca** linked to
   `ALPACA:SYN-ALP-1001`, the one link it makes, which wakes a read that
   records Alpaca's statement;
5. waits until the street store holds a complete statement for E2E Alpaca,
   prints the store with the harness's `store street` and compares it with
   `e2e/expected.street`;
6. asks the dashboard how many accounts the plugin reported and nothing
   links: 3, Interactive Brokers', Schwab's and Fidelity's;
7. waits until the street's activity holds Alpaca's sixteen activities,
   prints it with the harness's `store activity` and compares it with
   `e2e/expected.activity` (contract v14): Alpaca's history backfilled back
   to its `history_from`, 2025-06-02, one activity of each kind and an
   option's expiry as reported, a held security's instrument resolved
   (AAPL, SYNXX, ZZTOP) and a code it no longer holds as reported (SYNT,
   the expired call), units signed by what they did to the account; and
   each connection's latest sync status with its `history_from`, Alpaca's
   as heard before and after its link;
8. reads, inside the plugin's container, what it kept of SnapTrade's raw
   responses for Alpaca's account in the storage the harness grants it
   (`MERIDIAN_STORAGE_DIR`): the latest read's five calls, by name.
   `e2e/plugin.yaml` runs the plugin on a read-only root with `/tmp`
   writable, as the chart's pod has them;
9. grants the harness's admin View on the plugin and opens the Raw responses
   tab at the reference a holding carries
   (`/raw?ref=ALPACA:SYN-ALP-1001/<read>/positions`), which names the call it
   was converted from;
10. makes the plugin's container again and finds that read still kept: the
    granted storage outlives the container it was written from;
11. waits until the new container has backfilled Alpaca's history again,
    every activity answered already recorded, and finds the street's
    activity still `e2e/expected.activity`: nothing recorded twice; and
    finds SYNXX's reinvestment's own raw record in the granted storage by
    the key the activity carries;
12. links IBKR to a new account, **E2E IBKR**, whose first read's backfill
    reports the reinvestment under the plan's own code, `OQKR`, as the code,
    nothing linking it; then sets the table setting `plan_code_links` in the
    plugin's Settings form, linking `OQKR` on IBKR to SYNXX's record
    (contract v14), and finds the Settings tab naming who changed it. IBKR is
    backfilled again, and the street's activity holds the reinvestment as
    first recorded and its re-resolution through the link to SYNXX's record
    beside it (contract v15, `e2e/expected.re-resolved`), the plugin's log
    naming whose link and when; it makes the plugin's container again, whose
    backfill re-resolves nothing, and the two lines are unchanged;
13. links Fidelity's IRA to a new account, **E2E IRA**, whose statement is
    `e2e/expected.ira-marked`: three rows, SYNDP not among them but the USD
    cash of 10.00 that counts it, its quantity and market value derived;
    then lists SYNFD in the table setting `counted_as_cash`, Cash links
    (account `FIDELITY:SYN-FID-4004`, symbol SYNFD, currency USD), and finds the next
    statement `e2e/expected.ira-listed`: two rows, USD cash 12.99, its
    quantity and market value derived and supplied, and no SYNFD (the street
    keeps SYNFD's position as the earlier statement left it, which a reader
    of the account's latest statement takes as absent);
14. keeps a read of Alpaca's as if 40 days old in the plugin's storage, allows
    the plugin an archive on its Manage page, and sets `responses_past_window`
    to `archived`: the plugin archives that day past its window of 30 days,
    the conductor recording the move naming the window (`store moves`), the
    unit in the archive and no longer in storage. The read's reference opens
    on the Raw responses tab as "in the archive, and restorable"; with Open
    granted, the Archive view's Restore posts to the SDK's `POST
    /archive/restore`, the restore recorded for the person, the Archive view
    lists it restored, and the read is read back on the tab;
15. sets `responses_window_days` to 1 with `deleted` past it, then a hold of 30
    days over custody, and keeps a read as if 5 days old: on the next read
    the plugin's deletion is refused inside the hold, the unit kept in
    storage, and no deletion recorded;
16. makes the plugin's container again: its pass keeps the unit inside the
    hold again, and `store moves` is unchanged, nothing recorded twice.

`e2e/expected.street` is every account's rows, so a row for an unlinked
account is a difference, and step 6 proves the plugin reported them. The
harness has no platform, so every instrument is a record the deployment
minted: the file names each by the identifiers the plugin sent (AAPL by its
FIGI and symbol, ZZTOP by symbol only). It holds Alpaca's seven rows, as
`synthetic.py` serves them, each asset counted once: AAPL 12.5 long, ZZTOP
40 short (-40), the AAPL call, SYNXX 500.00, BTC 0.012345678, CAD 200.00
cash, and USD 1023.45 cash, SnapTrade's 1523.45 net of SYNXX; each with its
settled quantity, all of it, Alpaca's trades having settled; a `closed`
line for each value the plugin derived rather than read (every settled
quantity, and the USD cash's quantity and market value); and its statement,
complete with seven rows, with no buying power since Alpaca reports two
currencies.

On a failure it says which step, prints the difference if there is one, and
keeps the components' logs in `.e2e/components.log` and what the runner
printed in `.e2e/runner.log`; it always tears the harness down with its
volumes and removes its copy of it, which `meridian plugin check` would
otherwise read as the plugin's. `make ci-local` runs it, and so the pre-push hook; the `e2e`
workflow runs it on every push and pull request; and `e2e-latest` runs it
weekly against the runtime's and the harness's `latest`, blocking nothing,
to say early that moving the pins will need work. The pins move together by a
deliberate commit, with the SDK's when a contract version changes:

    make e2e RUNTIME_IMAGE=ghcr.io/open-meridian/meridian-runtime:latest \
        HARNESS_IMAGE=ghcr.io/open-meridian/meridian-harness:latest   # try a newer core

## Working on it

    make ci-local        # lint (ruff, mypy strict), tests, plugin check, the plugin's image, and e2e
    make e2e             # the plugin on the plugin harness of the runtime it pins
    make preview         # each page on synthetic data, in preview/, linking the kit
    make install-hooks   # once per clone, so git push runs ci-local first

Everything runs in containers. Put it in a deployment, once a session is open
with `meridian connect`, with `meridian plugin upload` and
`meridian plugin launch snaptrade 0.13.0 --instance snaptrade`; or develop it
live with `meridian plugin dev --instance snaptrade` and `synthetic` on.

A release is the `version` in `pyproject.toml`, raised, with a commit saying
what changed; nothing is published from here. Each deployment takes it into
its own catalogue with `meridian plugin upload`, or `meridian plugin dev
--release` from a live instance, and a version is never replaced.
`AGENTS.md` walks any coding agent through that loop, and through
`meridian plugin check`, which holds the plugin to the framework's rules;
`CLAUDE.md` and the `develop-live` skill under `.claude/` lead Claude Code to it.

## Licence

Apache-2.0, like the SDK it is built on. It is a plugin other vendors will
copy, and it should model the promise that a vendor keeps their plugin.
