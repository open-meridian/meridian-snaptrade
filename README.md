# meridian-snaptrade

An [Open Meridian](https://open-meridian.com) plugin that reads brokerage
accounts through [SnapTrade](https://snaptrade.com): their positions, their
cash in each currency, and how fresh SnapTrade's data about them is. It records
them in a deployment's street store as the custodian's view, following
workflow W2 (holdings ingestion). It holds the `custody` role. This is release
0.5.3. How a plugin like it is built is documented at
[open-meridian.dev](https://open-meridian.dev).

It is built on the SDK it pins, `open-meridian==0.10.1`, which declares
contract v5 and carries the whole account-side contract
(spec/the-account-side-fits-every-venue): a holding's side, a market value
left unset, a currency marked as assumed, a fund marked as counted in cash
too, buying power on the statement, the sync state with holdings and history
freshness, the accounts a connection reaches, linking them, and reading its
own links; and the access-by-button model (sdk-contract/a-plugin-has-admins):
its pages, each declared with the levels it serves, opened by the dashboard's
Manage, Open and View. An ambiguous miss carries the asset class SnapTrade's
instrument kind maps to, or none where it maps to none.

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
   ambiguous one is recorded unresolved and its miss published once (W3.2),
   with the instrument's asset class where its kind gives one.
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
| `instrument.kind` | the asset class an ambiguous miss is reported with (the product owner, 2026-10-01): `stock` is `equity`; `etf` and `mutualfund` are `fund`; `bond` is `debt`; `option` is `derivative`; `crypto` is `crypto_asset`. Any other kind (`adr`, `cef`, `future`, `future_option`, `cfd`, `tokenized_asset`, `other`, or one SnapTrade adds) is sent with no class, for a person to set on the platform; so is cash, which is no instrument SnapTrade reports |
| balance `buying_power` per currency | the statement's buying power, as reported, never derived |
| `cash_equivalent: true` (money-market funds) | kept as a position and marked; SnapTrade counts it in cash too |
| every JSON number | a `Decimal` read from its text; a float anywhere is `Decimal(repr(x))` |
| connection `data_freshness_mode.snaptrade` (`realtime` or `delayed`) | how SnapTrade serves the connection: whether the Connections tab offers Refresh (below); unknown when absent |
| (the read) | a statement per account, its ID `snaptrade:<account>:<read time in ns>`, as of `data_freshness.as_of` |

Sync state, first that applies:

| SnapTrade | State | What to do (shown on Connections) |
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

### Connections and Account links, under Manage

**A plugin admin is account agnostic** (the product owner, 2026-09-30): a
Manage session holds no account's data, and these pages show none of what the
plugin read for an account, no holdings, rows, statements or sync state per
account; only external identities, their links, and how each connection is.
The tests hold every page at `admin` to that with the SDK's
`PageClient.assert_no_account_data`, for a plugin admin and for a deployment
admin (`PageClient(..., deployment_admin=True)`).

- **Connections** (`/admin/connections`): figures for the read (connections
  and how many need attention, accounts reached, the last read), each
  brokerage connection with its health, what to do about it, and refreshing
  or reconnecting it, and the SnapTrade users under the key. **+ Add**
  (named "Add a brokerage connection" for assistive technology), in the
  Connections card's header, connects one through SnapTrade's Connection
  Portal (a link that opens outside the dashboard's frame); **Refresh** sits
  beside it there (below).
- **Account links** (`/admin/accounts`): each account the connections reach,
  who it is (its name, brokerage, type, number and SnapTrade's ID) and its
  link to one of the deployment's accounts, in one account map (below).

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
(`data-om-action="refresh"`, kit 0.4.0): where the dashboard frames the page,
it draws Refresh in its own header, beside the plugin's name, and the kit
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
which would be an account's data under Manage. Without the kit, each
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
dashboard's Accounts tab). The page offers the plain form to create one only
to a deployment admin, and a create sent by anybody else is answered that
only a deployment admin names a new account, and nothing is sent. A linked
one names the account it is linked to, from the link itself, and offers
**Unlink**, or another account: the conductor keeps one link per external
account, so a new link replaces the one standing.

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
which links the kit at 0.7.0 and draws the page's heading and the tab row of
the session's level (dropped when the dashboard frames the page). The
templates use the kit's classes, its `om-grid` for each statement's rows (as
cards where the frame is narrow), its `om-account-map` for linking, its
`om-status` for the head's dot and its `om-moment` for when SnapTrade was last
read, and have no style, colour, theme or script of their own: each component
reads the JSON declared inside it. The dashboard draws the plugin's name, the
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
Manage, and Statements as a reader sees it under View who may read two of the
three accounts. They link the kit at `/.meridian/ui/0.7.0/`, so serve them
beside the kit to see them styled; opened on their own they are the pages
without the kit.

## Depends on

The [Python SDK](https://github.com/open-meridian/meridian-python) and nothing
else from Open Meridian, plus SnapTrade's official Python SDK
(`snaptrade-python-sdk`, pinned exactly), which only `src/snaptrade/venue.py`
imports.

The SDK is pinned exactly, `open-meridian==0.10.1`, and the `Dockerfile` and
`Makefile` build on the base image of the same version, `plugin-python:0.10.1`.
Moving to 0.10.0 from 0.8.0 ran the SDK's migration (`python -m
meridian.migrations --from 0.8.0 --to 0.10.0`, what `meridian plugin migrate`
runs), which rewrote `admin_pages=` to `pages=`; the rest, the pages on
`meridian.Pages` with their levels and templates, was done by hand. 0.10.1
changed nothing the plugin calls; from it, the pages' body ceiling is
`Pages(max_body=...)` and the tests ask as a deployment admin with
`PageClient(..., deployment_admin=True)`.
To move to a new SDK release, change all three together and run
`make ci-local`; `tests/test_contract.py` fails on any operation or parameter
the new SDK has that this plugin does not know, naming it.

## Working on it

    make ci-local        # lint (ruff, mypy strict), tests, plugin check, and the plugin's image
    make preview         # each page on synthetic data, in preview/, linking the kit
    make install-hooks   # once per clone, so git push runs ci-local first

Everything runs in containers. Put it in a deployment, once a session is open
with `meridian connect`, with `meridian plugin upload` and
`meridian plugin launch snaptrade 0.5.3 --instance snaptrade`; or develop it
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
