# Changelog

What each release of the SnapTrade plugin changed. Releases before 0.11.0
are described in their commits (`git log`) and the README's "Depends on".

## 0.13.0 (not released)

Contract v16, on open-meridian 0.21.0 (meridian-design
plans/an-edge-plugins-older-records-move-to-the-archive, row 6;
tasks/sdk-contract/an-edge-plugins-older-records-move-to-the-archive).

- **Its two kinds of raw record are declared** (`declaration.py`): Reported
  activity (`activity`, a window of 2555 days, seven years, the custody
  guidance of 2026-10-05) and Raw responses (`responses`, 30 days), both
  archivable. The storage asked for stays 36,500 days, the longest a window
  may be.
- **The windows are the SDK's settings**: `activity_window_days`,
  `activity_past_window`, `responses_window_days` and
  `responses_past_window`, declared by the SDK from the kinds, the same for
  every edge plugin, set in the dashboard's Settings form. Past the window a
  record is archived (the default where the instance has an archive), kept
  (otherwise) or deleted.
- **0.12.0's settings carry over without loss.** `raw_retention_days` and
  `activity_retention_days` stay declared, as developer settings with no
  default, so a value a deployment saved under either is still delivered;
  it is `responses_window_days` or `activity_window_days` while that window
  setting holds its default, and the plugin's log names the window it was
  carried from on each move. The SDK fills a declared default into what it
  delivers, so a window at its default reads as unset; set to anything else,
  the window counts and the old value does not. A deployment that saved
  neither, or saved the default, has nothing to carry: 30 and 2555 days are
  the windows' defaults. To take a window's default exactly where an old
  value is saved, clear the old setting (a developer setting, shown on a
  development deployment). The deployment's hold applies to the window
  settings, not the old ones; a deletion inside it is refused either way.
  What 0.12.0 deleted past its retention is not brought back.
- **Past the window, in units, through the SDK's helpers** (`archive.py`):
  an account's day of reads and its month of activity records, moved whole
  once the day or month has ended. Archived with `archive_unit` (written to
  the archive, checked, recorded naming the window, then removed); kept; or
  deleted with `delete_unit`, recorded first, so a deletion inside the
  deployment's hold is refused (REFUSAL_REASON_WITHIN_HOLD) and the unit
  kept, said once, tried again each pass and recorded never. A reported
  activity is never deleted within the history SnapTrade reported. With no
  archive allowed, a unit past its window is kept. A pass runs when the
  settings arrive and after each read; a restart's records nothing twice.
  Each unit is noted first in its account's ledger (`moved.json`), so it is
  listed and a record in it is found again; the SDK's index says where it
  stands.
- **What storage holds goes on the heartbeat**: `plugin.stored`, one span
  per kind, count and first and last received, after each pass.
- **The layout is in units**: a read in its day, an activity's record in the
  month it was received. 0.12.0's flat records are moved into their units on
  the first pass, keeping their times; until then they read as before.
- **The Raw responses tab fits one screen** (kit 0.10.0), one account at a
  time: Read, each call one line with SnapTrade's JSON a click away; Kept
  reads, paged by `om-pager`; and Archive (`/raw/archive`), every unit moved
  for the accounts the person may read, archived, restored or deleted, with
  Restore under Open through the SDK's `POST /archive/restore`. Restored
  reads are read on the tab as any kept read. A row's reference to a record
  that moved resolves to where it stands, with Restore beside it.
- **Offered to agents**: `read_archived_units` (`GET /raw/archive`), and the
  SDK's `restore_unit` on the plugin's host.
- The Account links tab says the activity window and what is done past it.
- `make e2e` on core 25fbf37's runtime and harness (contract v16), the plugin
  given the harness's archive: a read 40 days old archived past its window,
  recorded, its reference resolving to archived, restorable, restored on the
  tab through the SDK's route for the person and read back; a read 5 days
  old, past a window of a day with `deleted` chosen, kept inside a hold of
  30 days with nothing recorded; and a new container's pass recording
  nothing twice. `make preview` writes the tab's kept reads and archive too.

## 0.12.0 (not released)

Contract v15, on open-meridian 0.20.0 (meridian-design
plans/access-is-granted-per-role, build 7; tasks/sdk-contract/an-activity-is-
re-resolved-when-its-instrument-resolves).

- **Access per role needs nothing of this plugin.** It holds one role,
  `custody`, so its pages and settings name none and serve as before. 0.11.1's
  image ran its `make e2e` unchanged on core 7b0b2a8's v15 runtime and
  harness before the pin moved.
- **A plan code linked later re-resolves the earlier activity.** When a
  settings delivery adds or changes a Plan-code links row, the account is
  backfilled again, and each activity the street already holds under the
  linked code is re-resolved through the link (`ReResolveActivity`, W2.15):
  the instrument record chosen, who added or last changed the row, and when
  the row says. The street keeps the activity as first recorded and the
  re-resolution beside it; the start's backfill re-resolves whatever a
  current link now resolves, and the street answers already recorded what it
  holds so, so a restart or a delivery again re-resolves nothing twice. A
  row that says no time re-resolves nothing, saying why; a row removed sends
  nothing.
