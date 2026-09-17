"""HTTP compatibility, streaming ownership, and privacy checks."""

import asyncio
import json

import httpx
import pytest
from conftest import Compressor, Events, Response, Router, Transport
from hypothesis import given
from hypothesis import strategies as st

from switchyard_gateway.adapters.ingress import OwnedStream, create_app
from switchyard_gateway.application import Gateway
from switchyard_gateway.domain import Endpoint, GatewayError, Model, Pair, Settings

pytestmark = pytest.mark.integration

_DONE_FRAME = "data: [DONE]"
_INTERRUPTED_FRAME = (
    'data: {"error":{"message":"upstream_stream_interrupted",'
    '"type":"gateway_error","code":"upstream_stream_interrupted"}}'
)


def _data_frames(response: httpx.Response) -> list[str]:
    return [line for line in response.text.splitlines() if line.startswith("data: ")]


def _gateway() -> Gateway:
    cheap = Model("cheap", "cheap-backend", (Endpoint("a", "http://replica-a/v1"),))
    return Gateway(
        Settings({"cheap": cheap}, {"switchyard": Pair("switchyard", "cheap", "cheap")}, "key"),
        Router(),
        Compressor(),
        Transport(),
        Events(),
    )


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


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

    async def test_unauthenticated_chat_is_rejected_with_an_event(self, gateway, request_body):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert gateway.transport.calls == []
        event = gateway.events.records[-1]
        assert event["event"] == "request"
        assert event["status"] == 401
        assert event["outcome"] == "rejected"
        assert event["error"] == "unauthorized"

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

    async def test_non_json_upstream_content_type_is_a_specific_error(self, gateway, request_body):
        upstream = Response(
            chunks=[b"<html>proxy interstitial</html>"], headers={"content-type": "text/html"}
        )
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
        assert response.json()["error"]["code"] == "invalid_upstream_content_type"
        assert upstream.closed
        assert gateway.events.records[-1]["error"] == "invalid_upstream_content_type"

    @pytest.mark.parametrize(
        "content_type",
        ["application/json; charset=utf-8", "APPLICATION/JSON", "application/vnd.api+json"],
    )
    async def test_json_content_types_are_accepted(self, gateway, request_body, content_type):
        upstream = Response(headers={"content-type": content_type})
        gateway.transport.responses = [upstream]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"authorization": "Bearer client-key"},
            )
        assert response.status_code == 200

    async def test_failed_close_keeps_the_built_completion(self, gateway, request_body):
        upstream = Response(close_error=RuntimeError("private close diagnostic"))
        gateway.transport.responses = [upstream]
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
        assert upstream.closed
        (event,) = gateway.events.records
        assert event["outcome"] == "failed"
        assert event["close_failed"] is True
        assert "private" not in json.dumps(gateway.events.records)


class TestHealthProbes:
    async def test_readiness_requires_an_eligible_replica(self, gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            ready = await client.get("/health/readiness")
            gateway._cooldown("cheap", "a")
            gateway._cooldown("cheap", "b")
            still_ready = await client.get("/health/readiness")
            gateway._cooldown("expensive", "c")
            not_ready = await client.get("/health/readiness")
            alive = await client.get("/health/liveliness")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ok"}
        assert still_ready.status_code == 200
        assert not_ready.status_code == 503
        assert not_ready.json() == {"status": "not_ready"}
        assert alive.status_code == 200
        assert gateway.events.records == []

    async def test_readiness_reports_ready_after_cooldown_expires(self, settings):
        now = [100.0]
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), lambda: now[0])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            gateway._cooldown("cheap", "a")
            gateway._cooldown("cheap", "b")
            gateway._cooldown("expensive", "c")
            not_ready = await client.get("/health/readiness")
            now[0] = 130.0
            ready = await client.get("/health/readiness")
        assert not_ready.status_code == 503
        assert ready.status_code == 200


class TestModelDiscoveryEvents:
    async def test_discovery_emits_an_event_for_success_and_rejection(self, gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            rejected = await client.get("/v1/models")
            completed = await client.get(
                "/v1/models", headers={"authorization": "Bearer client-key"}
            )
        assert rejected.status_code == 401
        assert completed.status_code == 200
        assert gateway.events.records == [
            {
                "event": "request",
                "request_id": rejected.headers["x-request-id"],
                "status": 401,
                "outcome": "rejected",
                "error": "unauthorized",
            },
            {
                "event": "request",
                "request_id": completed.headers["x-request-id"],
                "status": 200,
                "outcome": "completed",
            },
        ]


@given(st.booleans())
def test_every_discovery_request_emits_exactly_one_event(authorized):
    gateway = _gateway()
    headers = {"authorization": "Bearer key"} if authorized else {}

    async def exercise() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gateway"
        ) as client:
            return await client.get("/v1/models", headers=headers)

    response = _run(exercise())
    assert response.status_code == (200 if authorized else 401)
    assert len(gateway.events.records) == 1
    assert gateway.events.records[0]["outcome"] == ("completed" if authorized else "rejected")


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
        assert response.text.count(_DONE_FRAME) == 1
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
        frames = _data_frames(response)
        assert "[DONE]" not in response.text
        assert frames.count(_INTERRUPTED_FRAME) == 1
        assert frames[-1] == _INTERRUPTED_FRAME
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
            response = await client.post(
                "/v1/chat/completions",
                json=request_body | {"stream": True},
                headers={"authorization": "Bearer client-key"},
            )
        frames = _data_frames(response)
        assert frames == ['data: {"choices":[]}', _INTERRUPTED_FRAME]
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
