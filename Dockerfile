# SPDX-FileCopyrightText: Copyright (c) 2026 gyud-labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Self-contained variant of upstream
# NVIDIA-NeMo/Switchyard examples/litellm/deployment/Dockerfile.
#
# Upstream expects `context: ../../..` (the Switchyard repo root). This repo is
# standalone, so the builder stage clones Switchyard at a pinned ref and builds
# the maturin wheel from there. Pin reviewed on 2026-09-15; override with
# --build-arg SWITCHYARD_REF=<sha|tag> if you need a newer checkout.
ARG SWITCHYARD_REF=db9c9632e33686e6049d22722a5df2fb92cd62ef

FROM rust:1.96.1-slim-bookworm AS switchyard-builder
ARG SWITCHYARD_REF

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git patchelf python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/maturin \
    && /opt/maturin/bin/pip install --no-cache-dir "maturin==1.14.1"

WORKDIR /src
RUN git clone https://github.com/NVIDIA-NeMo/Switchyard.git . \
    && git checkout "${SWITCHYARD_REF}" \
    && git rev-parse HEAD
RUN /opt/maturin/bin/maturin build \
    --release \
    --locked \
    --compatibility manylinux_2_34 \
    --out /wheels \
    --interpreter /usr/bin/python3

FROM ghcr.io/astral-sh/uv:0.8.15 AS uv

# Pinned: switchyard-litellm integration verifies against litellm 1.97.0 only.
FROM ghcr.io/berriai/litellm:v1.97.0

COPY --from=uv /uv /usr/local/bin/uv
RUN --mount=type=bind,from=switchyard-builder,source=/wheels,target=/wheels \
    uv pip install --python /app/.venv/bin/python --no-deps /wheels/*.whl

COPY --from=switchyard-builder /src/examples/litellm/src /app/switchyard-plugin/src
ENV PYTHONPATH=/app/switchyard-plugin/src

# NOTE: upstream's smoke test (algorithms.random(['smoke'])) is stale at the
# pinned ref -- `random`'s first positional is now `weights`, so it fails, and
# upstream's own RandomRoutingPlugin is affected too. We don't use `random`,
# so the smoke test loads our real stage TOML through the real loader instead.
COPY profiles/stage/switchyard.toml /tmp/smoke/switchyard.toml
RUN SWITCHYARD_LITELLM_CONFIG=/tmp/smoke/switchyard.toml python -c "from importlib.metadata import version; \
from switchyard_litellm import StageRoutingPlugin; \
from switchyard_litellm.configuration.configured_plugin import ROUTING_PLUGIN; \
assert version('litellm') == '1.97.0'; \
assert isinstance(ROUTING_PLUGIN, StageRoutingPlugin); \
print('smoke OK:', type(ROUTING_PLUGIN).__name__)"

# Base image is Wolfi (BusyBox user tools), so upstream's Alpine-style
# addgroup/adduser lines are correct here.
RUN addgroup -S -g 10001 litellm \
    && adduser -S -D -u 10001 -G litellm -h /home/litellm litellm
USER litellm
