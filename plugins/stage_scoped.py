# SPDX-FileCopyrightText: Copyright (c) 2026 gyud-labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scope Switchyard stage routing to the `switchyard` model group.

LiteLLM runs `router_settings.plugins` globally, before every routing
decision, for whichever model group the request targets. Upstream
`StageRoutingPlugin` raises unless the candidate pool holds exactly two
unique deployments, so registering it directly would break every other
model group (including models added later via the Admin UI / DB).

This shim delegates to the TOML-configured upstream plugin only when the
request's candidate pool is exactly the cheap/expensive pair defined by
`CHEAP_MODEL` / `EXPENSIVE_MODEL` (composed in `compose.yaml`; must equal
the resolved `model:` values in `profiles/stage/litellm.yaml`). Anything
else passes through untouched, preserving plain LiteLLM routing:

- a single-candidate pool (the backend-ID direct groups, or any
  unrelated single-deployment group) passes silently — this is normal;
- a multi-candidate pool that is not the pair logs a warning, since it
  usually means someone edited the `switchyard` group itself — but the
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


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} must be set: it defines the Switchyard-routed model pair "
            "(and must match profiles/stage/litellm.yaml)"
        )
    return value


# These are the full LiteLLM model strings (e.g. "openai/my-model"), composed
# in compose.yaml from CHEAP_MODEL_ID / EXPENSIVE_MODEL_ID. They must equal
# the resolved `model:` values in profiles/stage/litellm.yaml.
_SWITCHYARD_PAIR = frozenset(
    {
        _require_env("EXPENSIVE_MODEL"),
        _require_env("CHEAP_MODEL"),
    }
)


class _ScopedStageRouter:
    """Routing-plugin shim: Switchyard for the pair, passthrough otherwise."""

    async def run(self, context: RoutingContext) -> RoutingContext:
        unique = list(dict.fromkeys(context.candidate_models))
        if set(unique) == _SWITCHYARD_PAIR:
            # Order is the capable/efficient contract (capable first, per
            # litellm.yaml declaration order, which LiteLLM 1.97 preserves).
            return await _STAGE_PLUGIN.run(context)
        if len(unique) > 1:
            logger.warning(
                "Switchyard scope: candidate pool %r is not the switchyard "
                "pair %r; passing through without stage routing. "
                "If you edited the `switchyard` group, restore exactly two "
                "deployments (capable first, efficient second).",
                unique,
                sorted(_SWITCHYARD_PAIR),
            )
        return context


PLUGIN = _ScopedStageRouter()

__all__ = ["PLUGIN"]
