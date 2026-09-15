"""Live Headroom guardrail checks (`pytest tests/ -m live`).

Requires the stack up (`docker compose up -d --wait`) with `.env` sourced
(conftest.py loads it automatically). Spends real backend calls.

Contract under test: Switchyard routes on pristine client messages FIRST,
then the `headroom-compression` pre_call guardrail compresses in-flight via
POST http://headroom:8787/v1/compress, then LiteLLM forwards to the backend
itself. Compression must never move the tier decision.

Two 1.97.0 behaviors shape these tests (verified against the shipped hook):

- `x-litellm-applied-guardrails` reflects *scheduling*, not execution: it is
  present even when `x-headroom-bypass: true` skips compression or the
  sidecar is down. Effect-level assertions therefore read the spend log row
  (`guardrail_information`), not the header.
- LiteLLM holds back system rows, the last user row, the last assistant row,
  and their whole tool exchanges from the compress payload (live-turn
  protection). Only *older* history compresses, so compression tests need at
  least two tool exchanges; the newest exchange always goes through intact.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

import pytest

from conftest import api_get, api_post

pytestmark = pytest.mark.live

GUARDRAIL = "headroom-compression"


def _header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _routed_to(headers: dict) -> str | None:
    return _header(headers, "x-litellm-model-name")


def _applied_guardrails(headers: dict) -> str:
    return _header(headers, "x-litellm-applied-guardrails") or ""


def _headroom_url() -> str:
    port = os.environ.get("HEADROOM_PORT", "8788")
    return os.environ.get("HEADROOM_URL", f"http://127.0.0.1:{port}")


def _spend_log_for(proxy_url: str, master_key: str, request_id: str) -> dict | None:
    """Fetch the spend-log row for one chat completion id (retried).

    Returns None when the row is not visible yet. `guardrail_information`
    on the row is the effect-level record: success + token stats when the
    guardrail compressed, None when bypassed or the sidecar was down.
    """
    for _ in range(10):
        req = urllib.request.Request(
            f"{proxy_url}/spend/logs?limit=50",
            headers={"Authorization": f"Bearer {master_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                rows = json.loads(resp.read().decode())
        except Exception:
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("request_id") == request_id:
                    return row
        time.sleep(2)
    return None


def _guardrail_info(row: dict | None) -> dict | None:
    if not row:
        return None
    info = (row.get("metadata") or {}).get("guardrail_information")
    if isinstance(info, list):
        return info[0] if info else None
    return info if isinstance(info, dict) else None


def _chat(proxy_url, master_key, messages, guardrail=True, extra_headers=None):
    payload: dict = {
        "model": "switchyard",
        "messages": messages,
        "max_tokens": 256,
    }
    if guardrail:
        payload["guardrails"] = [GUARDRAIL]
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        payload,
        extra_request_headers=extra_headers,
    )
    assert status == 200, body
    return headers, body


def test_headroom_sidecar_healthy():
    status, _, body = api_get(f"{_headroom_url()}/health", key="unused")
    assert status == 200, body


def test_baseline_without_guardrail_has_no_applied_header(
    proxy_url, master_key, expected_pair
):
    """Control: no opt-in means no compression header, routing still in-pair."""
    headers, _ = _chat(
        proxy_url,
        master_key,
        [{"role": "user", "content": "Reply with the word hello."}],
        guardrail=False,
    )
    assert GUARDRAIL not in _applied_guardrails(headers)
    assert _routed_to(headers) in (
        expected_pair["cheap"],
        expected_pair["expensive"],
    )


def test_guardrail_opt_in_preserves_routing(proxy_url, master_key, expected_pair):
    """Same prompt + guardrail: tier stays in-pair AND guardrail ran."""
    headers, body = _chat(
        proxy_url,
        master_key,
        [{"role": "user", "content": "Reply with the word hello."}],
        guardrail=True,
    )
    assert body["model"] == "switchyard"
    assert _routed_to(headers) in (
        expected_pair["cheap"],
        expected_pair["expensive"],
    )
    assert GUARDRAIL in _applied_guardrails(headers), (
        f"guardrail did not run; headers={headers}"
    )


def test_first_turn_with_guardrail_stays_efficient(
    proxy_url, master_key, expected_pair
):
    """efficient_first default holds with compression on (no false escalation)."""
    headers, _ = _chat(
        proxy_url,
        master_key,
        [{"role": "user", "content": "Reply with the word hello."}],
        guardrail=True,
    )
    assert _routed_to(headers) == expected_pair["cheap"]
    assert GUARDRAIL in _applied_guardrails(headers)


def test_critical_tool_error_with_guardrail_still_escalates(
    proxy_url, master_key, expected_pair
):
    """OOM transcript + guardrail must still escalate AND forward x-headers."""
    headers, _ = _chat(
        proxy_url,
        master_key,
        [
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
        guardrail=True,
        extra_headers={"x-opencode-session": "pytest-guardrail-escalation"},
    )
    assert _routed_to(headers) == expected_pair["expensive"], (
        "compression moved the tier decision (or header forwarding broke)"
    )
    assert GUARDRAIL in _applied_guardrails(headers)


def test_bypass_header_skips_compression(proxy_url, master_key, expected_pair):
    """x-headroom-bypass:true skips execution (scheduling header still present).

    The applied-guardrails header reflects scheduling, so the effect-level
    proof is the spend log: a bypassed call records no guardrail_information,
    while an executed one records success + token stats.
    """
    headers, body = _chat(
        proxy_url,
        master_key,
        [{"role": "user", "content": "Reply with the word hello."}],
        guardrail=True,
        extra_headers={"x-headroom-bypass": "true"},
    )
    assert _routed_to(headers) in (
        expected_pair["cheap"],
        expected_pair["expensive"],
    )
    row = _spend_log_for(proxy_url, master_key, body["id"])
    assert row is not None, "spend log row missing for bypassed call"
    assert _guardrail_info(row) is None, (
        f"bypassed call still compressed: {_guardrail_info(row)}"
    )


def test_large_tool_payload_reports_compression(proxy_url, master_key, expected_pair):
    """Older 200-row tool exchange compresses; newest exchange goes intact.

    LiteLLM holds back the last user/assistant rows and their whole tool
    exchange, so the big payload must sit in an OLDER exchange to be sent to
    /v1/compress (direct probe: 5690 -> 2492 tokens on this shape).
    """
    rows = [
        {"id": i, "status": "ok", "host": "worker-7", "detail": "all checks passed"}
        for i in range(200)
    ]
    # Corroboration trap: embed the error at the END so boundary-keep matters.
    rows.append({"id": 999, "status": "error", "detail": "FATAL: disk full on shard 3"})
    headers, body = _chat(
        proxy_url,
        master_key,
        [
            {"role": "user", "content": "Summarize the batch job results."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_batch1",
                        "type": "function",
                        "function": {
                            "name": "get_batch_results",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_batch1",
                "content": json.dumps(rows),
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_batch2",
                        "type": "function",
                        "function": {
                            "name": "get_status",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_batch2",
                "content": "worker-7: idle",
            },
            {"role": "user", "content": "Any failures? Quote the FATAL line."},
        ],
        guardrail=True,
    )
    assert _routed_to(headers) in (
        expected_pair["cheap"],
        expected_pair["expensive"],
    )
    assert GUARDRAIL in _applied_guardrails(headers)
    row = _spend_log_for(proxy_url, master_key, body["id"])
    assert row is not None, "spend log row missing for compression call"
    info = _guardrail_info(row)
    assert info is not None and info.get("guardrail_status") == "success", info
    response = info.get("guardrail_response") or {}
    assert response.get("tokens_saved", 0) > 0, (
        f"expected tokens_saved > 0 on 200-row older exchange: {response}"
    )
