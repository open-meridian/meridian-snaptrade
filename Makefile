SHELL := /bin/bash

# A pre-push hook runs with GIT_DIR naming this repository; nothing here runs
# git on the host, and this keeps it that way should something start to.
unexport GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR \
         GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX

.PHONY: help ci-local ci-local-deep ci-remote test lint check image e2e preview fmt install-hooks

DOCKER      := DOCKER_BUILDKIT=1 docker
PY_VERSION  := 3.12
CHECK       := meridian-snaptrade-check
IMAGE       ?= snaptrade:local
# The SDK pyproject.toml pins, and its base image. Where the sibling
# meridian-python checkout carries exactly that version -- one not yet
# published, being tried -- it is built from there (SDK_REPO): the tests' and
# the check's containers install it from source, and the plugin's image is
# built on a base made from it. Otherwise both come from PyPI and ghcr.io.
SDK_VERSION := $(shell sed -n 's/.*"open-meridian==\([0-9][0-9.]*\)".*/\1/p' pyproject.toml)
SDK_REPO    ?= $(if $(shell grep -s '^version = "$(SDK_VERSION)"$$' ../meridian-python/pyproject.toml),../meridian-python,)
SDK_CONTEXT := $(if $(SDK_REPO),--build-context sdk=$(SDK_REPO),)
BASE        ?= $(if $(SDK_REPO),plugin-python:$(SDK_VERSION)-local,ghcr.io/open-meridian/plugin-python:$(SDK_VERSION))
PLUGIN_CHECK := meridian-snaptrade-plugin-check
# The meridian check.yaml holds the plugin to, read from there so the two
# cannot drift.
MERIDIAN_VERSION := $(shell sed -n 's/^ *MERIDIAN_VERSION: *\([0-9][0-9.]*\).*/\1/p' .github/workflows/check.yaml)

# The released runtime `make e2e` proves the plugin against, by tag and
# digest: the tag says which core, the digest makes it immutable; and core's
# plugin harness published with it, by the same commit's tag and its digest,
# since a harness and a runtime of different commits may disagree about the
# dashboard's pages. Moved together by a deliberate commit, when this plugin
# chooses, and with the SDK when a contract version changes. `make e2e
# RUNTIME_IMAGE=...:latest HARNESS_IMAGE=...:latest` tries a newer core;
# e2e-latest.yaml does that weekly.
RUNTIME_IMAGE ?= ghcr.io/open-meridian/meridian-runtime:1d16c52@sha256:e40edf9361f7e646b602a1349106df1bfe648c568a6f730e8a411fcbce5e0af7
HARNESS_IMAGE ?= ghcr.io/open-meridian/meridian-harness:1d16c52@sha256:f959ec2001d3621080480d73ce7ea30bc690f56d43782ac4ed1901b2b1c03a34
# Its roles as pyproject.toml declares them (a JSON list's items), so the
# harness launches it as `meridian plugin upload` would.
ROLES := $(shell sed -n 's/^roles *= *\[\(.*\)\]/\1/p' pyproject.toml | tr -d ' ')
# The harness's one plugin, this one, as instance snaptrade: the list
# `harness.py compose` writes the plugins' half of the deployment from.
PLUGINS := [{"instance": "snaptrade", "image": "$(IMAGE)", "roles": [$(ROLES)]}]
# The plugin harness copied out of HARNESS_IMAGE, as its own project, with
# the plugins it wrote and e2e/plugin.yaml's root for the plugin. Every
# compose command gets the runtime, since compose reads every file each time.
E2E     := MERIDIAN_RUNTIME_IMAGE=$(RUNTIME_IMAGE) \
           docker compose -p snaptrade-e2e -f .e2e/harness/compose.yaml \
           -f .e2e/harness/plugins.yaml -f e2e/plugin.yaml
