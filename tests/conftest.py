"""Shared fixtures for proxy tests. Stdlib only (no extra deps)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must match the provider prefix prepended in compose.yaml
# (CHEAP_MODEL: openai/${CHEAP_MODEL_ID}).
PROVIDER_PREFIX = "openai/"


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@pytest.fixture(scope="session")
def proxy_url() -> str:
    port = os.environ.get("LITELLM_PORT", "4000")
    # `or` (not just get-default): CI sets PROXY_URL to "" when unconfigured.
    return os.environ.get("PROXY_URL") or f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def master_key() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY")
    assert key, "LITELLM_MASTER_KEY must be set (source .env first)"
    assert key != "sk-CHANGE-ME", "LITELLM_MASTER_KEY still has the placeholder value"
    return key


@pytest.fixture(scope="session")
def expected_pair() -> dict[str, str]:
    """Full LiteLLM model strings for both tiers, mirroring compose.yaml."""
    return {
        "cheap": PROVIDER_PREFIX + os.environ["CHEAP_MODEL_ID"],
        "expensive": PROVIDER_PREFIX + os.environ["EXPENSIVE_MODEL_ID"],
    }


@pytest.fixture(scope="session")
def expected_pair2() -> dict[str, str] | None:
    """Pair 2 (_N suffix guideline), or None on the single-pair default."""
    if os.environ.get("LITELLM_CONFIG_FILE", "litellm.yaml") != "litellm.multipair.yaml":
        return None
    return {
        "cheap": PROVIDER_PREFIX + os.environ["CHEAP_MODEL_ID_2"],
        "expensive": PROVIDER_PREFIX + os.environ["EXPENSIVE_MODEL_ID_2"],
    }


@pytest.fixture(scope="session")
def expected_group2() -> str | None:
    """Routed group name for pair 2, or None on the single-pair default."""
    if os.environ.get("LITELLM_CONFIG_FILE", "litellm.yaml") != "litellm.multipair.yaml":
        return None
    return os.environ.get("SWITCHYARD_GROUP_2", "switchyard_2")


@pytest.fixture(scope="session")
def expected_group() -> str:
    """Routed group name for pair 1 (SWITCHYARD_GROUP, default switchyard)."""
    return os.environ.get("SWITCHYARD_GROUP", "switchyard")


def api_get(url: str, key: str) -> tuple[int, dict, object]:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()[:2000]


def api_post(
    url: str, key: str, payload: dict, extra_request_headers: dict | None = None
) -> tuple[int, dict, object]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    headers.update(extra_request_headers or {})
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()[:2000]
