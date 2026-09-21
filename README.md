# meridian-snaptrade

A Meridian plugin that reads holdings from a brokerage through SnapTrade and
records them in a deployment's street store. It holds the `custody` role and
follows workflow W2, holdings ingestion.

**Not yet implemented.** It is waiting on typed sidecar operations
(meridian-design `sdk-contract/typed-sidecar-operations`, per decision 008), so
that it can be built on the Python SDK alone rather than importing the wire
schema to construct messages itself.

## What it depends on

The [Meridian Python SDK](https://github.com/open-meridian/meridian-python), and
nothing else from Meridian. A plugin reaches only its own sidecar, and the SDK
is the whole of that interface.

## What it needs to run

A SnapTrade API key, supplied by the deployment's administrator as a Kubernetes
Secret named in the plugin's chart entry. The key stays in the customer's
cluster: it is never placed in chart values and never sent to the Meridian
platform.

## Licence

Apache-2.0, like the SDK it is built on. This is the reference plugin other
vendors will copy, and it should model the promise that a vendor keeps their
plugin.
