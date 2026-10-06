# Changelog

What each release of the SnapTrade plugin changed. Releases before 0.11.0
are described in their commits (`git log`) and the README's "Depends on".

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
