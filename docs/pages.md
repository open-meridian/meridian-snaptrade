# Pages

You open the plugin from the dashboard's home by the button for the level
you hold on it: **Manage** (admin), **Open** (write) or **View** (read). Each
page's head has a status dot: green when the last read succeeded, amber
while one is under way, red when it failed or settings are missing, with why
on hover or tap.

## Under Manage: setting it up

Manage shows no account's holdings, statements or activity: only
connections, account identities and their links.

### Summary

Drawn by the dashboard. The plugin's health, then three figures from each
read: **Connections** (marked when any needs attention, saying how many and
why), **Accounts reached**, and **Last read**.

### Settings

The dashboard's form for the key and the timings. See [Setup](setup.md).

### Plan-code links and Cash links

The two link tables, each a tab of its own. See
[Setup](setup.md#4-the-link-tables-only-if-you-need-them).

### Connections

Each brokerage connection: its health and what to do about it.

- **+ Add** connects a brokerage through SnapTrade's Connection Portal.
- **Refresh**, in the card's header, reads SnapTrade now.
- **Refresh** on a connection asks SnapTrade to read that brokerage again.
  Offered only where SnapTrade serves the connection from its cache; it may
  charge for each.
- **Reconnect** opens the Connection Portal to sign in to the brokerage
  again.

It also lists the SnapTrade users under the key.

### Account links

Every account the connections reach, each with its name, brokerage, type,
number and SnapTrade's ID, and its link. Search by any of them, filter to
Unlinked, Linked or All, grouped by connection and paged. Link, create (a
deployment admin), Unlink, or take the suggestions. See
[Setup](setup.md#3-link-accounts).

One line says how many plan-code links and cash links are set, and how long
activity records are kept, as set and as actually kept.

## Under Open and View: daily work

Each page shows only the linked accounts you may read. Under Open, the head
has **Refresh**, which reads SnapTrade now; under View, nothing on these
pages changes anything. Somebody who may read no account here is told so.

### Statements

For each account: its sync state, how fresh its holdings and history are,
its last statement, and its rows. Each row shows SnapTrade's average price
per unit, or "not reported". A cash row that stands for a deposit says which
deposit, and whether SnapTrade marked it or a person listed it (naming
them). An account whose statement was held back shows "Nothing recorded"
and why (see [Troubleshooting](troubleshooting.md#statement-withheld)).

### History

Choose an account and dates (up to 366 days at a time; the last 30 by
default) to see its activity as SnapTrade wrote it: type, dates, symbol,
units, price, amount and currency. It says how far back SnapTrade's history
of the account goes.

**Propose lots** reads the whole history and, for each position SnapTrade
lists no lots for, proposes lots: one per purchase or reinvested dividend,
each naming the activity it came from. Only where those purchases are the
whole position. A position sold from, transferred in, or changed by a split
gets none, and says why. Nothing is sent anywhere: you carry the lots into
an opening balance, where you check them and answer for them.

### Raw responses

What SnapTrade answered, as received: for each account, the latest read,
each call by name with SnapTrade's answer formatted, or why it failed; older
reads below, twenty at a time. **Download JSON** saves a read as kept. Use
it to see exactly what SnapTrade said when a figure looks wrong.
