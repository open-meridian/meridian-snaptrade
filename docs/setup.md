# Setup

Four steps, each by an **admin of this plugin** (a deployment admin is one,
through the All plugins (admin) group, unless that was withdrawn). The plugin
must already be launched, its role approved by a deployment admin.

## Before you start: which key

SnapTrade issues one of two kinds of key:

- **Personal key.** Belongs to one SnapTrade user: you. For your own
  accounts.
- **Commercial key.** Your firm registers SnapTrade users of its own under
  it, each with a secret. One instance of this plugin reads one of those
  users' connections.

Which one you may use, and for whose accounts, is SnapTrade's to say: see
[Terms](terms.md).

## 1. Enter the key

**Manage → Settings.** The key is entered here and nowhere else.

1. **Key type**: Personal or Commercial. It decides which fields follow.
2. **Client ID**, from the SnapTrade dashboard's API keys page.
3. **Consumer key**, shown once, when SnapTrade issues the key.
4. With a commercial key, the **User ID** of the SnapTrade user to read, and
   its **User secret**.
5. **Save.** The plugin picks it up without a restart.

Secret fields are write-only: they show only "set" or "not set", and nobody
reads them back. Until every required field is given, the plugin calls
nothing and its status dot names what is missing.

The other settings have sensible defaults: how often to read (**Read
every**, five minutes), when a sync counts as **Stale after** (24 hours), and
how long raw responses (30 days) and activity records (seven years) are kept.
An activity record is never kept for less than the history SnapTrade
reported.

## 2. Connect a brokerage

**Manage → Connections → + Add.** This opens SnapTrade's Connection Portal,
outside the dashboard. Choose the brokerage and sign in to it there:
SnapTrade holds that sign-in, and your deployment never sees it.

Back on Connections, **Refresh** reads SnapTrade now. Nothing is recorded
for a new connection until SnapTrade's first sync of it is done; until then
it shows as stale.

## 3. Link accounts

**Manage → Account links.** Every account the connections reach, opening on
the unlinked ones. For each:

- **Link** it to one of your deployment's accounts, found by typing; or
- a deployment admin may **create a new account** from it, named after it,
  its custodian and type filled in, all editable.

Where an account's name or number matches exactly one of yours, the row
suggests it: **Link** takes it, and **Link N suggested…** takes several at
once. Each deployment account takes one external account.

Nothing is recorded for an account until it is linked. Then a deployment
admin grants people read or write on it, so they see it under View or Open.

## 4. The link tables, only if you need them

Both are tabs under Manage, each an editable table, up to 200 rows. Each row
records who added or last changed it, and when.

### Plan-code links

**Use it when** a retirement plan, such as a 401(k), names a fund in its
activity by the plan's own code rather than a public symbol, so that
activity arrives with no instrument.

A row is the **account**, the **plan code** as SnapTrade names it, and the
**instrument** record it is, found by search. From then on, that code's
activity is that instrument, naming who linked it.

Activity that arrived before you added the row is re-resolved too: on the
next read, each earlier activity under that code on that account is
recorded as that instrument, naming who linked it and when, beside the
activity as it first arrived, which is kept. Changing the row re-resolves
them again.

### Cash links

**Use it when** a custodian holds cash as a position SnapTrade does not mark
as cash, such as an FDIC-insured bank deposit held as an IRA's core
position. It then arrives as a holding.

A row is the **account** (leave it blank for every account holding the
symbol), the **symbol** as SnapTrade names it, and the **currency** as an
ISO code, such as USD. From the next read, the position is counted as cash
in that currency, naming who listed it.

- Where SnapTrade marks the position as cash itself, you need no row.
- Never list a money market fund: a fund stays a fund.
- Adding a row changes the next statement: the position goes and the cash
  rises. Your operations plugin shows that as a difference for a person to
  confirm.

## Agents

An agent you delegate to can connect a brokerage, link accounts and read
what you can, through the deployment's tools; the reference lists them. The
key is entered only on the Settings form.

## Synthetic mode

On a development deployment, **Synthetic mode** serves invented data shaped
like SnapTrade's, so the plugin can be tried before any key exists. Every
figure it shows is invented, and its pages say so.
