# SPDX-FileCopyrightText: Copyright (c) 2026 gyud-labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scope Switchyard stage routing to the configured model pairs.

LiteLLM runs `router_settings.plugins` globally, before every routing
decision, for whichever model group the request targets. Upstream
`StageRoutingPlugin` raises unless the candidate pool holds exactly two
unique deployments, so registering it directly would break every other
model group (including models added later via the Admin UI / DB).

This shim delegates to the TOML-configured upstream plugin only when the
request's candidate pool is exactly one of the configured cheap/expensive
pairs:

- pair 1: `CHEAP_MODEL` / `EXPENSIVE_MODEL`
  (group `SWITCHYARD_GROUP`, default `switchyard`);
- pair N>=2: `CHEAP_MODEL_N` / `EXPENSIVE_MODEL_N`
  (group `SWITCHYARD_GROUP_N`, default `switchyard_N`, settable in `.env`;
  used by litellm.multipair.yaml + .env.multipair).

Full LiteLLM model strings (e.g. "openai/my-model") are composed in
`compose.yaml` and must equal the resolved `model:` values in the active
yaml (`profiles/stage/litellm.yaml`, or `litellm.multipair.yaml` for N>=2). Group names are client-facing only; the shim
matches on model strings, not group names. Anything else passes through
untouched, preserving plain LiteLLM routing:

- a single-candidate pool (the backend-ID direct groups, or any
  unrelated single-deployment group) passes silently — this is normal;
- a multi-candidate pool that is not a known pair logs a warning, since it
  usually means someone edited a routed group itself — but the
  request still passes through rather than failing.

The upstream object stays registered under `litellm_settings.callbacks`, so
Switchyard request rewrites (`signals["switchyard"]["request_patch"]`) are
still applied after deployment selection.
"""

from __future__ import annotations

import logging
import os

from litellm.types.router import RoutingContext
from switchyard_litellm.configuration.configured_plugin import (
    ROUTING_PLUGIN as _STAGE_PLUGIN,
)

logger = logging.getLogger(__name__)

# Upper bound for _N discovery (N>=2). Bump if you ever need more pairs;
# litellm.multipair.yaml + compose.yaml + .env.multipair need matching
# entries anyway.
_MAX_PAIR_SUFFIX = 16

# Bare provider prefix compose.yaml leaves behind when a pair-N MODEL_ID is
# unset (e.g. single-pair .env with the default litellm.yaml). Treated the
# same as unset: the pair is skipped, not misconfigured.
_BARE_MODEL_PREFIX = "openai/"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} must be set: it defines the Switchyard-routed model pair "
            "(and must match profiles/stage/litellm.yaml)"
        )
    return value


def _model_or_none(name: str) -> str | None:
    """Full model string, or None when unset/degenerate (see above)."""
    value = os.environ.get(name)
    if not value or value == _BARE_MODEL_PREFIX:
        return None
    return value


def _discover_pairs() -> frozenset[frozenset[str]]:
    """Collect configured pairs: pair 1 (unsuffixed, required) + N>=2."""
    pairs: set[frozenset[str]] = set()
    first = frozenset(
        {
            _require_env("EXPENSIVE_MODEL"),
            _require_env("CHEAP_MODEL"),
        }
    )
    if len(first) != 2:
        raise RuntimeError(
            "EXPENSIVE_MODEL and CHEAP_MODEL must differ: stage routing needs "
            "exactly two unique candidate IDs"
        )
    pairs.add(first)
    for n in range(2, _MAX_PAIR_SUFFIX + 1):
        exp = _model_or_none(f"EXPENSIVE_MODEL_{n}")
        cheap = _model_or_none(f"CHEAP_MODEL_{n}")
        if exp is None and cheap is None:
            continue
        if exp is None or cheap is None:
            raise RuntimeError(
                f"EXPENSIVE_MODEL_{n} and CHEAP_MODEL_{n} must both be set "
                "to enable pair "
                f"{n} (and must match profiles/stage/litellm.multipair.yaml)"
            )
        pair = frozenset({exp, cheap})
        if len(pair) != 2:
            raise RuntimeError(
                f"EXPENSIVE_MODEL_{n} and CHEAP_MODEL_{n} must differ: "
                "stage routing needs exactly two unique candidate IDs"
            )
        if pair in pairs:
            raise RuntimeError(
                f"pair {n} duplicates an existing Switchyard pair: "
                f"{sorted(pair)} — reuse the same backends via one group "
                "instead of two groups with identical IDs"
            )
        pairs.add(pair)
    return frozenset(pairs)


# These are the full LiteLLM model strings (e.g. "openai/my-model"), composed
# in compose.yaml from CHEAP_MODEL_ID[_N] / EXPENSIVE_MODEL_ID[_N]. They must
# equal the resolved `model:` values in profiles/stage/litellm.yaml.
_SWITCHYARD_PAIRS = _discover_pairs()


class _ScopedStageRouter:
    """Routing-plugin shim: Switchyard for known pairs, passthrough otherwise."""

    async def run(self, context: RoutingContext) -> RoutingContext:
        unique = list(dict.fromkeys(context.candidate_models))
        if frozenset(unique) in _SWITCHYARD_PAIRS:
            # Order is the capable/efficient contract (capable first, per
            # litellm.yaml declaration order, which LiteLLM 1.97 preserves).
            return await _STAGE_PLUGIN.run(context)
        if len(unique) > 1:
            logger.warning(
                "Switchyard scope: candidate pool %r is not a known switchyard "
                "pair %r; passing through without stage routing. "
                "If you edited a routed group, restore exactly two "
                "deployments (capable first, efficient second).",
                unique,
                sorted(sorted(p) for p in _SWITCHYARD_PAIRS),
            )
        return context


PLUGIN = _ScopedStageRouter()

__all__ = ["PLUGIN"]
