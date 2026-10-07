# Troubleshooting

Each connection's state, and what to do, is on **Connections**; each
account's is on **Statements**. Raw responses shows what SnapTrade actually
said.

## Settings missing

The status dot is red and names the missing settings; nothing is read.
Enter them on **Manage → Settings** ([Setup](setup.md#1-enter-the-key)). A
reader sees only that SnapTrade is not being read yet.

## Needs sign-in

SnapTrade disabled the connection: its access to the brokerage lapsed.
SnapTrade gives no reason. It keeps serving its last data meanwhile, so
figures stop moving.

**Do:** **Reconnect** on Connections, and sign in to the brokerage again in
the Connection Portal. Meanwhile an operations plugin may flag the account's
statement as missing, saying it needs sign-in.

## Disabled

SnapTrade has turned the brokerage itself off. Signing in cannot fix it
while it is off; it is SnapTrade's to mend. Its last data is served
meanwhile.

## Stale

Either SnapTrade's first sync of a new connection is not done (nothing is
recorded until it is), or the holdings are older than **Stale after**.

**Do:** wait first: it is usually SnapTrade's to recover. If it lasts,
**Refresh** the connection where offered, or **Reconnect**.

## Delayed by design

Some brokerages are served by SnapTrade on a delay; Interactive Brokers, for
one, can be up to a business day late. Nothing is wrong, and nothing needs
doing. It turns stale after four days.

## Holdings unavailable

SnapTrade says holdings will not arrive through this connection. Waiting
changes nothing, and nothing is recorded: an empty list does not mean an
empty account.

**Do:** connect the account another way, or through another plugin.

## No Refresh on a connection

The connection is real-time: SnapTrade reads the brokerage on every call, so
there is nothing to refresh. On some SnapTrade plans a manual refresh is
refused outright; the page says so.

## Statement withheld

The account shows "Nothing recorded" and why. The plugin holds back a
statement rather than count an asset twice. It happens when SnapTrade counts
a fund or a deposit in the cash as well, and either gives no price for it or
values it above the cash it is counted in.

**Do:** read the account's latest answer on **Raw responses**. Nothing on
your side fixes it; the next read that comes back clean records as usual.
If it lasts, raise it with SnapTrade.

## An account readers cannot see

It is not linked, or they hold no grant on the account it is linked to.
Link it on **Account links**; a deployment admin grants read or write.

## An account appears again as new

Some brokerages give no stable account ID, so after a reconnect SnapTrade's
account is new. Link it again, and unlink the old one if it is still listed.

## A link refused

"ACC-3 already has external account … linked": each deployment account takes
one external account. Unlink the other first, or link to another account.

## Activity with no instrument

A code SnapTrade lists for a security the account no longer holds travels as
SnapTrade wrote it, with no instrument. If it is a retirement plan's own
fund code, add a row on **Plan-code links**: the activity already recorded
under it is re-resolved on the next read.

## A deposit shown as a holding

SnapTrade does not mark it as cash. Add a row on **Cash links**
([Setup](setup.md#cash-links)).

## No lots, or "not reported"

SnapTrade gives tax lots only for some brokerages, and with a paid add-on of
SnapTrade's. **Propose lots** on History may offer lots from the purchases;
otherwise lots come from your own records when you complete an opening
balance.

## Raw responses not kept

Raw responses says the responses were not kept: the plugin's storage could
not be written. What was recorded is unaffected; tell your deployment's
admin.

## A record is in the archive

A link to a record says it is in the archive, and restorable: it moved past
its window. **Do:** under **Open**, press **Restore** beside it (or on the
Archive view); it is readable for seven days.

## Old records are not moving

Records past their window stay in storage when the deployment gives this
plugin no archive, or its admin has not allowed one, or the choice past the
window is Kept. **Do:** ask your deployment's admin to allow an archive on
the plugin's Manage page, or choose Kept or Deleted on Settings. A deletion
inside your deployment's hold is refused and the records stay: that is the
hold working.

## A difference in your book

Differences between your book and what this plugin records are shown and
resolved in your operations plugin, not here.

**An example: a transfer between two of your accounts.** Cash moved from one
account to another at the same brokerage shows here as two activities, one
out and one in, and each account's next statement shows the new cash. Your
operations plugin raises a break on each account. The sample operations
plugin does not yet match a transfer to its activity, so a person resolves
both breaks by hand, each with its reason. See that plugin's
troubleshooting.

## Still stuck

Tell your deployment's admin, with the account, the time and the Raw
responses entry to hand.
