"""Composition-root contracts: adapter ownership, teardown isolation, and CLI events."""

import asyncio
import importlib.util
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from conftest import LifespanDriver, run_lifespan
from hypothesis import given
from hypothesis import strategies as st

from switchyard_gateway import bootstrap
from switchyard_gateway.domain import Endpoint, GatewayError, Model, Settings

pytestmark = pytest.mark.unit


class Sink:
    """Record lifecycle events, optionally failing a named record write."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.fail: set[str] = set()

    def emit(self, event: dict) -> None:
        if event["event"] in self.fail:
            raise OSError("private stdout failure")
        self.records.append(dict(event))


class FakeClient:
    def __init__(self, recorder: Recorder, kwargs: dict) -> None:
        self.recorder = recorder
        self.kwargs = kwargs
        self.close_calls = 0
        self.fail_close = recorder.fail_client

    async def aclose(self) -> None:
        self.close_calls += 1
        self.recorder.order.append("client")
        if self.fail_close:
            raise RuntimeError("private client close failure")


class FakeCompressor:
    def __init__(self, recorder: Recorder, workers: int) -> None:
        self.recorder = recorder
        self.workers = workers
        self.close_calls = 0
        self.fail_close = recorder.fail_compressor

    async def close(self) -> None:
        self.close_calls += 1
        self.recorder.order.append("compressor")
        if self.fail_close:
            raise RuntimeError("private compressor close failure")


class FakeRouter:
    def __init__(self, policy: Any) -> None:
        self.policy = policy


class FakeTransport:
    def __init__(self, client: Any) -> None:
        self.client = client


class Recorder:
    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.compressors: list[FakeCompressor] = []
        self.sink = Sink()
        self.order: list[str] = []
        self.fail_client = False
        self.fail_compressor = False

    def client_factory(self, **kwargs: Any) -> FakeClient:
        client = FakeClient(self, kwargs)
        self.clients.append(client)
        return client

    def compressor_factory(self, workers: int) -> FakeCompressor:
        compressor = FakeCompressor(self, workers)
        self.compressors.append(compressor)
        return compressor


@contextmanager
def wiring() -> Iterator[Recorder]:
    recorder = Recorder()
    replacements = {
        "httpx": SimpleNamespace(AsyncClient=recorder.client_factory, Timeout=httpx.Timeout),
        "HeadroomCompressor": recorder.compressor_factory,
        "SwitchyardRouter": FakeRouter,
        "HttpxTransport": FakeTransport,
        "JsonEvents": lambda: recorder.sink,
    }
    with ExitStack() as stack:
        for name, value in replacements.items():
            stack.enter_context(patch.object(bootstrap, name, value))
        yield recorder


@pytest.fixture
def wired() -> Iterator[Recorder]:
    with wiring() as recorder:
        yield recorder


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    yield
    logging.disable(logging.NOTSET)


async def test_build_app_defers_adapter_construction_until_lifespan(wired, settings):
    app = bootstrap.build_app(settings)
    assert wired.clients == []
    assert wired.compressors == []
    driver = await run_lifespan(app)
    assert driver.error is None
    assert len(wired.clients) == 1
    assert len(wired.compressors) == 1
    assert wired.clients[0].close_calls == 1
    assert wired.compressors[0].close_calls == 1


async def test_lifespan_installs_the_gateway_with_constructed_adapters(wired, settings):
    app = bootstrap.build_app(settings)
    async with LifespanDriver(app):
        gateway = app.state.gateway
        assert gateway.settings is settings
        assert gateway.router.policy is settings.stage
        assert gateway.transport.client is wired.clients[0]
        assert gateway.compressor is wired.compressors[0]


async def test_teardown_drains_compression_before_releasing_the_client(wired, settings):
    app = bootstrap.build_app(settings)
    await run_lifespan(app)
    assert wired.order == ["compressor", "client"]


async def test_client_construction_keeps_explicit_isolation(wired, settings):
    app = bootstrap.build_app(settings)
    async with LifespanDriver(app):
        kwargs = wired.clients[0].kwargs
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"].read == settings.read_timeout
        assert kwargs["timeout"].connect == settings.connect_timeout
        assert wired.compressors[0].workers == settings.compression_workers


async def test_startup_event_counts_configured_labels(wired, settings):
    app = bootstrap.build_app(settings)
    driver = await run_lifespan(app)
    assert driver.messages[-1]["type"] == "lifespan.shutdown.complete"
    assert wired.sink.records[0] == {"event": "startup", "models": 2, "pairs": 1}


async def test_clean_shutdown_emits_one_terminal_event(wired, settings):
    app = bootstrap.build_app(settings)
    await run_lifespan(app)
    assert [record["event"] for record in wired.sink.records] == ["startup", "shutdown"]


async def test_startup_emit_failure_still_releases_adapters(wired, settings):
    wired.sink.fail.add("startup")
    app = bootstrap.build_app(settings)
    driver = await run_lifespan(app)
    assert isinstance(driver.error, OSError)
    assert wired.order == ["compressor", "client"]
    assert wired.clients[0].close_calls == 1
    assert wired.compressors[0].close_calls == 1
    assert [record["event"] for record in wired.sink.records] == ["shutdown"]


async def test_client_close_failure_does_not_skip_the_compressor(wired, settings):
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    wired.clients[0].fail_close = True
    await driver.stop()
    assert driver.error is None
    assert wired.order == ["compressor", "client"]
    assert wired.compressors[0].close_calls == 1
    assert wired.sink.records[-1] == {"event": "shutdown", "error": "shutdown_failed"}


async def test_compressor_close_failure_does_not_skip_the_client(wired, settings):
    app = bootstrap.build_app(settings)
    driver = LifespanDriver(app)
    await driver.start()
    wired.compressors[0].fail_close = True
    await driver.stop()
    assert driver.error is None
    assert wired.order == ["compressor", "client"]
    assert wired.clients[0].close_calls == 1
    assert wired.sink.records[-1] == {"event": "shutdown", "error": "shutdown_failed"}


async def test_server_failure_still_releases_and_reports_shutdown(wired, settings):
    app = bootstrap.build_app(settings)
    with pytest.raises(GatewayError, match="server_failed"):
        async with app.router.lifespan_context(app):
            raise GatewayError("server_failed", 500)
    assert wired.order == ["compressor", "client"]
    assert wired.sink.records == [
        {"event": "startup", "models": 2, "pairs": 1},
        {"event": "shutdown"},
    ]


def test_main_reports_bad_arguments_as_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switchyard-gateway", "--unknown"])
    with pytest.raises(SystemExit) as caught:
        bootstrap.main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    record = json.loads(captured.out)
    assert record["event"] == "startup_failed"
    assert record["error"] == "invalid_arguments"


def test_main_help_exits_cleanly_without_a_failure_event(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switchyard-gateway", "--help"])
    with pytest.raises(SystemExit) as caught:
        bootstrap.main()
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert "startup_failed" not in captured.out
    lines = [line.strip() for line in captured.out.splitlines()]
    assert "Switchyard + Headroom gateway" in lines
    assert any(line.endswith("validate configuration and exit") for line in lines)


def test_main_reports_configuration_failure_as_json(monkeypatch, capsys):
    seen: list[Path] = []

    def broken(path: Path) -> Settings:
        seen.append(path)
        raise GatewayError("invalid_configuration", 500)

    monkeypatch.setattr(sys, "argv", ["switchyard-gateway", "--config", "missing.jsonc"])
    monkeypatch.setattr(bootstrap, "load_config", broken)
    with pytest.raises(SystemExit) as caught:
        bootstrap.main()
    assert caught.value.code == 1
    assert seen == [Path("missing.jsonc")]
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    record = json.loads(captured.out)
    assert record["event"] == "startup_failed"
    assert record["error"] == "invalid_configuration"


def test_main_refuses_a_litellm_contaminated_interpreter(monkeypatch, capsys):
    inspected: list[str] = []

    def find_spec(name: str) -> object:
        inspected.append(name)
        return object()

    monkeypatch.setattr(sys, "argv", ["switchyard-gateway"])
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    with pytest.raises(SystemExit) as caught:
        bootstrap.main()
    assert caught.value.code == 1
    assert inspected == ["litellm"]
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "startup_failed"
    assert record["error"] == "litellm_must_not_be_installed"


def test_main_check_validates_without_starting_the_server(monkeypatch, capsys, settings):
    started: list[Any] = []
    monkeypatch.setattr(sys, "argv", ["switchyard-gateway", "--check"])
    monkeypatch.setattr(bootstrap, "load_config", lambda path: settings)
    monkeypatch.setattr(bootstrap.uvicorn, "run", lambda *args, **kwargs: started.append(args))
    bootstrap.main()
    assert started == []
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "configuration_valid"


async def test_main_defaults_to_config_jsonc_and_one_isolated_worker(monkeypatch, wired, settings):
    paths: list[Path] = []
    calls: list[tuple[Any, dict]] = []

    def fake_load(path: Path) -> Settings:
        paths.append(path)
        return settings

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    for name in ("HEADROOM_BEACON", "DO_NOT_TRACK", "HEADROOM_TELEMETRY"):
        monkeypatch.setenv(name, "original")
    monkeypatch.setattr(sys, "argv", ["switchyard-gateway"])
    monkeypatch.setattr(bootstrap, "load_config", fake_load)
    monkeypatch.setattr(bootstrap.uvicorn, "run", fake_run)
    bootstrap.main()
    assert paths == [Path("config.jsonc")]
    assert wired.clients == []
    assert wired.compressors == []
    app, kwargs = calls[0]
    assert isinstance(app, bootstrap.FastAPI)
    assert kwargs == {
        "host": settings.host,
        "port": settings.port,
        "workers": 1,
        "access_log": False,
        "log_config": None,
    }
    assert os.environ["HEADROOM_BEACON"] == "off"
    assert os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["HEADROOM_TELEMETRY"] == "off"
    driver = await run_lifespan(app)
    assert driver.error is None
    assert app.state.gateway.settings is settings


def test_main_start_failure_constructs_no_adapters(monkeypatch, wired, settings):
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(sys, "argv", ["switchyard-gateway"])
    monkeypatch.setattr(bootstrap, "load_config", lambda path: settings)
    monkeypatch.setattr(bootstrap.uvicorn, "run", fail)
    with pytest.raises(OSError, match="bind failed"):
        bootstrap.main()
    assert wired.clients == []
    assert wired.compressors == []


def _settings() -> Settings:
    model = Model("cheap", "cheap-backend", (Endpoint("a", "http://replica-a/v1", "key"),))
    return Settings({"cheap": model}, {}, "client-key")


@given(
    fail_startup=st.booleans(),
    fail_client=st.booleans(),
    fail_compressor=st.booleans(),
)
def test_shutdown_is_reported_exactly_once_under_fault_injection(
    fail_startup, fail_client, fail_compressor
):
    with wiring() as recorder:
        if fail_startup:
            recorder.sink.fail.add("startup")
        recorder.fail_client = fail_client
        recorder.fail_compressor = fail_compressor
        app = bootstrap.build_app(_settings())

        async def exercise() -> None:
            driver = LifespanDriver(app)
            await driver.start()
            await driver.stop()

        asyncio.run(exercise())
        shutdowns = [record for record in recorder.sink.records if record["event"] == "shutdown"]
        startups = [record for record in recorder.sink.records if record["event"] == "startup"]
        assert len(shutdowns) == 1
        assert len(startups) == (0 if fail_startup else 1)
        assert (shutdowns[0].get("error") == "shutdown_failed") is (fail_client or fail_compressor)
        assert recorder.order == ["compressor", "client"]
        assert len(recorder.clients) == 1
        assert recorder.clients[0].close_calls == 1
        assert len(recorder.compressors) == 1
        assert recorder.compressors[0].close_calls == 1
