"""LLM-free smoke test: real Switchyard and Headroom end to end over the ASGI app."""

import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from switchyard_gateway.adapters.headroom import HeadroomCompressor
from switchyard_gateway.adapters.httpx import HttpxTransport
from switchyard_gateway.adapters.ingress import create_app
from switchyard_gateway.adapters.logging import JsonEvents
from switchyard_gateway.adapters.switchyard import SwitchyardRouter
from switchyard_gateway.application import Gateway
from switchyard_gateway.domain import Endpoint, Model, Pair, Settings

pytestmark = pytest.mark.smoke

_API_KEY = "smoke-client-key"
_TOOL_PAYLOAD = json.dumps(
    [{"id": index, "status": "ok", "region": "west", "count": index} for index in range(300)]
)


class FakeBackend:
    """Record synthetic OpenAI-compatible requests and answer without inference."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"path": request.url.path, "body": body})
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )


def _settings() -> Settings:
    cheap = Model(
        "cheap", "cheap-backend", (Endpoint("a", "http://cheap.invalid/v1", "backend-key"),)
    )
    expensive = Model(
        "expensive",
        "expensive-backend",
        (Endpoint("c", "http://expensive.invalid/v1", "backend-key"),),
    )
    return Settings(
        {"cheap": cheap, "expensive": expensive},
        {"switchyard": Pair("switchyard", "expensive", "cheap")},
        _API_KEY,
    )


def _escalation_history() -> list[dict]:
    return [
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "old result"},
        {"role": "user", "content": "current request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "live-call",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "pwd"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "live-call",
            "content": "torch.cuda.OutOfMemoryError: CUDA out of memory",
        },
    ]


def _tool_history(result: str) -> list[dict]:
    return [
        {"role": "system", "content": "retain-system"},
        {"role": "user", "content": "fetch records"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call", "type": "function", "function": {"name": "fetch", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": result},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "summarize"},
    ]


@asynccontextmanager
async def wired(backend: Callable[[httpx.Request], Any]) -> AsyncIterator[dict[str, Any]]:
    """Wire the real adapters around a fake backend and yield the live app plus recorder."""
    log = io.StringIO()
    settings = _settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(backend), timeout=10)
    compressor = HeadroomCompressor(workers=1)
    events = JsonEvents(output=log)
    gateway = Gateway(
        settings, SwitchyardRouter(settings.stage), compressor, HttpxTransport(client), events
    )
    app = create_app(gateway)
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway",
        headers={"authorization": f"Bearer {_API_KEY}"},
    )
    try:
        yield {"api": api, "log": log, "requests": backend.requests}
    finally:
        await api.aclose()
        await client.aclose()
        await compressor.close()


def _records(log: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in log.getvalue().splitlines()]


async def test_smoke_routes_first_turn_to_efficient():
    backend = FakeBackend()
    async with wired(backend) as env:
        response = await env["api"].post(
            "/v1/chat/completions",
            json={"model": "switchyard", "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200
    assert response.headers["x-gateway-model"] == "cheap"
    assert response.json()["model"] == "switchyard"
    assert backend.requests[-1]["body"]["model"] == "cheap-backend"


async def test_smoke_escalates_on_failure_signal_and_compresses_long_history():
    backend = FakeBackend()
    async with wired(backend) as env:
        escalated = await env["api"].post(
            "/v1/chat/completions",
            json={"model": "switchyard", "messages": _escalation_history()},
        )
        assert escalated.status_code == 200
        assert escalated.headers["x-gateway-model"] == "expensive"
        assert backend.requests[-1]["body"]["model"] == "expensive-backend"

        history = _tool_history(_TOOL_PAYLOAD)
        compressed = await env["api"].post(
            "/v1/chat/completions", json={"model": "switchyard", "messages": history}
        )
        assert compressed.status_code == 200
        sent = backend.requests[-1]["body"]["messages"]
        assert history[0] in sent
        assert history[-1] in sent
        assert any(row.get("tool_calls") == history[2]["tool_calls"] for row in sent)
        sent_tool = next(row for row in sent if row.get("role") == "tool")
        assert len(sent_tool["content"]) < len(_TOOL_PAYLOAD)

        bypassed = await env["api"].post(
            "/v1/chat/completions",
            json={"model": "switchyard", "messages": _tool_history(_TOOL_PAYLOAD)},
            headers={"x-headroom-bypass": "true"},
        )
        assert bypassed.status_code == 200
        assert history[3] in backend.requests[-1]["body"]["messages"]

    records = _records(env["log"])
    assert any(record.get("compression") == "savings" for record in records)
    assert any(record.get("compression") == "bypass" for record in records)


async def test_smoke_never_logs_payloads_or_credentials():
    backend = FakeBackend()
    async with wired(backend) as env:
        response = await env["api"].post(
            "/v1/chat/completions",
            json={"model": "switchyard", "messages": _tool_history(_TOOL_PAYLOAD)},
        )
        assert response.status_code == 200
        log = env["log"].getvalue()
    assert _TOOL_PAYLOAD not in log
    assert "summarize" not in log
    for secret in (_API_KEY, "backend-key", "cheap.invalid", "expensive.invalid"):
        assert secret not in log