E2E_RUN := $(E2E) run --rm -T runner
E2E_STREET := $(E2E) run --rm -T store street
# The custodian's activity and each connection's latest sync status, as the
# street keeps them (contract v14): the harness's `store activity`.
E2E_ACTIVITY := $(E2E) run --rm -T store activity
# How many activities synthetic Alpaca's history holds (synthetic.py), each
# one line of `store activity` once its backfill is reported.
E2E_ALPACA_ACTIVITIES := 16
# What the plugin kept of SnapTrade's raw responses for Alpaca's account, read
# inside its container from the storage the harness grants a custody plugin
# (decisions/028, MERIDIAN_STORAGE_DIR): the latest read's calls, by name.
# The harness's admin holds Manage alone, so the Raw responses tab, under Open
# and View, is the tests' to prove (tests/test_raw.py).
export E2E_RAW := import os; from snaptrade.raw import RawStore, storage_root; \
	assert os.environ.get("MERIDIAN_STORAGE_DIR"), "no storage granted"; \
	s = RawStore(storage_root()); r = s.latest("ALPACA:SYN-ALP-1001"); \
	calls = [c["call"] for c in r.calls] if r else []; \
	assert calls == ["listing connections", "listing accounts", "reading positions", "reading balances", "reading activities"], calls; \
	print(len(s.reads("ALPACA:SYN-ALP-1001")), "reads of Alpaca, the latest", r.key, "in", storage_root())
# The same read found again, by its key, in a new container: the granted
# storage outlives the one it was written from (decisions/028).
# Each activity Alpaca's backfill reported names its own raw record, kept in
# the granted storage (contract v14): SYNXX's reinvestment, found by its key.
export E2E_ACTIVITY_RAW := from snaptrade.raw import RawStore, storage_root; \
	k = "activities/ALPACA:SYN-ALP-1001/00000000-0000-4000-8000-00000000f107"; \
	found = RawStore(storage_root()).find(k); assert found, k; \
	assert found[1]["body"]["type"] == "REI", found[1]; print("its record", k, "kept")
export E2E_RAW_KEPT := import os; from snaptrade.raw import RawStore, storage_root; \
	k = os.environ["E2E_KEPT"]; keys = [r.key for r in RawStore(storage_root()).reads("ALPACA:SYN-ALP-1001")]; \
	assert k in keys, (k, keys); print("read", k, "kept across a new container")

help:
	@echo "  make ci-local       every gate: lint, tests, plugin check, the plugin's image, and e2e (the pre-push gate)"
	@echo "  make ci-remote      what ci.yaml runs: lint and tests"
	@echo "  make test           the tests, in a container"
	@echo "  make lint           ruff and mypy, strict"
	@echo "  make check          what check.yaml runs: meridian plugin check --verified --run-tests, in a container"
	@echo "  make image          build the plugin's image, as upload would, and check it"
	@echo "  make e2e            the plugin, synthetic, on the plugin harness of the runtime it pins: its rows in the street store"
	@echo "  make preview        write preview/: each page on synthetic data, linking the kit"
	@echo "  make fmt            apply the formatter"
	@echo "  make install-hooks  point git at hooks/ so push fires ci-local"

# Local green is the completion signal; CI is confirmation. Every CI job is a
# target reachable from here.
ci-local: ci-remote check image e2e
	@echo
	@echo "ci-local: GREEN"

ci-local-deep: ci-local

ci-remote: lint test
	@echo
	@echo "ci-remote: GREEN"

lint:
	@$(DOCKER) build $(SDK_CONTEXT) -f Dockerfile.check --target lint . >/dev/null 2>&1 \
		|| { echo "lint FAILED; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build $(SDK_CONTEXT) -f Dockerfile.check --target lint --progress=plain ." >&2; exit 1; }
	@echo "lint OK: ruff and mypy (strict) clean"

test:
	@$(DOCKER) build $(SDK_CONTEXT) -f Dockerfile.check --target test -t $(CHECK) . >/dev/null 2>&1 \
		|| { echo "test FAILED to build; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build $(SDK_CONTEXT) -f Dockerfile.check --target test --progress=plain ." >&2; exit 1; }
	@docker run --rm $(CHECK) python -m pytest -q -rs >.test.log 2>&1 \
		|| { echo "test FAILED. The last 40 lines, and the whole of it in .test.log:" >&2; \
		     tail -40 .test.log >&2; exit 1; }
	@echo "test OK: $$(tail -1 .test.log)"

