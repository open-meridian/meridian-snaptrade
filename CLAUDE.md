# snaptrade

A Meridian plugin, in Python on the open-meridian SDK. It runs beside a
sidecar in a Meridian deployment and reaches nothing else: what it may publish
and subscribe to comes from the roles `pyproject.toml` declares under
`[tool.meridian]`, once a deployment admin approves them.

- `src/snaptrade/__main__.py` connects to the sidecar, declares the settings
  (`settings.py`) and serves the admin page (`page.py`, styled by
  `static/page.css`). `venue.py` is SnapTrade behind a small interface and the
  only module importing its SDK; `synthetic.py` stands in for it.
  `normalise.py` turns SnapTrade's shapes into the platform's convention;
  `contract.py` sends them through the SDK and holds everything waiting for
  the account-side contract; `sync.py` carries one read through.
- SnapTrade's vocabulary stops at `normalise.py`, and a number is a `Decimal`
  from the moment it is read, never a float. A credential is never logged,
  shown or put in an exception's text.
- `make ci-local` before calling anything done. The README says what each
  setting does and what waits for the contract.
- To change it and see the change running on a cluster, follow the
  `develop-live` skill (`.claude/skills/develop-live/SKILL.md`): `meridian plugin
  dev` in the background, edit, wait for `ready` at your revision, then check.
- A save changes what the plugin does, never what it is allowed to do. Roles,
  tags and dependencies take a new version, which a person approves.
- This file and `.claude/` are committed with the plugin, for whoever works on
  it next. `CLAUDE.local.md` and `.claude/settings.local.json` are one
  person's own, and git-ignored. `.dockerignore` keeps all of them out of the
  image and the live instance.
