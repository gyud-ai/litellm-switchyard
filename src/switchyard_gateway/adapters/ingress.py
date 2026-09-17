"""OpenAI Chat Completions ingress, validation, and incremental SSE rewriting."""

import asyncio
import hmac
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from ..application import Exchange, Gateway
from ..domain import GatewayError, Payload

_EVENT_END = re.compile(rb"\r?\n\r?\n")
_MAX_RESPONSE = 32 * 1024 * 1024


def _usage(value: object) -> Payload | None:
    if not isinstance(value, dict):
        return None
    return {
        key: val
        for key, val in value.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
        and isinstance(val, int)
        and not isinstance(val, bool)
        and val >= 0
    }


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate(payload: object) -> Payload:
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise GatewayError("invalid_request", 400)
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise GatewayError("invalid_stream", 400)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError("invalid_messages", 400)
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "system",
            "developer",
            "user",
            "assistant",
            "tool",
        }:
            raise GatewayError("invalid_message", 400)
        content = message.get("content")
        if not (
            isinstance(content, str)
            or content is None
            or (
                isinstance(content, list)
                and all(
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    for block in content
                )
            )
        ):
            raise GatewayError("unsupported_message_content", 400)
        if content is None and message["role"] != "assistant":
            raise GatewayError("invalid_message_content", 400)
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise GatewayError("invalid_tool_calls", 400)
        for call in calls:
            try:
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not call["function"]["name"]
                    or not isinstance(call["function"].get("arguments"), str)
                    or not isinstance(json.loads(call["function"]["arguments"]), dict)
                ):
                    raise ValueError
            except ValueError, TypeError:
                raise GatewayError("invalid_tool_calls", 400) from None
        if message["role"] == "tool" and (
            not isinstance(message.get("tool_call_id"), str) or not message["tool_call_id"]
        ):
            raise GatewayError("invalid_tool_result", 400)
    return payload


def _error(error: GatewayError, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": error.code, "type": "gateway_error", "code": error.code}},
        status_code=error.status,
        headers={"x-request-id": request_id},
    )


async def sse_body(gateway: Gateway, exchange: Exchange, alias: str) -> AsyncIterator[bytes]:
    """Rewrite complete SSE events while preserving tool deltas and stream ordering."""
    buffer = b""
    done = False
    try:
        async for chunk in gateway.body(exchange):
            buffer += chunk
            while match := _EVENT_END.search(buffer):
                frame, buffer = buffer[: match.start()], buffer[match.end() :]
                if len(frame) > _MAX_RESPONSE:
                    raise GatewayError("upstream_event_too_large")
                lines = frame.decode("utf-8").splitlines()
                data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
                if data == "[DONE]":
                    done = True
                    yield b"data: [DONE]\n\n"
                    exchange.event["stream_outcome"] = "completed"
                    return
                if data:
                    value = json.loads(data)
                    if not isinstance(value, dict) or "error" in value:
                        raise GatewayError("upstream_stream_error")
                    if "model" in value:
                        value["model"] = alias
                    usage = _usage(value.get("usage"))
                    if usage is not None:
                        exchange.event["usage"] = usage
                    lines = [line for line in lines if not line.startswith("data:")]
                    lines.append("data: " + json.dumps(value, separators=(",", ":")))
                yield ("\n".join(lines) + "\n\n").encode()
            if len(buffer) > _MAX_RESPONSE:
                raise GatewayError("upstream_event_too_large")
        if not done:
            raise GatewayError("upstream_stream_incomplete")
    except asyncio.CancelledError:
        exchange.event["stream_outcome"] = "cancelled"
        raise
    except Exception:
        exchange.event["stream_outcome"] = "interrupted"
        # No synthetic success terminator or raw provider error is emitted.
        return