# check.yaml's job, in a container: the meridian it pins, installed as it is
# there, holds the plugin to the framework's rules and runs its tests. The
# repository is mounted read-only, and the check writes nothing to it.
check:
	@[ -n "$(MERIDIAN_VERSION)" ] \
		|| { echo "check FAILED: .github/workflows/check.yaml pins no MERIDIAN_VERSION" >&2; exit 1; }
	@$(DOCKER) build $(SDK_CONTEXT) -f Dockerfile.check --target check --build-arg MERIDIAN_VERSION=$(MERIDIAN_VERSION) \
		-t $(PLUGIN_CHECK) . >/dev/null 2>&1 \
		|| { echo "check FAILED to build; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build $(SDK_CONTEXT) -f Dockerfile.check --target check --build-arg MERIDIAN_VERSION=$(MERIDIAN_VERSION) --progress=plain ." >&2; exit 1; }
	@out="$$(docker run --rm -v "$(CURDIR)":/w:ro $(PLUGIN_CHECK) meridian plugin check --verified --run-tests 2>&1)" \
		|| { echo "check FAILED:" >&2; echo "$$out" >&2; exit 1; }
	@echo "check OK: meridian $(MERIDIAN_VERSION) plugin check --verified --run-tests, every rule holds"

# The image `meridian plugin upload` builds from this Dockerfile, checked for
# what a plugin's image must be: it starts as 65532, imports itself and
# SnapTrade's SDK, and carries nothing of this repository but the package (its
# final stage receives wheels, so the agents' files cannot reach it).
image:
	@if [ -n "$(SDK_REPO)" ]; then \
		$(MAKE) --no-print-directory -C "$(SDK_REPO)" base-image BASE_IMAGE=$(BASE) >/dev/null \
			|| { echo "image FAILED: the base for open-meridian $(SDK_VERSION) was not built from $(SDK_REPO)" >&2; exit 1; }; \
	fi
	@$(DOCKER) build --build-arg BASE=$(BASE) -t $(IMAGE) . >/dev/null 2>&1 \
		|| { echo "image FAILED; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build --build-arg BASE=$(BASE) --progress=plain ." >&2; exit 1; }
	@[ "$$(docker inspect -f '{{.Config.User}}' $(IMAGE))" = "65532" ] \
		|| { echo "image FAILED: it does not run as 65532" >&2; exit 1; }
	@docker run --rm --entrypoint python $(IMAGE) -c \
		"import meridian, snaptrade.__main__, snaptrade.page, snaptrade_client" \
		|| { echo "image FAILED: it does not import the SDK, SnapTrade's SDK and itself" >&2; exit 1; }
	@[ -z "$$(docker run --rm --entrypoint sh $(IMAGE) -c 'ls -A /plugin')" ] \
		|| { echo "image FAILED: /plugin holds files; only the installed package belongs" >&2; exit 1; }
	@echo "image OK: $(IMAGE) on $(BASE), $$(docker image inspect -f '{{.Size}}' $(IMAGE) | awk '{printf "%.0f MB", $$1/1e6}')"

