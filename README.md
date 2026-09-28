# meridian-snaptrade

An [Open Meridian](https://open-meridian.com) plugin that reads holdings from a
brokerage through SnapTrade and records them in a deployment's street store. It
holds the `custody` role and follows workflow W2, holdings ingestion.

**Not yet implemented.** The Python SDK now carries the typed operations it
needs (`record_holdings_statement`, `record_holding`, `report_sync_status`);
the plugin itself is still to be written.

## What it depends on

The [Python SDK](https://github.com/open-meridian/meridian-python), and nothing
else from Open Meridian. A plugin reaches only its own sidecar, and the SDK is
the whole of that interface. It will start, as every plugin does, from
`meridian plugin new`.

## What it needs to run

A SnapTrade API key, declared as a secret setting of the plugin and set by the
deployment's administrator in the dashboard. The key stays in the firm's own
environment: it is never placed in the plugin's code or image, and never sent to
the platform.

It is brought into a deployment like any other plugin: uploaded to the
deployment's catalogue and launched once an administrator approves the role it
asks for, with `meridian plugin upload` and `meridian plugin launch`.

## Licence

Apache-2.0, like the SDK it is built on. It is a plugin other vendors will
copy, and it should model the promise that a vendor keeps their plugin.
