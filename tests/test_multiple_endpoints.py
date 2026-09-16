"""Static (+ one live) checks for profiles/stage/litellm.multiple.yaml.

The file is litellm.yaml (DEFAULT single-pair inventory, routed group
`os.environ/SWITCHYARD_GROUP`) plus ONE extra cheap deployment reusing every
CHEAP_* variable except a hardcoded second api_base (172.17.12.4:8080).
Distinct from litellm.multipair.yaml (a SECOND pair under SWITCHYARD_GROUP_2):
this file keeps ONE pair and puts two endpoints behind its cheap tier.
Both routing layers dedupe by `litellm_params.model`:

- plugins/stage_scoped.py: `unique = list(dict.fromkeys(candidate_models))`
  then `frozenset(unique) in _SWITCHYARD_PAIRS` -> delegate.
- Upstream StageRoutingPlugin: same dedupe, requires exactly 2 UNIQUE
  candidates ordered capable-first, then narrows to the winning tier string
  (e.g. ["<cheap>", "<cheap>"]); LiteLLM keeps every healthy deployment in
  that set before load-balancing. Switchyard picks the TIER, LiteLLM picks
  the ENDPOINT.

Static tests pin that minimal-diff shape. The live test (proxy booted with
this file copied over litellm.yaml) proves tier routing still holds: clean
first turns stay efficient and the direct cheap group keeps serving.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = REPO_ROOT / "profiles" / "stage" / "litellm.yaml"
MULTIPLE = REPO_ROOT / "profiles" / "stage" / "litellm.multiple.yaml"

CHEAP_REF = "os.environ/CHEAP_MODEL"
EXPENSIVE_REF = "os.environ/EXPENSIVE_MODEL"
# Routed group name (env-valued since the multipair PR; default switchyard).
GROUP_REF = "os.environ/SWITCHYARD_GROUP"
SECOND_CHEAP_BASE = "http://172.17.12.4:8080/v1"

# Stub values standing in for the resolved env strings at runtime.
STUB_CHEAP = "openai/cheap-test-model"
STUB_EXPENSIVE = "openai/expensive-test-model"


def _load(path: Path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def _of(config, group: str) -> list[dict]:
    return [d for d in config["model_list"] if d["model_name"] == group]


def test_multiple_file_exists_alongside_base():
    assert MULTIPLE.exists(), "litellm.multiple.yaml must sit next to litellm.yaml"
    assert BASE.exists()


def test_switchyard_group_has_three_deployments_two_unique_ordered():
    config = _load(MULTIPLE)
    deployments = _of(config, GROUP_REF)
    assert len(deployments) == 3, (
        f"expected capable + 2x efficient, got {len(deployments)}"
    )
    models = [d["litellm_params"]["model"] for d in deployments]
    # Capable first, then the cheap pair sharing one env-owned string.
    assert models == [EXPENSIVE_REF, CHEAP_REF, CHEAP_REF], models
    unique = list(dict.fromkeys(models))
    assert unique == [EXPENSIVE_REF, CHEAP_REF], unique


def test_extra_cheap_deployment_reuses_cheap_vars_except_api_base():
    """The load-balancing contract: identical string, distinct api_base only.

    A `-host-b` suffix or a second prefix would look like a third tier and
    break the pair (shim warns, upstream StageRoutingPlugin raises).
    """
    config = _load(MULTIPLE)
    cheap_a, cheap_b = _of(config, GROUP_REF)[1:]
    bases = (cheap_a["litellm_params"]["api_base"], cheap_b["litellm_params"]["api_base"])
    assert bases[0] == "os.environ/CHEAP_API_BASE", bases
    assert bases[1] == SECOND_CHEAP_BASE, bases
    assert bases[1].endswith("/v1"), f"OpenAI-compatible base needs /v1: {bases[1]}"
    # Everything except api_base is reused verbatim.
    params_a = {k: v for k, v in cheap_a["litellm_params"].items() if k != "api_base"}
    params_b = {k: v for k, v in cheap_b["litellm_params"].items() if k != "api_base"}
    assert params_a == params_b, (params_a, params_b)
    assert cheap_a.get("model_info", {}) == cheap_b.get("model_info", {})


def test_everything_else_matches_base():
    """Only the extra switchyard deployment may differ from litellm.yaml."""
    base = _load(BASE)
    config = _load(MULTIPLE)
    for section in (
        "router_settings",
        "guardrails",
        "litellm_settings",
        "general_settings",
    ):
        assert config.get(section) == base.get(section), section
    base_switch = _of(base, GROUP_REF)
    multi_switch = _of(config, GROUP_REF)
    assert len(base_switch) == 2
    assert multi_switch[:2] == base_switch, "first two switchyard deployments drifted"
    assert multi_switch[2]["litellm_params"]["model"] == CHEAP_REF
    # Direct groups stay single-deployment each, exactly as in the base file.
    for ref in (CHEAP_REF, EXPENSIVE_REF):
        assert _of(config, ref) == _of(base, ref), ref


def test_capability_surface_matches_across_tiers():
    config = _load(MULTIPLE)
    infos = [d.get("model_info", {}) for d in _of(config, GROUP_REF)]
    assert len(infos) == 3
    for flag in (
        "supports_reasoning",
        "supports_function_calling",
        "supports_vision",
    ):
        assert {info[flag] for info in infos} == {infos[0][flag]}, flag
        assert infos[0][flag] is True or infos[0][flag] is False
    assert infos[0]["supports_vision"] is False


def _load_shim_with_stubs():
    """Import the real stage_scoped.py with litellm/switchyard stubbed."""
    import os

    os.environ["CHEAP_MODEL"] = STUB_CHEAP
    os.environ["EXPENSIVE_MODEL"] = STUB_EXPENSIVE
    # Hermetic pair discovery: the shim reads *_MODEL_N at import time, so
    # stray _N values from the shell must not add phantom pairs.
    for n in range(2, 17):
        os.environ.pop(f"CHEAP_MODEL_{n}", None)
        os.environ.pop(f"EXPENSIVE_MODEL_{n}", None)

    litellm = types.ModuleType("litellm")
    litellm_types = types.ModuleType("litellm.types")
    router_types = types.ModuleType("litellm.types.router")

    class RoutingContext:
        def __init__(self, candidate_models):
            self.candidate_models = list(candidate_models)
            self.structured_messages = []
            self.metadata = {}
            self.signals = {}

    router_types.RoutingContext = RoutingContext

    switchyard = types.ModuleType("switchyard_litellm")
    configuration = types.ModuleType("switchyard_litellm.configuration")
    configured_plugin = types.ModuleType(
        "switchyard_litellm.configuration.configured_plugin"
    )
    calls: list[list[str]] = []

    class FakeStagePlugin:
        async def run(self, context):
            calls.append(list(context.candidate_models))
            context.signals["switchyard"] = {"selected_model_id": "x"}
            return context

    configured_plugin.ROUTING_PLUGIN = FakeStagePlugin()
    saved = dict(sys.modules)
    sys.modules.update(
        {
            "litellm": litellm,
            "litellm.types": litellm_types,
            "litellm.types.router": router_types,
            "switchyard_litellm": switchyard,
            "switchyard_litellm.configuration": configuration,
            "switchyard_litellm.configuration.configured_plugin": configured_plugin,
        }
    )
    try:
        spec = importlib.util.spec_from_file_location(
            "stage_scoped_multi", REPO_ROOT / "plugins" / "stage_scoped.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, RoutingContext, calls
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _run(plugin, context):
    import asyncio

    return asyncio.run(plugin.PLUGIN.run(context))


def test_shim_delegates_triple_pool_with_duplicate_cheap():
    """The real shim must stage-route [expensive, cheap, cheap].

    This is exactly what LiteLLM hands over for the multiple file (one entry
    per deployment, duplicates included): dedupe -> the pair -> delegate.
    """
    module, RoutingContext, calls = _load_shim_with_stubs()
    out = _run(module, RoutingContext([STUB_EXPENSIVE, STUB_CHEAP, STUB_CHEAP]))
    assert calls == [[STUB_EXPENSIVE, STUB_CHEAP, STUB_CHEAP]]
    assert out.signals.get("switchyard")


def test_upstream_dedupe_contract_holds_for_triple_pool():
    """Mirror of StageRoutingPlugin.run's gate: deduped pool must be 2.

    Upstream raises unless `list(dict.fromkeys(candidate_models))` has length
    2 ordered capable-first. A suffixed second cheap string would make 3.
    """
    candidates = [STUB_EXPENSIVE, STUB_CHEAP, STUB_CHEAP]
    unique = list(dict.fromkeys(candidates))
    assert len(unique) == 2, unique
    assert unique == [STUB_EXPENSIVE, STUB_CHEAP]
    # Post-decision narrowing to the cheap tier keeps BOTH deployments, which
    # is what lets LiteLLM load-balance/fail over within the tier.
    narrowed = [c for c in candidates if c == STUB_CHEAP]
    assert narrowed == [STUB_CHEAP, STUB_CHEAP]
    healthy = [{"litellm_params": {"model": m}} for m in candidates]
    kept = [d for d in healthy if d["litellm_params"]["model"] in set(narrowed)]
    assert len(kept) == 2, "both cheap deployments must survive tier narrowing"


@pytest.mark.live
def test_live_extra_cheap_endpoint_keeps_tier_routing(
    proxy_url, master_key, expected_pair, expected_group
):
    """Tier verdicts hold with the extra cheap deployment in the pool.

    Boot the proxy with litellm.multiple.yaml copied over litellm.yaml for
    the extra-endpoint case; passes on the base file too. Asserts the tier
    verdict, not the physical endpoint: x-litellm-model-name carries the tier
    string either way, so per-endpoint distribution is checked via backend
    access logs or by stopping one endpoint and confirming failover is 200.
    """

    def _routed_to(headers: dict) -> str | None:
        for key, value in headers.items():
            if key.lower() == "x-litellm-model-name":
                return value
        return None

    from conftest import api_post

    # Clean first turn must stay on the cheap tier (efficient_first default).
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": expected_group,
            "messages": [{"role": "user", "content": "Reply with the word hello."}],
            "max_tokens": 256,
        },
    )
    assert status == 200, body
    assert _routed_to(headers) == expected_pair["cheap"], headers

    # Direct cheap group keeps serving repeatedly.
    for _ in range(4):
        status, headers, body = api_post(
            f"{proxy_url}/v1/chat/completions",
            master_key,
            {
                "model": expected_pair["cheap"],
                "messages": [
                    {"role": "user", "content": "Reply with the word hello."}
                ],
                "max_tokens": 256,
            },
        )
        assert status == 200, body
        assert _routed_to(headers) == expected_pair["cheap"]