- The custody suite's v15 case, `activity-re-resolved-when-a-plan-code-is-
  linked-later`, runs from SnapTrade's own words.
- `make e2e` on core 7b0b2a8's runtime and harness (contract v15): IBKR is
  linked first, its reinvestment under OQKR reported as the code, then OQKR
  is linked in the settings, and the street holds the reinvestment as first
  recorded with its re-resolution beside it (`e2e/expected.re-resolved`); a
  new container re-resolves nothing twice.

## 0.11.1 (not released)

The two table settings read alike (the product owner, 2026-10-05). Nothing
the plugin sends or reads changed.

- **`counted_as_cash` is labelled Cash links**, beside Plan-code links: the
  dashboard draws each table setting as its own tab under Manage, titled by
  its label. The setting's key is unchanged.
- **One column order for both:** Account | Plan code / Symbol | Instrument /
  Currency. Cash links' account moves first and stays optional, a blank one
  applying the row to every account holding the symbol, as its description
  now says. A row is kept by column name, so one saved in 0.11.0's order
  (symbol, currency, account) reads the same.
- **No page sets a setting:** the README no longer says
  `activity_retention_days` is also set on the Account links tab, or that a
  view refuses a value. Every setting is set in the dashboard's Settings
  form; the Account links tab only shows what they hold, now counting the
  cash links too.
- `make e2e` on core 7d4ce0f's runtime and harness.
- **Its documentation, in `docs/`**: an overview, setup (the key, connecting
  a brokerage, linking accounts, Plan-code links and Cash links), each page,
  troubleshooting by state, and terms, in plain Markdown ahead of the format
  a plugin's documentation will ship in. No reference is hand-written: that
  part will be generated from the declaration.

## 0.11.0 (not released)

Contract v14, on open-meridian 0.19.0 (meridian-design
plans/the-custodians-activity-explains-a-break, build 5).

- **The custodian's activity reaches the street.** Each activity on a linked
  account is reported as SnapTrade states it (W2.10): on the account's first
  read in a process, a backfill of every activity SnapTrade holds back to its
  `history_from`; on each read after, the activities that read fetched. The
  street keeps each once by SnapTrade's activity ID, so a restart's backfill
  records nothing twice.
- **SnapTrade's types mapped to the closed list:** `BUY` purchase, `SELL`
  sale, `REI` reinvestment, `DIVIDEND`, `INTEREST`, `FEE`, `TAX`, `SPLIT`,
  `STOCK_DIVIDEND` corporate action, the transfers in and out,
  `CONTRIBUTION`, `WITHDRAWAL`, `JOURNALED` journal; anything else not known,
  with the type as reported. Units signed by what they did to the account; a
  value SnapTrade writes as 0 where none applies sent unset.
- **The instrument:** a held security resolved by its holding's
  identifiers; a plan's own fund code linked by a person resolved as the
  linked symbol, with that person as its provenance; any other code as
  reported.
- **The plan-code link** (the product owner's option A, 2026-10-05): the
  table setting `plan_code_links` -- per row an external account, a plan's
  own code and a deployment instrument record -- entered by an admin of the
  plugin in the dashboard's Settings form as an editable typed table. An
  activity under a linked code is that record, naming who changed the row
  and when. The plugin only reads its settings; no page sets one.
- **`activity_retention_days`** (the product owner, 2026-10-05): how long
  each reported activity's raw record is kept, seven years by default, an
  admin of the plugin may set it longer (up to 36,500 days, which the
  storage declaration now asks for) on the dashboard's Settings form; never
  shorter than the history SnapTrade reported, which a shorter value is kept
  to, as the Account links tab says.
- **A custodian's deposit is cash** (the product owner, 2026-10-05;
  meridian-design tasks/sdk-contract/snaptrade-counts-a-core-deposit-as-cash).
  A position SnapTrade marks `cash_equivalent` that is no fund (Fidelity's
  FDIC-insured deposit as an IRA's core position, say) is sent as the cash
  of its currency, not a holding: SnapTrade counts what it marks so in that
  cash already, so the cash row stands for it and nothing is added twice,
  derived by a named rule with the deposit's symbol and description as
  reported. A money market fund stays a fund, its cash net of it as before.
- **`counted_as_cash`**, a table setting (symbol, currency, optionally the
  account), set by an admin of the plugin on the dashboard's Settings form:
  a position SnapTrade does not mark, listed there, is added to the cash of
  the row's currency, derived by a named rule and supplied by the person who
  changed the row, when. SnapTrade's own flag decides first; a fund stays a
  fund; a row that cannot count (no currency code, a currency SnapTrade
  contradicts, no price) is said, and the position sent as it is. Never
  decided from what a symbol looks like.
- Synthetic Fidelity: a Traditional IRA with a deposit SnapTrade marks a
  cash equivalent (SYNDP) and one it does not (SYNFD, 2.99 at 1); `make e2e`
  links it, then lists SYNFD, and its next statement is USD cash 12.99 and
  no SYNFD.
- **The sync status carries `history_from`**, the first date SnapTrade's
  history of the account reaches.
- **Raw-record retention covers what was reported:** each reported
  activity's raw record is its own, `activities/<external account>/<activity
  ID>`, kept for `activity_retention_days` from receipt (seven years by
  default), and the Raw responses tab opens it by that reference. Each read's
  records keep `raw_retention_days`.
- An activity's `fee` and `fx_rate` are declared as received and not carried,
  and counted as reads see them.
- Synthetic Alpaca has a history: one activity of each kind and an option's
  expiry, on fixed dates from 2025-06-02; synthetic IBKR a reinvestment
  under a plan's own code (OQKR).
- `make e2e` on core 1d16c52's runtime and harness: the street's activity
  is `e2e/expected.activity`, a new container's backfill records nothing
  twice, and after OQKR is linked to SYNXX's record in the Settings form,
  IBKR's first read reports its reinvestment under OQKR as that record.
