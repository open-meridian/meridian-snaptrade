# snaptrade

A Meridian plugin, in Python on the open-meridian SDK, that reads brokerage
accounts through SnapTrade and records them as the custodian's view (W2). It
runs beside a sidecar in a Meridian deployment and reaches nothing else but
that sidecar and SnapTrade: what it may publish and subscribe to comes from
the roles `pyproject.toml` declares under `[tool.meridian]` (`custody`), once
a deployment admin approves them.

- `src/snaptrade/__main__.py` connects to the sidecar, declares the settings
  (`settings.py`) and the pages, and serves them. `page.py` declares each page
  on the SDK's `meridian.Pages`, a view function and a Jinja2 template under
  `templates/` (built on the plugin UI kit the dashboard serves: its classes
  and components, never a colour, spacing or font of its own, and usable
  without the kit; below). Connections and Account links, at `admin`
  (Manage), are setup only; Statements, at `/`, at `write` and `read` (Open
  and View), is daily work: each account linked to one the person may read
  (`caller.may_read`), Refresh under Open alone, and "nothing here for you"
  for somebody who may read none
  (intent/a-custody-plugin-serves-its-statement-receivers); Raw responses,
  at `/raw`, at `write` and `read` too, is what SnapTrade answered each read
  for those same accounts (below); History, at `/history`, at `write` and
  `read`, reads an account's activities over a range from SnapTrade when
  asked, and proposes lots for positions SnapTrade lists none for
  (`history.py`, below). `venue.py` is
  SnapTrade behind a small interface and the only module importing its SDK;
  `synthetic.py` stands in for it. `normalise.py` turns SnapTrade's shapes
  into the platform's convention; `contract.py` sends them through the SDK;
  `sync.py` carries one read through; `linking.py` links an account to one of
  the deployment's, acting for the plugin admin on the Account links tab, and
  holds the plugin's links as `plugin.account_scope()` last delivered them
  (`__main__` holds the first before serving the pages). An account is
  linked, naming its account, or not linked: never guess a third state from
  what a read recorded, and never store the links. A statement names its
  external account and institution, and one for an account nothing links is
  refused before any row: `meridian.NotLinked`; never match a refusal's
  words. Each of the deployment's accounts takes one external account (v7):
  the Account links tab offers none this plugin's links already name, and
  shows the conductor's refusal of a second link as it is worded.
- **SnapTrade's raw responses are kept, per account, by `raw.py`**, the
  edge plugin's own raw records (decisions/028; the separation of duties in
  meridian-design spec/vendor-differences-have-a-place-in-the-contract):
  each read's calls for an account, by name and request, with SnapTrade's
  JSON as received (exact, never a float) or why the call failed, and a list
  call kept as this account's own entry, never another account's. Never a
  credential, a header or a query string: `raw.redact` replaces a field
  named as a credential and any credential value before anything is written.
  Kept for `raw_retention_days` (30 by default), pruned when the settings
  arrive and after each read, in the storage the deployment grants this
  instance (`raw.storage_root()`: `MERIDIAN_STORAGE_DIR`, or `/tmp` where
  none is mounted), which the version's declaration asks for
  (`declaration.py`, contract v11). Every row and statement references the
  raw record it was converted from (`plugin.raw_record(raw.record_key(...))`:
  the external account, the read and the call); never another instance's,
  and never a key the store does not keep. Nothing is read back from the
  store into what is recorded, and the Raw responses tab, an account's data,
  is never at `admin`.
- **The declaration** (`declaration.py`) names the secret settings, what
  SnapTrade sends and this plugin does not carry, by name only, with why,
  and the storage. A name newly left out is declared there, and counted as a
  read sees it (`declaration.seen`); never a value.
- SnapTrade's vocabulary stops at `normalise.py`, its instrument kinds
  mapped to the platform's asset classes there and nowhere else (unmapped
  kinds get none, never a guess), and a number is a `Decimal`
  from the moment it is read, never a float. A credential is never logged,
  shown or put in an exception's text.
