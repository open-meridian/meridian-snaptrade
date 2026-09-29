---
name: develop-live
description: Change this Meridian plugin and see the change running on a development deployment with `meridian plugin dev`, then check it with `open --print`, `logs` and `events`, and release it as a version. Use when editing the plugin's code or page and confirming the result on the cluster.
---

# Developing snaptrade live

The loop is in `AGENTS.md`, at the plugin's root, shared with every coding
agent so the two never drift. Read it and follow it: `plugin dev --json` in
the background with its output under `.meridian/`, a save's revision to
`ready`, the check that answers what you changed, and a release the person
approves. A change to a page is built with the plugin UI kit, as its
"Building its pages" section says.
