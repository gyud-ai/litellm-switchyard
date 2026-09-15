"""Static checks: run anywhere, no proxy needed (`pytest tests/ -m 'not live'`)."""

from __future__ import annotations

import py_compile
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_litellm_yaml_has_exactly_two_ordered_switchyard_deployments():
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    deployments = config["model_list"]
    assert len(deployments) == 2, "stage routing requires exactly two deployments"
    assert {d["model_name"] for d in deployments} == {"switchyard"}
    # Order is the capable/efficient contract: expensive first, cheap second.
    models = [d["litellm_params"]["model"] for d in deployments]
    assert models == ["os.environ/EXPENSIVE_MODEL", "os.environ/CHEAP_MODEL"], models
    for deployment in deployments:
        params = deployment["litellm_params"]
        assert params["api_base"].startswith("os.environ/")
        assert params["api_key"].startswith("os.environ/")


def test_litellm_yaml_registers_routing_plugin_and_callback():
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    assert config["router_settings"]["plugins"] == ["stage_scoped.PLUGIN"]
    assert config["litellm_settings"]["callbacks"] == [
        "switchyard_litellm.configuration.configured_plugin.ROUTING_PLUGIN"
    ]
    assert config["general_settings"]["store_model_in_db"] is True


def test_litellm_yaml_forwards_client_headers_for_switchyard_group():
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    groups = config["litellm_settings"]["model_group_settings"][
        "forward_client_headers_to_llm_api"
    ]
    assert groups == ["switchyard"]


def test_switchyard_toml_stage_policy():
    with open(REPO_ROOT / "profiles" / "stage" / "switchyard.toml", "rb") as fh:
        policy = tomllib.load(fh)
    assert policy["algorithm"] == "stage"
    assert policy["picker"] == "efficient_first"
    assert policy["confidence_threshold"] == 0.5
    assert policy["recent_window"] == 3


def test_scoping_shim_compiles_and_tracks_both_tiers():
    shim = REPO_ROOT / "plugins" / "stage_scoped.py"
    assert shim.exists()
    py_compile.compile(str(shim), doraise=True)
    source = shim.read_text()
    assert "EXPENSIVE_MODEL" in source and "CHEAP_MODEL" in source


def test_compose_wires_plugin_path_and_model_strings():
    compose = (REPO_ROOT / "compose.yaml").read_text()
    assert "./plugins:/app/plugins:ro" in compose
    assert "/app/switchyard-plugin/src:/app/plugins" in compose
    assert "CHEAP_MODEL: openai/${CHEAP_MODEL_ID" in compose
    assert "EXPENSIVE_MODEL: openai/${EXPENSIVE_MODEL_ID" in compose


def test_dockerfile_smoke_test_covers_bundled_toml():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "profiles/stage/switchyard.toml" in dockerfile
    assert "ROUTING_PLUGIN" in dockerfile