- **Send what SnapTrade reported; what it did not, close only by a named
  rule, with its provenance** (the product owner, 2026-10-01; contract v11).
  The settled quantity and the pending by value date come from SnapTrade's
  activities, the cash of a currency is sent net of the money market funds
  SnapTrade counts in it, an unstated currency is the account's only cash
  currency or USD, and an unstated institution is the connection's: each
  carries its provenance (`normalise.Closed`, sent by `contract.py`). SnapTrade
  is a cache of the brokerage, and the plugin resolves it (the product owner,
  2026-10-03): a connection SnapTrade disabled needs sign-in, or is disabled
  where SnapTrade turned its brokerage off, the rule in the sync status's
  detail (it has no provenance field); the holdings are as of what the read
  returned (`data_freshness.as_of`, else the last sync), stale past the
  setting, never as of the read; and no suite case is declared not
  presented. A deposit is cash (the product owner, 2026-10-05): a position
  SnapTrade marks `cash_equivalent` that is no fund is the cash row of its
  currency, which SnapTrade counts it in already, never added twice; one it
  does not mark is cash only where an admin of the plugin lists it in the
  table setting `counted_as_cash` (`counted_as_cash.py`), added to that
  cash, supplied by who changed the row, when. SnapTrade's flag decides
  first, a fund stays a fund, and never from what a symbol looks like. A
  statement that cannot be served clean is withheld
  (`normalise.Withheld`), never sent with a double count. An account's kind is converted from
  SnapTrade's type, or not known with the type as reported; SnapTrade's
  type is never sent as the venue's. Otherwise nothing is derived. A
  position's `cost_basis` is SnapTrade's average per unit, sent as
  `average_cost`, never multiplied into a `cost_basis`, which stays unset; a lot is a `tax_lots` entry, its quantity signed as its holding,
  its cost as reported (sign included), the date part of its purchase date;
  no `tax_lots` is no lots, and a holding with any lot not read exactly sends
  none, with a problem said. The account's `balance.total` is the
  statement's `net_liquidation`, and buying power from exactly one currency
  is its `buying_power`, both in one `StatementFigures(segment="")`; never
  the flat figures, never a margin requirement, never a sum across
  currencies. A number that would not cross the wire exactly (18 places, 38
  digits) is caught in `normalise.py`, before its statement opens.
- **Every route is a tool, or says why not** (contract v12, SDK 0.17.0;
  the product owner, 2026-10-03). Each route declares its inputs as one
  typed record (`params=`, in `records.py` or `history.py`), named as its
  form's inputs; a read declares its typed answer (`answers=`) and answers
  with `pages.answer`; a refusal names each field by its path with
  `pages.refuse`. A route that cannot be a tool says `tool=False, why=`,
  and none that changes something may (`meridian plugin check --verified`,
  which `make check` runs). A new route gets its record and a test calling
  it as a tool (`PageClient.call_tool`).