# The plugin, in synthetic mode and with no key, on the plugin harness
# published with the runtime it pins (the harness's README, in
# HARNESS_IMAGE): an admin turns
# synthetic on and creates "E2E Alpaca" linked to Alpaca's account through
# this plugin's own Account links form; then the street store holds Alpaca's
# statement exactly as e2e/expected.street says, and nothing for the two
# accounts left unlinked, which the dashboard counts as reported and not
# linked; and the street's activity is e2e/expected.activity (contract v14):
# Alpaca's history backfilled back to its history_from, one of each kind and
# a type as reported, each once, and every connection's latest sync status
# with its history_from. The plugin's container made again backfills again,
# every activity answered already recorded, and the file is unchanged. Last,
# the admin links IBKR's plan code OQKR to SYNXX's record in the plugin's
# Settings form, a table setting (contract v14, W6.11), the Settings tab
# naming who changed it, and links IBKR: its first read's backfill reports
# the reinvestment under OQKR as SYNXX's record. Then it links Fidelity's IRA:
# its statement holds SYNDP, a deposit SnapTrade marks a cash equivalent, as
# the USD cash that counts it (e2e/expected.ira-marked); the admin lists SYNFD
# in the counted_as_cash table setting, and the next statement holds two rows,
# USD cash 12.99, SYNFD's 2.99 added, supplied by the admin, and no SYNFD
# (e2e/expected.ira-listed; the street keeps SYNFD's position as the earlier
# statement left it, which a reader of the latest statement takes as absent). Every instrument is a placeholder there, since the harness has no
# platform, so the file names each row by the identifiers this plugin sent.
# Its own project and no published port; nothing outlives it.
e2e: image
	@[ -n '$(ROLES)' ] || { echo "e2e FAILED: pyproject.toml declares no roles" >&2; exit 1; }
	@rm -rf .e2e && mkdir -p .e2e
	@# A tag alone, such as latest, can move: take what the registry has now.
	@for image in $(RUNTIME_IMAGE) $(HARNESS_IMAGE); do \
		case "$$image" in *@sha256:*) ;; *) docker pull -q "$$image" >/dev/null 2>&1 || true;; esac; \
	done
	@# The harness is taken down, and its copy removed, however the run ends:
	@# `meridian plugin check` reads every source file under this directory,
	@# and the harness's runner is not the plugin's.
	@started=$$(date +%s); \
	trap '$(E2E) down -v --remove-orphans >>.e2e/components.log 2>&1; rm -rf .e2e/harness' EXIT; \
	fail() { grep -h '^harness .* FAILED' .e2e/runner.log 2>/dev/null | tail -1 >&2; \
		echo "e2e FAILED: $$1; the components' logs are in .e2e/components.log, the runner's in .e2e/runner.log" >&2; \
		$(E2E) logs --no-color >>.e2e/components.log 2>&1; exit 1; }; \
	id="$$(docker create $(HARNESS_IMAGE) none 2>>.e2e/components.log)" \
		&& docker cp "$$id:/harness" .e2e/harness >/dev/null \
		&& docker rm "$$id" >/dev/null \
		|| { echo "e2e FAILED: no plugin harness at /harness in $(HARNESS_IMAGE)" >&2; trap - EXIT; exit 1; }; \
	printf '%s\n' '$(PLUGINS)' >.e2e/plugins.json; \
	docker run --rm -i -v "$(CURDIR)/.e2e/harness":/harness:ro python:3.12-alpine \
		python /harness/harness.py compose <.e2e/plugins.json >.e2e/harness/plugins.yaml 2>>.e2e/components.log \
		|| { echo "e2e FAILED: the harness did not write its plugins from .e2e/plugins.json" >&2; exit 1; }; \
	$(E2E) down -v --remove-orphans >>.e2e/components.log 2>&1; \
	$(E2E) up -d >>.e2e/components.log 2>&1 || fail "the harness did not start"; \
	$(E2E_RUN) ready >>.e2e/runner.log 2>&1 || fail "the plugin never registered"; \
	$(E2E_RUN) settings synthetic=true >>.e2e/runner.log 2>&1 || fail "synthetic was not set"; \
	$(E2E_RUN) page --level admin /admin/accounts --until SYN-ALP-1001 >>.e2e/runner.log 2>&1 \
		|| fail "Account links never listed Alpaca's account"; \
	$(E2E_RUN) form --level admin --page /admin/accounts --post /admin/accounts/link \
		intent=create external_account_id=ALPACA:SYN-ALP-1001 'new_account_name=E2E Alpaca' \
		--expect "Created E2E Alpaca and linked" >>.e2e/runner.log 2>&1 \
		|| fail "the Account links form did not create and link E2E Alpaca"; \
	for i in $$(seq 1 60); do \
		$(E2E_STREET) >.e2e/street 2>>.e2e/components.log || fail "store street did not print the street store"; \
		grep -q '^statement|E2E Alpaca|snaptrade|[0-9]*|complete|' .e2e/street && break; \
		sleep 1; \
	done; \
	diff -u e2e/expected.street .e2e/street >&2 \
		|| fail "the street store is not e2e/expected.street"; \
	unlinked="$$($(E2E_RUN) unlinked --expect 3 2>>.e2e/runner.log)" \
		|| fail "the dashboard did not count the three accounts left unlinked"; \
	for i in $$(seq 1 60); do \
		$(E2E_ACTIVITY) >.e2e/activity 2>>.e2e/components.log || fail "store activity did not print the street's activity"; \
		[ "$$(grep -c '^activity|E2E Alpaca|' .e2e/activity)" -ge $(E2E_ALPACA_ACTIVITIES) ] && break; \
		sleep 1; \
	done; \
	diff -u e2e/expected.activity .e2e/activity >&2 \
		|| fail "the street's activity is not e2e/expected.activity"; \
	raw="$$($(E2E) exec -T snaptrade python -c "$$E2E_RAW" 2>>.e2e/components.log)" \
		|| fail "the plugin kept no raw responses for Alpaca's account in its granted storage"; \
	key="$$(printf '%s' "$$raw" | sed -n 's/.* the latest \([^ ]*\) in .*/\1/p')"; \
	[ -n "$$key" ] || fail "the raw responses named no read"; \
	$(E2E_RUN) grant --level read >>.e2e/runner.log 2>&1 || fail "read on the plugin was not granted"; \
	$(E2E_RUN) page --level read "/raw?ref=ALPACA:SYN-ALP-1001/$$key/positions" --until "The record a row references" \
		>>.e2e/runner.log 2>&1 || fail "a holding's raw-record reference did not resolve on the Raw responses tab"; \
	$(E2E) up -d --no-deps --force-recreate snaptrade >>.e2e/components.log 2>&1 || fail "the plugin's container was not made again"; \
	kept="$$($(E2E) exec -T -e E2E_KEPT="$$key" snaptrade python -c "$$E2E_RAW_KEPT" 2>>.e2e/components.log)" \
		|| fail "the raw responses did not survive a new container"; \
	for i in $$(seq 1 60); do \
		$(E2E) logs --no-color snaptrade 2>>.e2e/components.log | grep -q "backfilled ALPACA:SYN-ALP-1001: $(E2E_ALPACA_ACTIVITIES) activities back to 2025-06-02, 0 recorded" && break; \
		[ "$$i" = 60 ] && fail "the new container did not backfill Alpaca's history again, every activity already recorded"; \
		sleep 1; \
	done; \
	$(E2E_ACTIVITY) >.e2e/activity-again 2>>.e2e/components.log || fail "store activity did not print the street's activity"; \
	diff -u e2e/expected.activity .e2e/activity-again >&2 \
		|| fail "the backfill reported again recorded something twice"; \
	activity="$$($(E2E) exec -T snaptrade python -c "$$E2E_ACTIVITY_RAW" 2>>.e2e/components.log)" \
		|| fail "an activity's raw record was not kept in the granted storage"; \
	synxx="$$($(E2E_RUN) instruments 2>>.e2e/runner.log | awk -F'\t' '/SYNXX/ {print $$1; exit}')"; \
	[ -n "$$synxx" ] || fail "the deployment holds no record for SYNXX to link a plan's code to"; \
	saved="$$($(E2E_RUN) settings 'plan_code_links[0].account=INTERACTIVE-BROKERS-FLEX:SYN-IB-2002' \
		'plan_code_links[0].code=OQKR' "plan_code_links[0].instrument=$$synxx" \
		--expect 'Last changed by' 2>>.e2e/runner.log)" \
		|| fail "the plan-code link was not set in the plugin's settings form"; \
	printf '%s\n' "$$saved" >>.e2e/runner.log; \
	$(E2E_RUN) form --level admin --page /admin/accounts --post /admin/accounts/link \
		intent=create external_account_id=INTERACTIVE-BROKERS-FLEX:SYN-IB-2002 'new_account_name=E2E IBKR' \
		--expect "Created E2E IBKR and linked" >>.e2e/runner.log 2>&1 \
		|| fail "the Account links form did not create and link E2E IBKR"; \
	for i in $$(seq 1 60); do \
		$(E2E_ACTIVITY) >.e2e/activity-linked 2>>.e2e/components.log || fail "store activity did not print the street's activity"; \
		grep -q "^activity|E2E IBKR|snaptrade|00000000-0000-4000-8000-00000000f116|3|" .e2e/activity-linked && break; \
		[ "$$i" = 60 ] && fail "IBKR's reinvestment under OQKR never reached the street"; \
		sleep 1; \
	done; \
	planned="$$(grep '^activity|E2E IBKR|snaptrade|00000000-0000-4000-8000-00000000f116|' .e2e/activity-linked)"; \
	echo "$$planned" | grep -q "|local(symbol:SYNXX@snaptrade)|2026-07-31|1.5$$" \
		|| fail "IBKR's reinvestment under OQKR did not resolve through the link to SYNXX's record: $$planned"; \
	$(E2E_RUN) form --level admin --page /admin/accounts --post /admin/accounts/link \
		intent=create external_account_id=FIDELITY:SYN-FID-4004 'new_account_name=E2E IRA' \
		--expect "Created E2E IRA and linked" >>.e2e/runner.log 2>&1 \
		|| fail "the Account links form did not create and link E2E IRA"; \
	for i in $$(seq 1 60); do \
		$(E2E_STREET) >.e2e/street-ira 2>>.e2e/components.log || fail "store street did not print the street store"; \
		grep -q '^statement|E2E IRA|snaptrade|3|complete|' .e2e/street-ira && break; \
		[ "$$i" = 60 ] && fail "E2E IRA's statement never reached the street"; \
		sleep 1; \
	done; \
	grep '|E2E IRA|' .e2e/street-ira | diff -u e2e/expected.ira-marked - >&2 \
		|| fail "E2E IRA's statement is not e2e/expected.ira-marked: SYNDP, marked a cash equivalent, as the cash"; \
	listed="$$($(E2E_RUN) settings 'counted_as_cash[0].symbol=SYNFD' 'counted_as_cash[0].currency=USD' \
		'counted_as_cash[0].account=FIDELITY:SYN-FID-4004' --expect 'Last changed by' 2>>.e2e/runner.log)" \
		|| fail "SYNFD was not listed as cash in the plugin's settings form"; \
	printf '%s\n' "$$listed" >>.e2e/runner.log; \
	for i in $$(seq 1 60); do \
		$(E2E_STREET) >.e2e/street-listed 2>>.e2e/components.log || fail "store street did not print the street store"; \
		grep -q '^statement|E2E IRA|snaptrade|2|complete|' .e2e/street-listed && break; \
		[ "$$i" = 60 ] && fail "E2E IRA's statement with SYNFD as cash never reached the street"; \
		sleep 1; \
	done; \
	grep '|E2E IRA|' .e2e/street-listed | diff -u e2e/expected.ira-listed - >&2 \
		|| fail "E2E IRA's statement once SYNFD is listed is not e2e/expected.ira-listed"; \
	echo "e2e OK in $$(( $$(date +%s) - started ))s on $(RUNTIME_IMAGE) and its harness: synthetic on and E2E Alpaca linked through Account links; the street store is e2e/expected.street, nothing for the accounts left unlinked, which the dashboard counts ($$unlinked); Alpaca's history backfilled to 2025-06-02, every kind and a type as reported, as e2e/expected.activity, each sync status with its history_from, and the backfill again from a new container recorded nothing twice ($$activity); the raw responses kept in its granted storage on a read-only root ($$raw), a holding's reference resolved on the Raw responses tab, and the $$kept; then OQKR linked to SYNXX's record in the plugin's settings ($$saved), and IBKR's next read's reinvestment under OQKR resolved through it; E2E IRA's deposit SnapTrade marks a cash equivalent sent as its cash, and once SYNFD is listed in the plugin's settings ($$listed) the next statement is two rows, USD cash 12.99, supplied by who listed it, and no SYNFD"

preview:
	@$(DOCKER) build $(SDK_CONTEXT) -f Dockerfile.check --target test -t $(CHECK) . >/dev/null 2>&1
	@mkdir -p preview
	@for tab in connections accounts statements raw; do \
		docker run --rm $(CHECK) python -m snaptrade.preview $$tab >preview/$$tab.html || exit 1; \
	done
	@echo "preview: preview/connections.html, preview/accounts.html, preview/statements.html, preview/raw.html"

# Applied in a container and written back, because the host has no toolchain.
fmt:
	@docker run --rm -v "$(CURDIR)":/w -w /w python:$(PY_VERSION)-slim \
		sh -c 'pip install -q ruff >/dev/null 2>&1; python -m ruff check --fix src tests >/dev/null; python -m ruff format src tests'
	@echo "fmt: applied"

install-hooks:
	@git config core.hooksPath hooks
	@echo "hooks installed: git push now runs 'make ci-local' first"
