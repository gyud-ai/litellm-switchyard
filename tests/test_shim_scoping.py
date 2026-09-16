"""Shim scoping rules without litellm/switchyard installed (stubbed imports).

Pins the contract `profiles/stage/litellm.yaml` relies on: the exact pair
delegates to stage routing (order preserved), single-candidate pools pass
silently, anything else warns but still passes through.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CHEAP = "openai/cheap-test-model"
EXPENSIVE = "openai/expensive-test-model"


@pytest.fixture(scope="module")
def scoped_plugin():
    os.environ["CHEAP_MODEL"] = CHEAP
    os.environ["EXPENSIVE_MODEL"] = EXPENSIVE

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
            "stage_scoped", REPO_ROOT / "plugins" / "stage_scoped.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module, RoutingContext, calls
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _run(plugin, context):
    import asyncio

    return asyncio.run(plugin.PLUGIN.run(context))


def test_exact_pair_delegates_with_order_preserved(scoped_plugin):
    module, RoutingContext, calls = scoped_plugin
    ctx = RoutingContext([EXPENSIVE, CHEAP])
    out = _run(module, ctx)
    assert calls == [[EXPENSIVE, CHEAP]]
    assert out.signals.get("switchyard")


def test_single_candidate_alias_passes_silently(scoped_plugin, caplog):
    module, RoutingContext, calls = scoped_plugin
    before = len(calls)
    for pool in ([CHEAP], [EXPENSIVE], ["openai/unrelated-model"]):
        with caplog.at_level(logging.WARNING, logger="stage_scoped"):
            out = _run(module, RoutingContext(pool))
        assert len(calls) == before
        assert out.candidate_models == pool
        assert out.signals == {}
    assert "Switchyard scope" not in caplog.text


def test_multi_candidate_non_pair_warns_but_passes_through(scoped_plugin, caplog):
    module, RoutingContext, calls = scoped_plugin
    before = len(calls)
    pool = [EXPENSIVE, CHEAP, "openai/third-model"]
    with caplog.at_level(logging.WARNING, logger="stage_scoped"):
        out = _run(module, RoutingContext(pool))
    assert len(calls) == before
    assert out.candidate_models == pool
    assert "Switchyard scope" in caplog.text
