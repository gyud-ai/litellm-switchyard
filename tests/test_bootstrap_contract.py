"""Real-adapter lifespan contracts: ownership, teardown isolation, terminal events."""

import io
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import LifespanDriver

from switchyard_gateway import bootstrap
from switchyard_gateway.adapters.headroom import HeadroomCompressor
from switchyard_gateway.adapters.logging import JsonEvents

pytestmark = pytest.mark.integration


class FaultyOutput(io.StringIO):
    """A stdout stand-in that raises while writing one named record."""

    def __init__(self, fail_on: str = "") -> None:
        super().__init__()
        self.fail_on = fail_on

    def write(self, text: str) -> int:
        if self.fail_on and self.fail_on in text:
            raise OSError("private stdout failure")
        return super().write(text)


class Adapters:
    """Record the real adapters the composition root constructs and releases."""

    def __init__(self) -> None:
        self.clients: list[RecordingClient] = []
        self.compressors: list[RecordingCompressor] = []
        self.closed: list[str] = []


class RecordingClient(httpx.AsyncClient):
    def __init__(self, recorder: Adapters, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.recorder = recorder
        self.fail_close = False
        recorder.clients.append(self)

    async def aclose(self) -> None:
        self.recorder.closed.append("client")
        await super().aclose()
        if self.fail_close:
            raise httpx.HTTPError("private client close failure")


class RecordingCompressor(HeadroomCompressor):
    def __init__(self, recorder: Adapters, workers: int) -> None:
        super().__init__(workers)
        self.recorder = recorder
        self.close_calls = 0
        self.fail_close = False
        recorder.compressors.append(self)

    async def close(self) -> None:
        self.close_calls += 1
        self.recorder.closed.append("compressor")
        await super().close()
        if self.fail_close:
            raise RuntimeError("private compressor close failure")


@pytest.fixture
def real_adapters(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Adapters, FaultyOutput]]:
    recorder = Adapters()
    output = FaultyOutput()
    monkeypatch.setattr(
        bootstrap,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: RecordingClient(recorder, **kwargs),
            Timeout=httpx.Timeout,
        ),
    )
    monkeypatch.setattr(
        bootstrap, "HeadroomCompressor", lambda workers: RecordingCompressor(recorder, workers)
    )
    monkeypatch.setattr(bootstrap, "JsonEvents", lambda: JsonEvents(output=output))
    yield recorder, output


async def test_lifespan_serves_models_then_releases_every_adapter(settings, real_adapters):
    recorder, output = real_adapters
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    assert driver.error is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as api:
        response = await api.get(
            "/v1/models", headers={"authorization": f"Bearer {settings.api_key}"}
        )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == ["switchyard", "cheap", "expensive"]
    await driver.stop()
    assert driver.error is None
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(records) == 3
    assert records[0]["event"] == "startup"
    assert records[0]["models"] == 2
    assert records[0]["pairs"] == 1
    assert records[1]["event"] == "request"
    assert records[1]["status"] == 200
    assert records[1]["outcome"] == "completed"
    assert records[2]["event"] == "shutdown"
    assert "error" not in records[2]
    assert recorder.closed == ["compressor", "client"]
    assert recorder.clients[0].is_closed
    assert recorder.compressors[0].close_calls == 1
    assert recorder.compressors[0]._executor._shutdown is True
    assert settings.api_key not in output.getvalue()


async def test_startup_emit_failure_releases_adapters_and_reports_shutdown(settings, real_adapters):
    recorder, output = real_adapters
    output.fail_on = "startup"
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    await driver.stop()
    assert isinstance(driver.error, OSError)
    assert driver.messages[-1]["type"] == "lifespan.startup.failed"
    assert recorder.closed == ["compressor", "client"]
    assert recorder.clients[0].is_closed
    assert recorder.compressors[0]._executor._shutdown is True
    shutdown = json.loads(output.getvalue().splitlines()[-1])
    assert shutdown["event"] == "shutdown"
    assert "private" not in output.getvalue()


async def test_client_close_failure_does_not_skip_the_compressor(settings, real_adapters):
    recorder, output = real_adapters
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    recorder.clients[0].fail_close = True
    await driver.stop()
    assert driver.error is None
    assert recorder.closed == ["compressor", "client"]
    assert recorder.compressors[0].close_calls == 1
    assert recorder.compressors[0]._executor._shutdown is True
    shutdown = json.loads(output.getvalue().splitlines()[-1])
    assert shutdown["event"] == "shutdown"
    assert shutdown["error"] == "shutdown_failed"
    assert "private" not in output.getvalue()


async def test_compressor_close_failure_does_not_skip_the_client(settings, real_adapters):
    recorder, output = real_adapters
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    recorder.compressors[0].fail_close = True
    await driver.stop()
    assert driver.error is None
    assert recorder.closed == ["compressor", "client"]
    assert recorder.clients[0].is_closed
    shutdown = json.loads(output.getvalue().splitlines()[-1])
    assert shutdown["event"] == "shutdown"
    assert shutdown["error"] == "shutdown_failed"


async def test_shutdown_emit_failure_happens_after_release(settings, real_adapters):
    recorder, output = real_adapters
    output.fail_on = "shutdown"
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    await driver.stop()
    assert isinstance(driver.error, OSError)
    assert recorder.closed == ["compressor", "client"]
    assert recorder.clients[0].is_closed
    assert recorder.compressors[0]._executor._shutdown is True
    assert "startup" in output.getvalue()
