"""Explicit real-backend checks; use pytest -m live with environment configured."""

import os

import httpx
import pytest

pytestmark = pytest.mark.live


class TestLiveGateway:
    def test_first_turn_and_escalation(self):
        url = os.environ.get("PROXY_URL")
        key = os.environ.get("GATEWAY_API_KEY")
        pair = os.environ.get("LIVE_PAIR")
        efficient = os.environ.get("LIVE_EFFICIENT_MODEL")
        capable = os.environ.get("LIVE_CAPABLE_MODEL")
        if not all((url, key, pair, efficient, capable)):
            pytest.fail(
                "Set PROXY_URL, GATEWAY_API_KEY, LIVE_PAIR, "
                "LIVE_EFFICIENT_MODEL, LIVE_CAPABLE_MODEL"
            )
        with httpx.Client(
            base_url=url,
            headers={"authorization": f"Bearer {key}", "x-opencode-session": "gateway-live-check"},
            timeout=300,
        ) as client:
            assert client.get("/health/readiness").status_code == 200
            assert pair in [m["id"] for m in client.get("/v1/models").json()["data"]]
            first = client.post(
                "/v1/chat/completions",
                json={
                    "model": pair,
                    "messages": [{"role": "user", "content": "Reply hello"}],
                    "max_tokens": 64,
                },
            )
            assert first.status_code == 200
            assert first.headers["x-gateway-model"] == efficient
            escalated = client.post(
                "/v1/chat/completions",
                json={
                    "model": pair,
                    "max_tokens": 64,
                    "messages": [
                        {"role": "user", "content": "Train the model"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {"name": "bash", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call", "content": "CUDA out of memory"},
                    ],
                },
            )
            assert escalated.status_code == 200
            assert escalated.headers["x-gateway-model"] == capable
