"""HTTP compatibility, streaming ownership, and privacy checks."""

import asyncio
import json

import httpx
import pytest
from conftest import Response

from switchyard_gateway.adapters.ingress import OwnedStream, create_app
from switchyard_gateway.domain import GatewayError

pytestmark = pytest.mark.integration


class TestChatIngress:
    async def test_alias_extensions_and_usage(self, gateway, request_body):
        gateway.transport.responses = [
            Response(
                chunks=[
                    json.dumps(
                        {
                            "model": "backend",
                            "choices": [{"message": {"content": "private output"}}],
                            "usage": {
                                "prompt_tokens": 12,
                                "completion_tokens": 3,
                                "untrusted_field": "private output",
                            },
                        }
                    ).encode()
                ]
            )
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 200
        assert response.json()["model"] == "switchyard"
        assert response.headers["x-gateway-model"] == "cheap"
        assert response.headers["x-gateway-endpoint"] == "a"
        assert gateway.events.records[-1]["usage"] == {"prompt_tokens": 12, "completion_tokens": 3}
        assert "private output" not in json.dumps(gateway.events.records)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"model": "switchyard", "messages": []},
            {"model": "switchyard", "messages": [{"role": "user", "content": 1}]},
            {"model": "switchyard", "messages": [{"role": "tool", "content": "x"}]},
        ],
    )
    async def test_invalid_requests_are_400(self, gateway, payload):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions", json=payload, headers={"authorization": "Bearer client-key"}
            )
        assert response.status_code == 400
        assert gateway.transport.calls == []

    async def test_authentication_and_model_discovery(self, gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            assert (await client.get("/health/readiness")).status_code == 200
            assert (await client.get("/v1/models")).status_code == 401
            models = await client.get("/v1/models", headers={"authorization": "Bearer client-key"})
            assert [row["id"] for row in models.json()["data"]] == [
                "switchyard",
                "cheap",
                "expensive",
            ]

    async def test_upstream_error_body_not_exposed(self, gateway, request_body):
        upstream = Response(400, [b'{"error":"https://private-host credential"}'])
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 400
        assert "private-host" not in response.text
        assert upstream.closed

    async def test_invalid_upstream_json_closes_response(self, gateway, request_body):
        upstream = Response(chunks=[b"not-json"])
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 502
        assert upstream.closed


class TestStreams:
    async def test_fragmented_sse_preserves_tools_and_usage(self, gateway, request_body):
        delta = {
            "model": "backend",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "echo", "arguments": '{"x":'},
                            }
                        ]
                    }
                }
            ],
        }
        usage = {"model": "backend", "choices": [], "usage": {"total_tokens": 12}}
        wire = (
            ": keepalive\r\n\r\ndata: "
            + json.dumps(delta)
            + "\r\n\r\ndata: "
            + json.dumps(usage)
            + "\n\ndata: [DONE]\n\n"
        ).encode()
        upstream = Response(
            chunks=[bytes([b]) for b in wire], headers={"content-type": "text/event-stream"}
        )
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body | {"stream": True},
                headers={"authorization": "Bearer client-key"},
            )
        values = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert values[0]["choices"] == delta["choices"]
        assert all(value["model"] == "switchyard" for value in values)
        assert response.text.endswith("data: [DONE]\n\n")
        assert upstream.closed
        assert gateway.events.records[-1]["outcome"] == "completed"
        assert gateway.events.records[-1]["usage"] == {"total_tokens": 12}

    @pytest.mark.parametrize("wire", [b"data: {bad}\n\n", b'data: {"choices":[]}\n\n'])
    async def test_malformed_or_incomplete_stream_is_interrupted(self, gateway, request_body, wire):
        upstream = Response(chunks=[wire], headers={"content-type": "text/event-stream"})
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body | {"stream": True},
                headers={"authorization": "Bearer client-key"},
            )
        assert "[DONE]" not in response.text
        assert upstream.closed
        assert len(gateway.transport.calls) == 1
        assert gateway.events.records[-1]["outcome"] == "interrupted"

    async def test_midstream_read_failure_never_retries(self, gateway, request_body):
        upstream = Response(
            chunks=[b'data: {"choices":[]}\n\n'], headers={"content-type": "text/event-stream"}
        )
        upstream.error = GatewayError("upstream_read_failed")
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            await client.post(
                "/v1/chat/completions",
                json=request_body | {"stream": True},
                headers={"authorization": "Bearer client-key"},
            )
        assert upstream.closed
        assert len(gateway.transport.calls) == 1
        assert gateway.events.records[-1]["outcome"] == "interrupted"

    async def test_cancellation_before_stream_iteration_releases_upstream(
        self, gateway, request_body
    ):
        exchange = await gateway.open(request_body, {}, "test")
        stream = OwnedStream(gateway, exchange, "switchyard", {})

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await stream({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert exchange.response.closed
        assert gateway.events.records[-1]["outcome"] == "cancelled"


class TestRequestLimits:
    async def test_rejects_nonfinite_json(self, gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=b'{"model":"switchyard","temperature":NaN,"messages":[]}',
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 400
        assert gateway.transport.calls == []

    async def test_rejects_oversized_request_before_routing(self, gateway, request_body):
        from dataclasses import replace

        gateway.settings = replace(gateway.settings, max_request_bytes=8)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 413
        assert gateway.router.calls == []
