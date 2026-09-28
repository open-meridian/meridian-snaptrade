# The plugin's image. It runs beside a sidecar in the deployment's cluster, and
# reaches nothing but that sidecar and SnapTrade.
#
# Built on the base image for the SDK release this plugin tracks: Python and
# open-meridian, already installed. BASE is for building against a base made
# locally rather than a released one.
#
# Two stages rather than the template's one, for as long as pyproject.toml pins
# the SDK by git commit (see the README): the wheels are built where git is,
# and the image receives only them, so it holds no git and no build tools. The
# SDK at that commit is installed over the base's copy. When the account-side
# operations ship in a release, the pin goes back to `open-meridian==<version>`,
# BASE moves to that version, and this goes back to the template's one stage.
ARG BASE=ghcr.io/open-meridian/plugin-python:0.3.0

FROM ${BASE} AS wheels
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml ./
COPY src src
RUN pip wheel --wheel-dir /wheels .

FROM ${BASE}
WORKDIR /plugin
# Mounted rather than copied, so no layer keeps them. Installed: the SDK at
# the pinned commit, over the base's; and every other wheel the base does not
# already hold at that version, so its gRPC and protobuf are not sent twice.
# --no-deps, since the wheels are the whole set, which pip check proves.
RUN --mount=type=bind,from=wheels,source=/wheels,target=/tmp/wheels \
    set -e; wanted=""; \
    for wheel in /tmp/wheels/*.whl; do \
      file="$(basename "$wheel")"; name="${file%%-*}"; version="$(echo "$file" | cut -d- -f2)"; \
      held="$(pip show "$name" 2>/dev/null | sed -n 's/^Version: //p')"; \
      if [ "$name" = open_meridian ] || [ "$held" != "$version" ]; then wanted="$wanted $wheel"; fi; \
    done; \
    pip install --no-deps $wanted; \
    pip check
USER 65532
CMD ["snaptrade"]
