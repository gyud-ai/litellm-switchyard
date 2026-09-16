"""Static checks: run anywhere, no proxy needed (`pytest tests/ -m 'not live'`)."""

from __future__ import annotations

import py_compile
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _deployments_of(config, group: str) -> list[dict]:
    return [d for d in config["model_list"] if d["model_name"] == group]


def test_litellm_yaml_has_exactly_two_ordered_switchyard_deployments():
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    deployments = _deployments_of(config, "switchyard")
    assert len(deployments) == 2, "stage routing requires exactly two deployments"
    # Order is the capable/efficient contract: expensive first, cheap second.
    models = [d["litellm_params"]["model"] for d in deployments]
    assert models == ["os.environ/EXPENSIVE_MODEL", "os.environ/CHEAP_MODEL"], models
    for deployment in deployments:
        params = deployment["litellm_params"]
        assert params["api_base"].startswith("os.environ/")
        assert params["api_key"].startswith("os.environ/")


def test_tier_metadata_and_reasoning_wiring():
    """Both tiers expose the same surface: effort defaults from env (via
    extra_body: LiteLLM rejects the bare reasoning_effort param for
    openai-prefixed custom models) and endpoint-verified model_info.
    Text-only vision defaults."""
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    expensive, cheap = (
        d["litellm_params"] for d in _deployments_of(config, "switchyard")
    )
    assert "reasoning_effort" not in expensive, (
        "bare reasoning_effort default 400s every request (UnsupportedParamsError)"
    )
    assert expensive["extra_body"] == {
        "reasoning_effort": "os.environ/EXPENSIVE_REASONING_EFFORT"
    }
    assert "max_tokens" not in expensive, (
        "per-request output cap removed by design; max_output_tokens is "
        "declarative metadata, not an enforced cap"
    )
    # Symmetric exposure: cheap carries the same knob (accepted-but-ignored
    # by the current backend; live if a future cheap backend honors effort).
    assert cheap["extra_body"] == {
        "reasoning_effort": "os.environ/CHEAP_REASONING_EFFORT"
    }
    assert "max_tokens" not in cheap
    exp_info, cheap_info = (
        d.get("model_info", {}) for d in _deployments_of(config, "switchyard")
    )
    for flag in (
        "supports_reasoning",
        "supports_function_calling",
        "supports_vision",
    ):
        assert exp_info[flag] == cheap_info[flag], (
            f"{flag} differs across tiers; Switchyard routes mid-conversation, "
            "so capabilities must match for consistent behavior"
        )
    assert exp_info["max_input_tokens"] == "os.environ/EXPENSIVE_MAX_INPUT_TOKENS"
    assert exp_info["max_output_tokens"] == "os.environ/EXPENSIVE_MAX_OUTPUT_TOKENS"
    assert exp_info["supports_reasoning"] is True
    assert exp_info["supports_function_calling"] is True
    assert exp_info["supports_vision"] is False
    assert cheap_info["max_input_tokens"] == "os.environ/CHEAP_MAX_INPUT_TOKENS"
    assert cheap_info["max_output_tokens"] == "os.environ/CHEAP_MAX_OUTPUT_TOKENS"
    assert cheap_info["supports_reasoning"] is True
    assert cheap_info["supports_function_calling"] is True
    assert cheap_info["supports_vision"] is False
    assert "reasoning_effort" not in cheap
    for deployment in config["model_list"]:
        params = deployment["litellm_params"]
        assert params["timeout"] == (
            f"os.environ/{'EXPENSIVE' if 'EXPENSIVE' in params['model'] else 'CHEAP'}"
            "_MODEL_TIMEOUT"
        ), deployment["model_name"]
        assert params["max_retries"] == (
            f"os.environ/{'EXPENSIVE' if 'EXPENSIVE' in params['model'] else 'CHEAP'}"
            "_MODEL_RETRIES"
        ), deployment["model_name"]
    compose = (REPO_ROOT / "compose.yaml").read_text()
    assert "CHEAP_REASONING_EFFORT: ${CHEAP_REASONING_EFFORT:-low}" in compose
    assert "EXPENSIVE_REASONING_EFFORT: ${EXPENSIVE_REASONING_EFFORT:-max}" in compose
    assert "CHEAP_MODEL_TIMEOUT: ${CHEAP_MODEL_TIMEOUT:-300}" in compose
    assert "CHEAP_MODEL_RETRIES: ${CHEAP_MODEL_RETRIES:-2}" in compose
    assert "EXPENSIVE_MODEL_TIMEOUT: ${EXPENSIVE_MODEL_TIMEOUT:-300}" in compose
    assert "EXPENSIVE_MODEL_RETRIES: ${EXPENSIVE_MODEL_RETRIES:-2}" in compose
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "CHEAP_REASONING_EFFORT=low" in env_example
    assert "EXPENSIVE_REASONING_EFFORT=max" in env_example
    assert "CHEAP_MAX_INPUT_TOKENS=131072" in env_example
    assert "EXPENSIVE_MAX_INPUT_TOKENS=1048576" in env_example
    assert "CHEAP_MAX_TOKENS" not in env_example
    assert "EXPENSIVE_MAX_TOKENS" not in env_example
    assert "CHEAP_MAX_OUTPUT_TOKENS=32768" in env_example
    assert "EXPENSIVE_MAX_OUTPUT_TOKENS=262144" in env_example
    assert "CHEAP_MODEL_TIMEOUT=300" in env_example
    assert "CHEAP_MODEL_RETRIES=2" in env_example
    assert "EXPENSIVE_MODEL_TIMEOUT=300" in env_example
    assert "EXPENSIVE_MODEL_RETRIES=2" in env_example
    assert "MODEL_NAME" not in env_example, "dead MODEL_NAME vars must go"


