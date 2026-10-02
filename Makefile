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
BASE        ?= ghcr.io/open-meridian/plugin-python:0.13.0
PLUGIN_CHECK := meridian-snaptrade-plugin-check
# The meridian check.yaml holds the plugin to, read from there so the two
# cannot drift.
MERIDIAN_VERSION := $(shell sed -n 's/^ *MERIDIAN_VERSION: *\([0-9][0-9.]*\).*/\1/p' .github/workflows/check.yaml)

# The released runtime `make e2e` proves the plugin against, by tag and
# digest: the tag says which core, the digest makes it immutable. Moved by a
# deliberate commit, when this plugin chooses, and with the SDK when a
# contract version changes. `make e2e RUNTIME_IMAGE=...:latest` tries a newer
# one; e2e-latest.yaml does that weekly.
RUNTIME_IMAGE ?= ghcr.io/open-meridian/meridian-runtime:b49445e@sha256:5031dc4c962f380755e9e608456da5d3a18bdda1b04938de01ad0e6d44a3a5d6
# Its roles as pyproject.toml declares them, so the harness launches it as
# `meridian plugin upload` would.
ROLES := $(shell sed -n 's/^roles *= *\[\(.*\)\]/\1/p' pyproject.toml | tr -d '" ')
# The plugin harness copied out of RUNTIME_IMAGE, as its own project, with
# e2e/plugin.yaml's restart for the plugin. Every compose command gets all
# three variables, since compose reads the whole file each time.
E2E     := MERIDIAN_RUNTIME_IMAGE=$(RUNTIME_IMAGE) MERIDIAN_HARNESS_PLUGIN_IMAGE=$(IMAGE) \
           MERIDIAN_HARNESS_PLUGIN_ROLES=$(ROLES) \
           docker compose -p snaptrade-e2e -f .e2e/harness/compose.yaml -f e2e/plugin.yaml
E2E_RUN := $(E2E) run --rm -T runner
E2E_STREET := $(E2E) exec -T postgres psql -U meridian -d meridian -At -v ON_ERROR_STOP=1 -f /harness/street.sql
# What the plugin kept of SnapTrade's raw responses for Alpaca's account, read
# inside its container (raw.STAND_IN, its /tmp): the latest read's calls, by
# name. The harness's admin holds Manage alone, so the Raw responses tab,
# under Open and View, is the tests' to prove (tests/test_raw.py).
export E2E_RAW := from snaptrade.raw import STAND_IN, RawStore; \
	r = RawStore(STAND_IN).latest("ALPACA:SYN-ALP-1001"); \
	calls = [c["call"] for c in r.calls] if r else []; \
	assert calls == ["listing connections", "listing accounts", "reading positions", "reading balances"], calls; \
	print(len(RawStore(STAND_IN).reads("ALPACA:SYN-ALP-1001")), "reads of Alpaca, the latest", r.key)

help:
	@echo "  make ci-local       every gate: lint, tests, plugin check, the plugin's image, and e2e (the pre-push gate)"
	@echo "  make ci-remote      what ci.yaml runs: lint and tests"
	@echo "  make test           the tests, in a container"
	@echo "  make lint           ruff and mypy, strict"
	@echo "  make check          what check.yaml runs: meridian plugin check --run-tests, in a container"
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
	@$(DOCKER) build -f Dockerfile.check --target lint . >/dev/null 2>&1 \
		|| { echo "lint FAILED; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build -f Dockerfile.check --target lint --progress=plain ." >&2; exit 1; }
	@echo "lint OK: ruff and mypy (strict) clean"

test:
	@$(DOCKER) build -f Dockerfile.check --target test -t $(CHECK) . >/dev/null 2>&1 \
		|| { echo "test FAILED to build; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build -f Dockerfile.check --target test --progress=plain ." >&2; exit 1; }
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
	@$(DOCKER) build -f Dockerfile.check --target check --build-arg MERIDIAN_VERSION=$(MERIDIAN_VERSION) \
		-t $(PLUGIN_CHECK) . >/dev/null 2>&1 \
		|| { echo "check FAILED to build; see it with:" >&2; \
		     echo "  DOCKER_BUILDKIT=1 docker build -f Dockerfile.check --target check --build-arg MERIDIAN_VERSION=$(MERIDIAN_VERSION) --progress=plain ." >&2; exit 1; }
	@out="$$(docker run --rm -v "$(CURDIR)":/w:ro $(PLUGIN_CHECK) meridian plugin check --run-tests 2>&1)" \
		|| { echo "check FAILED:" >&2; echo "$$out" >&2; exit 1; }
	@echo "check OK: meridian $(MERIDIAN_VERSION) plugin check --run-tests, every rule holds"

