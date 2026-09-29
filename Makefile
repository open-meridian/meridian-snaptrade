SHELL := /bin/bash

# A pre-push hook runs with GIT_DIR naming this repository; nothing here runs
# git on the host, and this keeps it that way should something start to.
unexport GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR \
         GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX

.PHONY: help ci-local ci-local-deep ci-remote test lint image preview fmt install-hooks

DOCKER      := DOCKER_BUILDKIT=1 docker
PY_VERSION  := 3.12
CHECK       := meridian-snaptrade-check
IMAGE       ?= snaptrade:local
BASE        ?= ghcr.io/open-meridian/plugin-python:0.5.0

help:
	@echo "  make ci-local       every gate: lint, tests, and the plugin's image (the pre-push gate)"
	@echo "  make ci-remote      what CI runs: lint and tests"
	@echo "  make test           the tests, in a container"
	@echo "  make lint           ruff and mypy, strict"
	@echo "  make image          build the plugin's image, as upload would, and check it"
	@echo "  make preview        write preview/: each admin page on synthetic data, linking the kit"
	@echo "  make fmt            apply the formatter"
	@echo "  make install-hooks  point git at hooks/ so push fires ci-local"

# Local green is the completion signal; CI is confirmation. Every CI job is a
# target reachable from here.
ci-local: ci-remote image
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

preview:
	@$(DOCKER) build -f Dockerfile.check --target test -t $(CHECK) . >/dev/null 2>&1
	@mkdir -p preview
	@for tab in connections accounts holdings; do \
		docker run --rm $(CHECK) python -m snaptrade.preview $$tab >preview/$$tab.html || exit 1; \
	done
	@echo "preview: preview/connections.html, preview/accounts.html, preview/holdings.html"

# Applied in a container and written back, because the host has no toolchain.
fmt:
	@docker run --rm -v "$(CURDIR)":/w -w /w python:$(PY_VERSION)-slim \
		sh -c 'pip install -q ruff >/dev/null 2>&1; python -m ruff check --fix src tests >/dev/null; python -m ruff format src tests'
	@echo "fmt: applied"

install-hooks:
	@git config core.hooksPath hooks
	@echo "hooks installed: git push now runs 'make ci-local' first"