def test_model_info_booleans_stay_literal():
    """Env values arrive as strings and model_info is a plain TypedDict with
    no Pydantic coercion — the string "false" is truthy. Capability flags
    must be literal booleans, never os.environ/ refs."""
    source = (REPO_ROOT / "profiles" / "stage" / "litellm.yaml").read_text()
    assert "os.environ/SUPPORT" not in source
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    for deployment in config["model_list"]:
        info = deployment.get("model_info", {})
        for flag in (
            "supports_reasoning",
            "supports_function_calling",
            "supports_vision",
        ):
            assert info[flag] is True or info[flag] is False, (deployment, flag)


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


def test_litellm_yaml_defines_headroom_pre_call_guardrail_opt_in():
    """Headroom must run as a pre_call guardrail AFTER routing (opt-in first).

    Routing (router_settings.plugins) scores pristine client messages; the
    guardrail POSTs {messages, model} to the sidecar and LiteLLM forwards the
    compressed payload itself. `default_on: false` keeps the rollout opt-in
    via per-request `guardrails` / per-key attach until live tests go green.
    """
    with open(REPO_ROOT / "profiles" / "stage" / "litellm.yaml") as fh:
        config = yaml.safe_load(fh)
    guardrails = config.get("guardrails")
    assert isinstance(guardrails, list) and len(guardrails) == 1, guardrails
    entry = guardrails[0]
    assert entry.get("guardrail_name") == "headroom-compression", entry
    params = entry.get("litellm_params", {})
    assert params.get("guardrail") == "headroom", params
    assert params.get("mode") == "pre_call", params
    assert params.get("api_base") == "os.environ/HEADROOM_API_BASE", params
    # Env-owned so flipping needs no YAML edit. The value must stay an
    # os.environ/ reference (LiteLLM interpolates whole values only) pointing
    # at a strict true/false string: pydantic coerces to bool (verified live
    # in-container), while an empty string fails proxy startup.
    assert params.get("default_on") == "os.environ/HEADROOM_DEFAULT_ON", params


def test_compose_defines_single_headroom_sidecar():
    """One stateless Headroom sidecar; backends stay direct (no api_base chain)."""
    import re

    compose = (REPO_ROOT / "compose.yaml").read_text()
    # Single headroom service built from the pinned Dockerfile.
    assert "dockerfile: Dockerfile.headroom" in compose
    assert "ghcr.io/gyud-ai/litellm-switchyard-headroom:0.27.0" in compose
    # Mandatory sidecar env: remote access (else /v1/compress 404s) + user-role
    # compression (else requests_compressed stays 0) + local-only telemetry.
    assert 'HEADROOM_COMPRESS_ALLOW_REMOTE: "1"' in compose
    assert 'HEADROOM_COMPRESS_USER_MESSAGES: "1"' in compose
    assert 'HEADROOM_TELEMETRY: "on"' in compose
    assert 'HEADROOM_BEACON: "off"' in compose
    # LiteLLM reaches the sidecar internally; backends are NOT rewired.
    assert "HEADROOM_API_BASE: http://headroom:8787" in compose
    assert "HEADROOM_DEFAULT_ON: ${HEADROOM_DEFAULT_ON:-false}" in compose
    assert "http://headroom-cheap" not in compose
    assert "http://headroom-expensive" not in compose
    assert "REAL_CHEAP_API_BASE" not in compose
    # Loopback-only stats port for host pytest, never public.
    assert "127.0.0.1:${HEADROOM_PORT:-8788}:8787" in compose
    assert re.search(r"headroom:\s*\n\s*condition: service_started", compose), (
        "litellm must depend on headroom (service_started so a slow sidecar "
        "cannot block boot; fail-open behavior is asserted live)"
    )


def test_compose_pulls_ghcr_images_with_local_build_fallback():
    """Both services default to version-pinned GHCR images (published by
    publish.yml) while keeping a local `build:` block, so fresh machines
    deploy with plain `up -d` and Dockerfile work still builds locally."""
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    expected = {
        "litellm": "ghcr.io/gyud-ai/litellm-switchyard-proxy:1.97.0",
        "headroom": "ghcr.io/gyud-ai/litellm-switchyard-headroom:0.27.0",
    }
    for service, default_image in expected.items():
        svc = compose["services"][service]
        assert "build" in svc, f"{service} must keep a local build fallback"
        assert default_image in svc["image"], (
            f"{service} image must default to {default_image}"
        )
        assert svc.get("pull_policy") != "build", (
            f"{service} must not force local builds (GHCR pull is the default)"
        )


def test_compose_binds_litellm_to_configurable_loopback_by_default():
    """Proxy bind is env-owned, loopback by default, never bare 0.0.0.0."""
    compose = (REPO_ROOT / "compose.yaml").read_text()
    assert "${LITELLM_IP:-127.0.0.1}:${LITELLM_PORT:-4000}:4000" in compose
    assert '"0.0.0.0:${LITELLM_PORT' not in compose
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "LITELLM_IP=127.0.0.1" in env_example


def test_dockerfile_headroom_pins_version():
    dockerfile = (REPO_ROOT / "Dockerfile.headroom").read_text()
    assert "python:3.13-slim" in dockerfile
    assert "headroom-ai[proxy]==0.27.0" in dockerfile
    assert '"--host", "0.0.0.0", "--port", "8787"' in dockerfile


def test_env_example_documents_headroom():
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "HEADROOM_PORT=8788" in env_example
    assert "HEADROOM_LOG_LEVEL=warning" in env_example
    assert "HEADROOM_DEFAULT_ON=false" in env_example