# The image `meridian plugin upload` builds from this Dockerfile, checked for
# what a plugin's image must be: it starts as 65532, imports itself and
# SnapTrade's SDK, and carries nothing of this repository but the package (its
# final stage receives wheels, so the agents' files cannot reach it).
image:
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

# The plugin, in synthetic mode and with no key, on the plugin harness of the
# runtime it pins (the harness's README, in that image): an admin turns
# synthetic on and creates "E2E Alpaca" linked to Alpaca's account through
# this plugin's own Account links form; then the street store holds Alpaca's
# statement exactly as e2e/expected.street says, and nothing for the two
# accounts left unlinked, which the dashboard counts as reported and not
# linked. Every instrument is a placeholder there, since the harness has no
# platform, so the file names each row by the identifiers this plugin sent.
# Its own project and no published port; nothing outlives it.
e2e: image
	@[ -n "$(ROLES)" ] || { echo "e2e FAILED: pyproject.toml declares no roles" >&2; exit 1; }
	@rm -rf .e2e && mkdir -p .e2e
	@# A tag alone, such as latest, can move: take what the registry has now.
	@case "$(RUNTIME_IMAGE)" in *@sha256:*) ;; *) docker pull -q $(RUNTIME_IMAGE) >/dev/null 2>&1 || true;; esac
	@# The harness is taken down, and its copy removed, however the run ends:
	@# `meridian plugin check` reads every source file under this directory,
	@# and the harness's runner is not the plugin's.
	@started=$$(date +%s); \
	trap '$(E2E) down -v --remove-orphans >>.e2e/components.log 2>&1; rm -rf .e2e/harness' EXIT; \
	fail() { grep -h '^harness .* FAILED' .e2e/runner.log 2>/dev/null | tail -1 >&2; \
		echo "e2e FAILED: $$1; the components' logs are in .e2e/components.log, the runner's in .e2e/runner.log" >&2; \
		$(E2E) logs --no-color >>.e2e/components.log 2>&1; exit 1; }; \
	id="$$(docker create $(RUNTIME_IMAGE) none 2>>.e2e/components.log)" \
		&& docker cp "$$id:/usr/share/meridian/harness" .e2e/harness >/dev/null \
		&& docker rm "$$id" >/dev/null \
		|| { echo "e2e FAILED: no plugin harness at /usr/share/meridian/harness in $(RUNTIME_IMAGE)" >&2; trap - EXIT; exit 1; }; \
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
		$(E2E_STREET) >.e2e/street 2>>.e2e/components.log || fail "street.sql did not run"; \
		grep -q '^statement|E2E Alpaca|snaptrade|[0-9]*|complete|' .e2e/street && break; \
		sleep 1; \
	done; \
	diff -u e2e/expected.street .e2e/street >&2 \
		|| fail "the street store is not e2e/expected.street"; \
	unlinked="$$($(E2E_RUN) unlinked --expect 2 2>>.e2e/runner.log)" \
		|| fail "the dashboard did not count the two accounts left unlinked"; \
	raw="$$($(E2E) exec -T plugin python -c "$$E2E_RAW" 2>>.e2e/components.log)" \
		|| fail "the plugin kept no raw responses for Alpaca's account on its read-only root"; \
	echo "e2e OK in $$(( $$(date +%s) - started ))s on $(RUNTIME_IMAGE): synthetic on and E2E Alpaca linked through Account links; the street store is e2e/expected.street, nothing for the accounts left unlinked, which the dashboard counts ($$unlinked); the raw responses kept in /tmp on a read-only root ($$raw)"

preview:
	@$(DOCKER) build -f Dockerfile.check --target test -t $(CHECK) . >/dev/null 2>&1
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