- **Lots are proposed, never confirmed, and never sent** (the product
  owner, 2026-10-04). `history.py` proposes lots for a position SnapTrade
  lists no tax lots for: from its purchases in the activities where they are
  all of it and nothing else moved it, one lot per purchase (its units, the
  amount paid, its trade date), each naming its source: a lot is specific,
  and never made from the average purchase price, which is shown beside the
  position as reported (W2.3's Q-A binds a proposal too). No FIFO or any other rule SnapTrade does not state; a position
  whose purchases do not account for it (a sale, a transfer, a corporate
  action) gets no lot, and says why; a short or an option gets none. A proposal goes to the
  person or their agent, never into a statement: the street's lots are
  SnapTrade's own, and its `average_cost` is never multiplied there. Every
  history read is kept as a raw record of the account, and named.
- **The custodian's activity, as SnapTrade states it** (contract v14, SDK
  0.19.0; meridian-design plans/the-custodians-activity-explains-a-break).
  `activities.py` converts each of SnapTrade's activities, the one place its
  activity types are read: the plan's kinds (`BUY` purchase ... `JOURNALED`
  journal, `STOCK_DIVIDEND` a corporate action), any other type not known
  with the type as reported; units signed by what they did to the account,
  the amount as SnapTrade signs it, a 0 SnapTrade writes where none applies
  unset, never zero, and no price worked out. The instrument is a held
  security's (resolved by the holding's identifiers), or a person's
  plan-code link with their name as provenance, or the code as reported:
  never a symbol only an activity names resolved, which would mint records.
  `sync.py` reports a linked account's whole history back to `history_from`
  on its first read in the process (the backfill), then each read's; the
  street keeps each once by its ID, so nothing is held to remember a
  backfill by, and a restart's is answered already recorded. Activity is
  evidence, never netted or merged, and nothing is derived from it. Each
  activity's raw record is its own (`raw.activity_key`), written once, kept
  for the `activity_retention_days` setting (seven years by default, at most
  the 36,500 days the storage declaration asks for) and never for less than
  the history SnapTrade reported (`RawStore.kept_for`, `history_reach_days`),
  which the Account links tab says. The plan-code link is the table setting
  `plan_code_links` (`plan_codes.py`; contract v14): per row an external
  account, the plan's code and a deployment instrument record's ID, entered
  in the dashboard's Settings form; an activity under a linked code is that
  record, its provenance who changed the row and when. The plugin only reads
  its settings and sets none (the product owner's option A, 2026-10-05):
  never keep a person's link in the plugin's storage.
- `make ci-local` before calling anything done. The README says what each
  setting does, and how 0.1.0's saved settings still count.
- A save changes what the plugin does, never what it is allowed to do. Roles
  and dependencies take a new version, which a person approves.
- Who may use it is not the plugin's to say (decisions/026, 027). A person
  holds `admin`, `read` or `write` on a plugin, or `admin` and one of the
  others, granted in the deployment's access groups, the same for every
  plugin, and opens it from the dashboard's home by Manage (`admin`), Open
  (`write`) or View (`read`). A session carries the one level chosen, and
  `request.caller` tells a view that level (`caller.level`, `caller.admin`)
  and the accounts it reaches: what the person may read (`caller.read`,
  `caller.may_read`) and write (`caller.write`, `caller.may_write`), both
  under Open, read alone under View, and none under Manage. Being a
  deployment admin opens no page and reaches no account;
  `caller.deployment_admin` is read only to offer a new account when linking
  (W6.4). Declare no `tags`: a plugin has none, and `meridian plugin upload`
  refuses a `pyproject.toml` that names them.
- **Declare each page with the levels it serves**, where its view is:
  `@pages.page(path, title, levels=[...])` for a tab, `@pages.route(path,
  levels=[...])` for anything else. The dashboard shows a page under the
  buttons of its levels, and the SDK refuses any other session before the
  view runs. **A page at `admin` shows no account's data** (the product
  owner: plugin admins are account agnostic): no holdings, rows, statements
  or sync state of an account, only external identities, their links and how
  each connection is; `PageClient.assert_no_account_data` in the tests holds
  every page at `admin` to that, and `PageClient(..., deployment_admin=True)`
  asks as a deployment admin.
- **Manage opens on the plugin's Summary, which core draws**: its status,
  then the figures the plugin reports about its own work. `sync.py`'s
  `figures` makes them from the last read -- Connections (warn, with why,
  when any needs attention), Accounts reached, Last read (an error, with
  why, when it failed) -- and each read reports them with its health
  (`plugin.report(..., figures=...)`); the SDK sends both on every
  heartbeat until the next. Counts and a moment only, never an account's
  data. Build no tiles of these on a page; the tests read them as the
  heartbeat the sidecar receives (`meridian.testing.heartbeat`).

This file is for any coding agent working on the plugin, and is committed with
it for whoever works on it next. It is the canonical one: `CLAUDE.md` and the
`develop-live` skill under `.claude/` lead Claude Code here rather than
repeating it. `CLAUDE.local.md` and `.claude/settings.local.json` are one
person's own, and git-ignored. `.dockerignore` keeps all of them out of the
image and the live instance.

## Checking it: `meridian plugin check`