class OwnedStream(StreamingResponse):
    """Ensure cleanup even when the ASGI response is cancelled before iteration starts."""

    def __init__(
        self, gateway: Gateway, exchange: Exchange, alias: str, headers: dict[str, str]
    ) -> None:
        super().__init__(
            sse_body(gateway, exchange, alias), media_type="text/event-stream", headers=headers
        )
        self.gateway = gateway
        self.exchange = exchange

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.gateway.finish(
                    self.exchange, self.exchange.event.pop("stream_outcome", "cancelled")
                )


def create_app(
    gateway: Gateway | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Build the HTTP adapter; a lifespan may install the gateway on app.state."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if gateway is not None:
        app.state.gateway = gateway

    def current(request: Request) -> Gateway:
        current_gateway: Gateway = request.app.state.gateway
        return current_gateway

    def authorize(request: Request, gateway: Gateway) -> None:
        expected = f"Bearer {gateway.settings.api_key}".encode()
        actual = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(actual, expected):
            raise GatewayError("unauthorized", 401)

    @app.get("/health/liveliness")
    @app.get("/health/readiness")
    async def health() -> Payload:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        request_id = uuid.uuid4().hex
        gateway = current(request)
        try:
            authorize(request, gateway)
            names = [*gateway.settings.pairs, *gateway.settings.models]
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "created": 0, "owned_by": "gateway"}
                        for name in names
                    ],
                },
                headers={"x-request-id": request_id},
            )
        except GatewayError as error:
            return _error(error, request_id)

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> Any:
        request_id = uuid.uuid4().hex
        exchange: Exchange | None = None
        handed_off = False
        opened = False
        outcome = "failed"
        gateway = current(request)
        try:
            authorize(request, gateway)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > gateway.settings.max_request_bytes:
                    raise GatewayError("request_too_large", 413)
            try:
                payload = _validate(json.loads(data, parse_constant=_reject_constant))
            except ValueError, UnicodeDecodeError:
                raise GatewayError("invalid_json", 400) from None
            opened = True
            exchange = await gateway.open(payload, dict(request.headers), request_id)
            response_headers = {
                "x-request-id": request_id,
                "x-gateway-model": exchange.event["model"],
                "x-gateway-endpoint": exchange.event["endpoint"],
            }
            if exchange.response.status >= 400:
                # Upstream bodies can contain private endpoint URLs or credential details.
                raise GatewayError("upstream_rejected_request", exchange.response.status)
            if payload.get("stream"):
                if "text/event-stream" not in exchange.response.headers.get("content-type", ""):
                    raise GatewayError("invalid_upstream_stream")
                handed_off = True
                return OwnedStream(gateway, exchange, payload["model"], response_headers)
            body = bytearray()
            async for chunk in gateway.body(exchange):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE:
                    raise GatewayError("upstream_response_too_large")
            try:
                value = json.loads(body)
                if not isinstance(value, dict) or "error" in value or "choices" not in value:
                    raise ValueError
            except ValueError, UnicodeDecodeError:
                raise GatewayError("invalid_upstream_response") from None
            value["model"] = payload["model"]
            usage = _usage(value.get("usage"))
            if usage is not None:
                exchange.event["usage"] = usage
            outcome = "completed"
            return JSONResponse(value, headers=response_headers)
        except GatewayError as error:
            if exchange is not None:
                exchange.event.update(status=error.status, error=error.code)
            elif not opened:
                gateway.events.emit(
                    {
                        "event": "request",
                        "request_id": request_id,
                        "status": error.status,
                        "outcome": "rejected",
                        "error": error.code,
                    }
                )
            return _error(error, request_id)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            if exchange is not None:
                exchange.event.update(status=500, error="request_failed")
            elif not opened:
                gateway.events.emit(
                    {
                        "event": "request",
                        "request_id": request_id,
                        "status": 500,
                        "outcome": "failed",
                        "error": "request_failed",
                    }
                )
            return _error(GatewayError("request_failed", 500), request_id)
        finally:
            if exchange is not None and not handed_off:
                with anyio.CancelScope(shield=True):
                    await gateway.finish(exchange, outcome)

    return app
