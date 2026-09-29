# snaptrade

A Meridian plugin, in Python on the open-meridian SDK. It runs beside a
sidecar in a Meridian deployment and reaches nothing else: what it may publish
and subscribe to comes from the roles `pyproject.toml` declares under
`[tool.meridian]`, once a deployment admin approves them.

- `src/snaptrade/__main__.py` connects to the sidecar, declares the settings
  (`settings.py`) and serves the admin pages, Connections, Accounts and
  Holdings, which the dashboard shows as tabs (`page.py`, built on the plugin
  UI kit the dashboard serves: its classes and components, never a colour,
  spacing or font of its own, and usable without the kit). `venue.py` is
  SnapTrade behind a small interface and the only module importing its SDK;
  `synthetic.py` stands in for it. `normalise.py` turns SnapTrade's shapes
  into the platform's convention; `contract.py` sends them through the SDK;
  `sync.py` carries one read through; `linking.py` links an account to one of
  the deployment's, acting for the admin on the Accounts tab.
- SnapTrade's vocabulary stops at `normalise.py`, and a number is a `Decimal`
  from the moment it is read, never a float. A credential is never logged,
  shown or put in an exception's text.
- `make ci-local` before calling anything done. The README says what each
  setting does, and how 0.1.0's saved settings still count.
- To change it and see the change running on a cluster, follow the
  `develop-live` skill (`.claude/skills/develop-live/SKILL.md`): `meridian plugin
  dev` in the background, edit, wait for `ready` at your revision, then check.
- A save changes what the plugin does, never what it is allowed to do. Roles
  and dependencies take a new version, which a person approves. A plugin
  declares no `tags` (decisions/026): who may use it is the deployment's
  access groups', at `read` or `write`, and `meridian plugin upload` refuses
  a `pyproject.toml` that names them.
- This file and `.claude/` are committed with the plugin, for whoever works on
  it next. `CLAUDE.local.md` and `.claude/settings.local.json` are one
  person's own, and git-ignored. `.dockerignore` keeps all of them out of the
  image and the live instance.
