"""The SnapTrade plugin for Open Meridian: holdings, cash and sync state from
brokerages through SnapTrade, recorded through the Python SDK (role `custody`).

- venue.py: SnapTrade behind a small interface; synthetic.py: its stand-in.
- normalise.py: SnapTrade's shapes in the platform's convention.
- contract.py: what reaches the sidecar, and what waits for the contract.
- sync.py: one read, carried through; page.py and templates/: its pages.
- raw.py: SnapTrade's raw responses to each read, kept per account.
"""