Whatever in this file can be decided from the text is checked, not only
said: `meridian plugin check` holds the plugin to the rules every Meridian
plugin is built to (the template's shape, `[tool.meridian]`, the kit linked
and no raw colour, nothing from another origin, settings declared rather
than read from the environment, no secret logged or put in a page, the
deployment reached only through the SDK, and tests). It needs no deployment
and no session, and changes nothing. It came in `meridian` 0.1.15; with an
older one, the person runs `meridian upgrade`.

- **Run `meridian plugin check` after each change.** Exit 0: every rule
  holds. Exit 1: at least one does not. Each failure names the rule, the file
  and line, and what to write instead: fix each one as it says, then run it
  again, until it exits 0. Never work around a rule; if one seems wrong for
  this plugin, tell the person.
- **`meridian plugin check --run-tests`** also runs the tests under `tests/`
  with pytest, in the plugin's `.venv` if it has one, otherwise with the
  `python3` on the PATH, which needs the plugin and its dev dependencies
  installed (`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).
  The tests run against stand-ins for the sidecar, SnapTrade's SDK and the
  clock (`tests/conftest.py`); a new operation or page gets a test beside the
  others.
- **`make ci-local`** runs lint (ruff, mypy strict), the tests,
  `meridian plugin check --run-tests`, the plugin's image, and `make e2e`
  (the plugin on the plugin harness the `Makefile` pins beside its runtime, the
  README's "Proven against a released runtime"), in containers, and needs
  nothing installed but Docker. Its check (`make check`) uses the
  meridian that CI's `check.yaml` pins, which may be newer than yours.

What the check cannot decide stays advice, below: which component fits, the
page's layout, custom properties the kit does not define, figures as strings,
and names.

## Building its pages: the kit

Build every page with Open Meridian's plugin UI kit, and nothing else for its
look. Then it looks like the rest of the platform, follows each person's
colour scheme, light or dark, and their market-direction convention, and
needs no design work. The kit is plain CSS and web components: it works from
plain HTML, as the templates write it, and from React, Vue or Svelte alike.

**It is linked for you.** Each page's template, under `templates/`, extends
the SDK's base template, `{% extends "meridian/base.html" %}`, which links the
kit from the path the dashboard serves it at on this plugin's own host, at the
version `page.py`'s `KIT` names (`/.meridian/ui/0.8.0/`), and draws the page's
heading and tab row (the kit drops both when the dashboard frames the page and
draws its own). A template fills three blocks: `content`, the page itself;
`head_actions`, buttons marked `data-om-action="<id>"`, which the dashboard
draws in its header, beside the status dot (here Refresh, on Statements
under Open, marked `data-om-icon="refresh"` so it is the kit's circular-arrow
icon, with `title="Refresh"`; on
Connections it sits beside + Add in the card's header instead); and `status`,
an `<om-status data-om-header>` it draws beside the plugin's name (here how
the plugin is reading). `pages.render("x.html", ...)` renders it, every value escaped, with
`caller` and `level` (admin, write or read) in it; a template adapts by `{% if
level == "write" %}`, and `templates/_kit.html`'s macros (the status dot, a
one-button form, a grid with its plain table, notices, tiles) are shared
between pages. A form that posts puts `{{ csrf_input }}` inside it (a script
sends `request.csrf_token` as `X-CSRF-Token`): every request but GET and HEAD
without this plugin's token is refused before the view runs, so another
site's page cannot act here as the person. A GET changes nothing.

Never copy the kit into the plugin, and never load it, or anything else for
the page, from another origin or a CDN.

**Which component for what:**

| For | Use |
|---|---|
| A table of records | `<om-grid row-key="…">`: set `columns`, then `setRows(rows)`; `upsert(rows)` replaces rows by key in place; figures sort exactly. `narrow="cards"` makes each row a card on a phone; a column's `hint`, `tone: {"field": …}`, `blank` and `strong` are plain JSON |
| A stream: thousands of rows, many changes a second | `<om-grid high-rate>`: only the rows in view are drawn, only changed cells are touched, and they flash up or down; `freeze-sort` stops rows jumping while streaming |
| Live data from the plugin's server | `<om-live src="events" snapshot="snapshot.json" for="grid-id">`: follows server-sent events in sequence, and reads the snapshot again after a gap or a reconnect, so nothing is missed |
| The date the figures are as of | `<om-asof>` to choose one; `<om-moment label="…" value="…ISO…">` to read one |
| How something is doing: read, reading, needs attention or failed | `<om-status state="ok\|busy\|warn\|error" label="…" detail="…" at="…ISO…" at-label="Last read">`: a small coloured dot, a mark per state, its note on hover, focus or a tap; the words inside show without the kit's script. In the page head, `data-om-header` hands it to the dashboard, which draws it beside the plugin's name when the page is framed |
| Linking external accounts to the deployment's (W6.4) | `<om-account-map action="…" token-name="csrf" token="{{ csrf_token }}">`: every form posts to `action` with an `intent` field (`link`, `create`, `unlink`); thousands of accounts are searched, filtered, grouped (`group-by`) and paged; with `link-several`, suggested links are sent in one form, `intent` `link-several` and a pair of IDs per link. An account may carry a `status` and `values` (a Status column), but not here: the map is on a page at `admin`, and those are an account's data |
| Choosing an instrument | `<om-instrument-picker src="…" asof="…">`, searching through the plugin's own server |
| A time series | `<om-chart type="line">` (or `bar`), `series` set in script |
| Several views on one page, resizable and rearrangeable | `<om-panels layout-id="…">`, a `data-panel` child per view; each person's arrangement is remembered |
| Everything else | The kit's classes: `.page`, `.page-head`, `.panel` and `.panel-body`, `.tiles`, `.tabs`, `.field`, `.filters`, `.notice`, `.badge`, `button.primary` and `.danger`, `table` and `.num`, `.empty-state` |

Give a component its data as JSON declared inside it, a
`<script type="application/json">` child, as the templates do for `om-grid`
and `om-account-map` with `{{ data | tojson }}`, which writes `<`, `>` and `&`
in its strings as `\u003c`, `\u003e` and `\u0026`; or set it as properties (`columns`, `rows`,
`series`, `data`) in a `customElements.whenDefined(...)` callback. The kit's
README (open-meridian/meridian-ui) documents every component, attribute and
event.

**Colour.** Only the kit's custom properties, never a hex, `rgb()`, `hsl()` or
a colour's name: not in a stylesheet, an inline style, an SVG or a script. A
person chooses their scheme and their deployment's administrator may add
schemes; a raw colour is one no scheme can change and no contrast check has
seen. Prefer a class to a property, and a property to anything else:

- surfaces `--page`, `--card`, `--hover`; text `--ink`, `--ink-soft`,
  `--ink-faint`; edges `--line`, `--line-soft`, `--line-strong`
- `--accent` and `--accent-wash`; `--primary` and `--primary-ink`
- status, which never changes meaning: `--good`, `--danger`, `--warn-ink`,
  `--violet`, each with its `-wash`
- **market direction:** `--buy` for a buy, a rise or a gain, and `--sell` for a
  sell, a fall or a loss, with `--buy-wash` and `--sell-wash`. Never green and
  red, and never `--good` and `--danger`, for a price move: where a person
  reads red as up (China, Japan, Korea, Taiwan) the kit swaps buy and sell for
  them. In markup, `.buy-ink` and `.sell-ink`, `.badge.buy` and `.badge.sell`;
  in a grid, a column's `tone: "sign"`.

Spacing, radii, shadows and type are the kit's too: `var(--space-1)` to
`var(--space-10)`, `var(--radius)`, `var(--shadow)`, `var(--sans)`,
`var(--mono)`.

**No chrome, no theme code.** The dashboard frames the page and draws the
plugin's name, the way back to the dashboard and the person. The page draws
only its content, in the base template's `content` block: no header bar,
navigation, logo, sign-in or sign-out of its own, and no light and dark
switch. The kit applies the
person's theme, which the frame hands it, with no code of the page's.

**Money is exact.** Send prices and quantities to the page as decimal strings
and show them as sent; never `parseFloat` one. The grid sorts them exactly.

**The kit's rules, which a page keeps too:** no raw colour; no custom
property the kit does not define, other than the page's own layout ones; the
page reaches only its own origin, the plugin's server, which proxies anything
further; figures are strings, never floats.

**Where the kit is not served.** Until every dashboard serves `/.meridian/ui/`,
build pages that work without it, as the templates do: put a plain `<table>`
inside `<om-grid>` and plain forms inside `<om-account-map>` (a browser shows
them as they are, and the kit's component replaces them), and make actions
plain forms. Without the kit the page is unstyled, but everything on it works.

**This plugin's pages.** Every action is a plain POST, declared with
`@pages.route` at the levels it serves, whose form carries the SDK's CSRF
token (`{{ csrf_input }}`), checked before the view runs (the README's
"Forms and the kit"); a new form carries it too. The Account links tab is
the kit's `om-account-map`, whose forms all post to `/admin/accounts/link`
with an `intent`, and which is `no-new-account` for a plugin admin who is no
deployment admin (the handler refuses their `create` regardless); the page
has no grid code of its own, since the grid's rich
cells are declared JSON. `make preview` writes each page on synthetic data to
`preview/`, linking the kit.

A short page, whole: the view, in `page.py`,

```python
COLUMNS = [
    {"key": "symbol", "label": "Instrument"},
    {"key": "quantity", "label": "Quantity", "type": "decimal", "group": True},
    {"key": "market_value", "label": "Market value", "type": "decimal", "group": True},
    {"key": "day_pnl", "label": "Day P&L", "type": "decimal", "group": True, "tone": "sign"},
]


@pages.page("/positions", "Positions", levels=["write", "read"])
def positions(request: meridian.Request) -> str:
    return pages.render("positions.html", grid={"columns": COLUMNS, "rows": []})
```

and its template, `templates/positions.html`:

```html+jinja
{% extends "meridian/base.html" %}
{% block head_actions %}
<om-live src="events" snapshot="positions.json" for="positions"></om-live>
{% endblock %}
{% block content %}
<p class="muted">Every account you may read here.</p>
<section class="panel">
  <om-grid id="positions" row-key="position_id" high-rate sort="market_value:desc" caption="Positions">
    <script type="application/json">{{ grid | tojson }}</script>
    <table><tr><th>Instrument</th><th>Quantity</th><th>Market value</th></tr></table>
  </om-grid>
</section>
{% endblock %}
```

The plugin's server answers `positions.json` with `{ "sequence": "41", "rows":
[…] }` and `events` as server-sent events, each `id: <sequence>` and `data:
{ "rows": […] }` carrying whole records; `om-live` feeds them to the grid.

## Developing it live

On a Meridian deployment installed for development, `meridian plugin dev`
runs this plugin as you write it: each saved file is sent, the plugin's process
restarts on it in the same pod, and it is running again in about a second. The
pod, its sidecar and what it is allowed to do stay as they were.

Everything here runs on the person's own session with the deployment, and is
theirs.

### Before starting

- `meridian --version` is 0.1.21 or later. Older ones open a page at no
  chosen level (`--level` came in 0.1.21), before 0.1.15 have no `plugin
  check`, and before 0.1.3 no `plugin dev`: the person runs `meridian
  upgrade`.
- The person has run `meridian connect` (with the address, for a deployment
  not on this machine). You cannot do it for them: it signs in through their
  browser. `meridian plugin list` says whether the
  session is there: it lists the catalogue, or exits **3**. Whenever any
  command exits 3, the session is missing or has lapsed. Stop, tell the person
  to run the `meridian connect` the command printed, and carry on once they
  have. There is no `meridian status`.
- The deployment was installed for development (`meridian up --development`).
  Elsewhere `plugin dev` is refused, and nothing is wrong with the plugin.
- With the `synthetic` setting on, it runs on invented data shaped like
  SnapTrade's, before any SnapTrade key exists (the README's "Synthetic
  mode").

### Start the loop

The instance is called `snaptrade` below. Use whatever the person
wants it called, and the same name in every command.

**The first time an instance is launched, the person approves what it asks
for.** Show them the `roles` in `pyproject.toml`'s `[tool.meridian]`
and ask. Only once they say yes, pass `--yes`; never pass it to get past a
question they have not answered. An instance that is live already asks
nothing.

Run it in the background, and keep it running for the whole session:

```sh
mkdir -p .meridian
meridian plugin dev --instance snaptrade --yes --json > .meridian/dev.jsonl 2> .meridian/dev.err
```

Its output goes under `.meridian/` on purpose. `plugin dev` sends every file
in this directory that changes, except `.meridian/` and what `.dockerignore`
names. Anywhere else in this directory, its own output would be sent to the
plugin as a change, again and again. Not under your own agent's directory
(`.claude/`, `.cursor/` and the like) either: some agents guard theirs, and
writing there needs a permission a session may not have. `.meridian/` is
git-ignored, since it is this session's, not the plugin's.

`.meridian/dev.jsonl` gets one JSON object a line:

| `event` | Means |
|---|---|
| `seeded` | The live folder was filled from the plugin's image, the first time it runs live |
| `sent` | This process sent a change; `revision` is the number it was given |
| `synced` | The sidecar wrote it |
| `restarted` | The plugin's process started on that revision |
| `ready` | It connected to its sidecar again: that revision is running |
| `crashed` | It stopped with an error. `traceback` has the last of what it printed |
| `exited` | It stopped by itself, without an error |
| `refused` | The sidecar refused it something; `reason` says what |

Wait for the first `ready` before changing anything. `.meridian/dev.err` says
what it is doing, and why it stopped if it did. The first run builds and
uploads the plugin's image, which takes a minute or two.

### Change something

1. Edit and save. There is nothing to run: the save is the deploy.
2. Find the `sent` line after your save, and its `revision`, R.
3. Wait for `ready` or `crashed` at R. Nothing about R is known before then.
4. On `crashed`, read its `traceback`, fix the cause, and save again: the next
   revision replaces it. Nothing needs restarting.

Several saves close together can land as one revision. Read the newest `sent`.

### Check it

Ask the question that answers what you changed:

| To know | Run |
|---|---|
| What the page shows, as the person is served it | `meridian plugin open --instance snaptrade --level view --print /` (any path on the plugin, at the level whose button serves it: `--level manage` for `/admin/connections` and `/admin/accounts`, `open` or `view` for `/` and `/raw`) |
| What the plugin printed or logged since your change | `meridian plugin logs --instance snaptrade --since <R-1>` |
| What the sidecar refused it, or what else happened | `meridian plugin events --instance snaptrade --since <R-1> --json` |
| What the person sees in a browser | `meridian plugin open --instance snaptrade --level manage` (or `open`, `view`): a link one browser opens once, to a session at that level. Give it to the person, or open it in your browser pane |

`--print` exits non-zero when the plugin answers with an error, and prints what
it answered. Prefer it to a browser for checking your own work: it needs no
browser and gives the same page every time.

### What a save cannot change

- **What it is allowed to do.** A `refused` event is its grants working, not a
  bug to code around. Adding a role to `pyproject.toml` changes nothing
  live: it takes a new version, and a person approves it.
- **Its dependencies.** The live code runs on the image the instance was
  launched from. A new package in `pyproject.toml` needs a new version.

Tell the person when a change needs either, rather than looking for a way
round it.

### Release it

When the person is satisfied:

1. Run `meridian plugin check --run-tests` and `make ci-local`, and fix what
   fails until both pass.
2. Raise `version` in `pyproject.toml`. A version is never replaced.
3. Show the person the roles again, and get their yes.
4. Run `meridian plugin dev --release --instance snaptrade --yes`.

That uploads this directory as that version and runs it in place of the live
instance. It is then an ordinary version in the deployment's catalogue.

### Stop

Stop the background `plugin dev`. The instance keeps running, live, as it was.
`meridian plugin stop snaptrade` ends it, which is the person's call.
