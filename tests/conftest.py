"""Hermetic application fixtures; live configuration is never loaded implicitly."""

import copy
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from switchyard_gateway.application import Gateway
from switchyard_gateway.domain import (
    CompressionResult,
    Endpoint,
    Model,
    Pair,
    Payload,
    RoutingResult,
    Settings,
)


class Events:
    def __init__(self) -> None:
        self.records: list[Payload] = []

    def emit(self, event: Payload) -> None:
        self.records.append(copy.deepcopy(event))


class Router:
    def __init__(self) -> None:
        self.calls: list[Payload] = []

    async def route(self, request: Payload, pair: Pair) -> RoutingResult:
        self.calls.append(copy.deepcopy(request))
        return RoutingResult("efficient", copy.deepcopy(request))


class Compressor:
    def __init__(self) -> None:
        self.calls: list[list[Payload]] = []
        self.fail = False

    async def compress(self, messages: list[Payload], model: Model) -> CompressionResult:
        self.calls.append(copy.deepcopy(messages))
        if self.fail:
            raise RuntimeError("private compression failure")
        result = copy.deepcopy(messages)
        for row in result:
            row["content"] = "compressed"
        return CompressionResult(result, "savings", 100, 10, 90)


class Response:
    def __init__(
        self,
        status: int = 200,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"content-type": "application/json"}
        self.data = (
            chunks
            if chunks is not None
            else [
                json.dumps(
                    {
                        "model": "backend-model",
                        "choices": [{"message": {"content": "hello"}}],
                    }
                ).encode()
            ]
        )
        self.closed = False
        self.error: Exception | None = None

    async def chunks(self) -> AsyncIterator[bytes]:
        for chunk in self.data:
            yield chunk
        if self.error:
            raise self.error

    async def close(self) -> None:
        self.closed = True


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[Endpoint, Payload, dict[str, str]]] = []
        self.responses: list[Response | Exception] = []

    async def send(self, endpoint: Endpoint, request: Payload, headers: dict[str, str]) -> Response:
        self.calls.append((endpoint, copy.deepcopy(request), headers))
        result = self.responses.pop(0) if self.responses else Response()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def settings() -> Settings:
    cheap = Model(
        "cheap",
        "cheap-backend",
        (
            Endpoint("a", "http://replica-a/v1", "backend-key"),
            Endpoint("b", "http://replica-b/v1", "other-key"),
        ),
    )
    expensive = Model("expensive", "expensive-backend", (Endpoint("c", "http://c/v1"),))
    return Settings(
        {"cheap": cheap, "expensive": expensive},
        {"switchyard": Pair("switchyard", "expensive", "cheap")},
        "client-key",
    )


@pytest.fixture
def request_body() -> Payload:
    return {"model": "switchyard", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def history(request_body: Payload) -> Payload:
    result = copy.deepcopy(request_body)
    result["messages"] = [
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
        {"role": "tool", "tool_call_id": "live-call", "content": "live result"},
    ]
    return result


@pytest.fixture
def gateway(settings: Settings) -> Gateway:
    return Gateway(settings, Router(), Compressor(), Transport(), Events())


@pytest.fixture
def without_compression(settings: Settings) -> Settings:
    return replace(settings, compression=False)
