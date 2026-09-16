"""Live checks against a running stack (`pytest tests/ -m live`).

Requires the proxy up (`docker compose up -d --wait`) with `.env` sourced
(conftest.py loads it automatically). These spend real backend calls.
"""

from __future__ import annotations

import pytest

from conftest import api_get, api_post

pytestmark = pytest.mark.live


def _routed_to(headers: dict) -> str | None:
    for key, value in headers.items():
        if key.lower() == "x-litellm-model-name":
            return value
    return None


def test_liveliness(proxy_url):
    status, _, body = api_get(f"{proxy_url}/health/liveliness", key="unused")
    assert status == 200


def test_models_lists_all_groups(proxy_url, master_key, expected_pair):
    status, _, body = api_get(f"{proxy_url}/v1/models", master_key)
    assert status == 200
    ids = [m["id"] for m in body["data"]]
    assert {"switchyard", expected_pair["cheap"], expected_pair["expensive"]} <= set(
        ids
    ), ids


def test_chat_round_trip_reports_selected_model(proxy_url, master_key, expected_pair):
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": "switchyard",
            "messages": [{"role": "user", "content": "Reply with the word hello."}],
            "max_tokens": 256,
        },
    )
    assert status == 200, body
    assert body["model"] == "switchyard"
    assert body["choices"], "no choices returned"
    assert body["choices"][0]["message"]["content"], "empty reply content"
    routed = _routed_to(headers)
    assert routed in (expected_pair["cheap"], expected_pair["expensive"]), (
        f"routed outside the switchyard pair: {routed!r}"
    )


def test_critical_tool_error_escalates_with_forwarded_session_header(
    proxy_url, master_key, expected_pair
):
    """OOM tool transcript must escalate to the expensive tier, and the plain
    x-opencode-session HTTP header must reach the backend (200, not the
    backend's missing-header 400). Spends one expensive-tier call."""
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": "switchyard",
            "max_tokens": 3000,
            "messages": [
                {"role": "user", "content": "Train the model on the full dataset."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_train1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "python train.py"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_train1",
                    "content": "Traceback (most recent call last):\n"
                    "torch.cuda.OutOfMemoryError: CUDA out of memory.",
                },
                {
                    "role": "user",
                    "content": "It crashed with out of memory. How do we fix it?",
                },
            ],
        },
        extra_request_headers={"x-opencode-session": "pytest-escalation-probe"},
    )
    assert status == 200, body
    routed = _routed_to(headers)
    assert routed == expected_pair["expensive"], (
        f"critical tool error did not escalate, routed to {routed!r}"
    )


def test_direct_cheap_route_hits_cheap_tier(proxy_url, master_key, expected_pair):
    """Single-deployment group named by the backend ID bypasses stage routing."""
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": expected_pair["cheap"],
            "messages": [{"role": "user", "content": "Reply with the word hello."}],
            "max_tokens": 256,
        },
    )
    assert status == 200, body
    assert body["model"] == expected_pair["cheap"]
    assert _routed_to(headers) == expected_pair["cheap"]


def test_direct_expensive_route_hits_expensive_tier(
    proxy_url, master_key, expected_pair
):
    """Single-deployment group named by the backend ID bypasses stage routing;
    the plain session header must still reach the backend (forwarding covers
    all groups). Spends one expensive-tier call."""
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": expected_pair["expensive"],
            "messages": [{"role": "user", "content": "Reply with the word hello."}],
            "max_tokens": 256,
        },
        extra_request_headers={"x-opencode-session": "pytest-direct-probe"},
    )
    assert status == 200, body
    assert body["model"] == expected_pair["expensive"]
    assert _routed_to(headers) == expected_pair["expensive"]


def test_first_turn_without_tool_history_stays_efficient(
    proxy_url, master_key, expected_pair
):
    """efficient_first default: a clean first turn must not escalate."""
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": "switchyard",
            "messages": [{"role": "user", "content": "Reply with the word hello."}],
            "max_tokens": 256,
        },
    )
    assert status == 200, body
    assert _routed_to(headers) == expected_pair["cheap"]
