"""Observability contract: emitted event fields, outcomes, and retry records."""

import asyncio
import copy
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from conftest import Compressor, Events, Response, Router, Transport
from hypothesis import given
from hypothesis import strategies as st

from switchyard_gateway.adapters.logging import DependencyLogSilencer, silence_dependency_logs
from switchyard_gateway.application import Gateway
from switchyard_gateway.domain import ConnectFailure, GatewayError

pytestmark = pytest.mark.unit


def _numeric(event: dict, *keys: str) -> None:
    for key in keys:
        assert key in event, key
        assert isinstance(event[key], (int, float)) and not isinstance(event[key], bool), key


async def test_successful_request_event_contract(gateway, request_body):
    exchange = await gateway.open(request_body, {"x-session-id": "s"}, "req-1")
    assert gateway.transport.calls[0][1]["model"] == "cheap-backend"
    async for _ in gateway.body(exchange):
        pass
    await gateway.finish(exchange, "completed")
    event = gateway.events.records[-1]
    assert event["event"] == "request"
    assert event["request_id"] == "req-1"
    assert event["route"] == "switchyard"
    assert event["tier"] == "efficient"
    assert event["model"] == "cheap"
    assert event["endpoint"] == "a"
    assert event["attempts"] == 1
    assert event["status"] == 200
    assert event["outcome"] == "completed"
    _numeric(
        event,
        "total_ms",
        "routing_ms",
        "compression_ms",
        "upstream_headers_ms",
        "first_body_byte_ms",
    )


async def test_direct_route_event_contract(gateway, request_body):
    request_body["model"] = "cheap"
    exchange = await gateway.open(request_body, {}, "req-2")
    await gateway.finish(exchange, "completed")
    event = gateway.events.records[-1]
    assert event["route"] == "cheap"
    assert event["tier"] == "direct"
    assert event["model"] == "cheap"


async def test_compression_event_fields(gateway, history):
    exchange = await gateway.open(history, {}, "req-3")
    event = exchange.event
    assert event["compression"] == "savings"
    assert event["tokens_before"] == 100
    assert event["tokens_after"] == 10
    assert event["tokens_saved"] == 90
    await gateway.finish(exchange, "completed")


async def test_no_eligible_history_event(gateway, request_body):
    exchange = await gateway.open(request_body, {}, "req-4")
    assert exchange.event["compression"] == "no_eligible_history"
    assert gateway.compressor.calls == []
    await gateway.finish(exchange, "completed")


async def test_bypass_event(gateway, history):
    exchange = await gateway.open(history, {"x-headroom-bypass": "true"}, "req-5")
    assert exchange.event["compression"] == "bypass"
    assert gateway.compressor.calls == []
    await gateway.finish(exchange, "completed")


async def test_failed_open_records_status_and_code(gateway, request_body):
    class Boom:
        async def send(self, endpoint, request, headers):
            raise RuntimeError("private transport diagnostic")

    gateway.transport = Boom()
    with pytest.raises(RuntimeError):
        await gateway.open(request_body, {}, "req-6")
    event = gateway.events.records[-1]
    assert event["event"] == "request"
    assert event["outcome"] == "failed"
    assert event["status"] == 500
    assert event["error"] == "request_failed"
    _numeric(event, "total_ms")
    assert "private" not in str(event)


async def test_gateway_error_open_records_its_status(gateway, request_body):
    request_body["model"] = "missing"
    with pytest.raises(GatewayError) as caught:
        await gateway.open(request_body, {}, "req-7")
    assert caught.value.code == "unknown_model"
    assert caught.value.status == 404
    event = gateway.events.records[-1]
    assert event["outcome"] == "failed"
    assert event["status"] == 404
    assert event["error"] == "unknown_model"
    assert event["attempts"] == 0
    _numeric(event, "total_ms")


async def test_cancelled_open_records_cancelled_outcome(gateway, request_body):
    class Blocking:
        async def send(self, endpoint, request, headers):
            await asyncio.sleep(60)

    gateway.transport = Blocking()
    task = asyncio.create_task(gateway.open(request_body, {}, "req-8"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    event = gateway.events.records[-1]
    assert event["outcome"] == "cancelled"
    _numeric(event, "total_ms")


async def test_retryable_status_records_retry_and_replica_failure(gateway, request_body):
    gateway.transport.responses = [Response(503), Response()]
    exchange = await gateway.open(request_body, {}, "req-9")
    retry = next(record for record in gateway.events.records if record.get("event") == "retry")
    failure = next(
        record for record in gateway.events.records if record.get("event") == "replica_failure"
    )
    assert retry == {
        "event": "retry",
        "request_id": "req-9",
        "model": "cheap",
        "endpoint": "b",
    }
    assert failure["event"] == "replica_failure"
    assert failure["request_id"] == "req-9"
    assert failure["model"] == "cheap"
    assert failure["endpoint"] == "a"
    assert failure["status"] == 503
    await gateway.finish(exchange, "completed")


async def test_connect_failure_records_error_code(gateway, request_body):
    gateway.transport.responses = [ConnectFailure(), Response()]
    exchange = await gateway.open(request_body, {}, "req-10")
    failure = next(
        record for record in gateway.events.records if record.get("event") == "replica_failure"
    )
    assert failure["request_id"] == "req-10"
    assert failure["model"] == "cheap"
    assert failure["endpoint"] == "a"
    assert failure["error"] == "upstream_connect_failed"
    await gateway.finish(exchange, "completed")


async def test_requests_preserve_nested_extension_values(gateway, history):
    history["messages"][1]["nested"] = {"inner": {"values": [1, 2, 3]}}
    original = copy.deepcopy(history)
    exchange = await gateway.open(history, {}, "req-11")
    sent = gateway.transport.calls[0][1]
    assert sent["messages"][1]["nested"] == {"inner": {"values": [1, 2, 3]}}
    assert history == original
    await gateway.finish(exchange, "completed")


async def test_empty_chunks_do_not_mark_first_body_byte(gateway, request_body):
    gateway.transport.responses = [Response(chunks=[b""])]
    exchange = await gateway.open(request_body, {}, "req-12")
    async for _ in gateway.body(exchange):
        pass
    assert "first_body_byte_ms" not in exchange.event
    await gateway.finish(exchange, "completed")


class TestTimings:
    class Ticks:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 0.001
            return self.value

    class Stepped:
        """Binary-exact increasing timestamps; exact values pin every scale and sign."""

        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 10.0
            return self.value

    async def test_event_timings_are_millisecond_scaled(self, settings, request_body):
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), self.Ticks())
        exchange = await gateway.open(request_body, {}, "req-13")
        async for _ in gateway.body(exchange):
            pass
        await gateway.finish(exchange, "completed")
        event = gateway.events.records[-1]
        for key in (
            "routing_ms",
            "compression_ms",
            "upstream_headers_ms",
            "first_body_byte_ms",
            "total_ms",
        ):
            value = event[key]
            assert value > 0, key
            assert abs(value - round(value)) < 1e-9, (key, value)

    async def test_success_event_records_exact_millisecond_values(self, settings, request_body):
        request_body["model"] = "cheap"
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), self.Stepped())
        gateway.transport.responses = [Response(chunks=[b"a", b"b"])]
        exchange = await gateway.open(request_body, {}, "req-14")
        async for _ in gateway.body(exchange):
            pass
        await gateway.finish(exchange, "completed")
        event = gateway.events.records[-1]
        assert event["routing_ms"] == 10000.0
        assert event["compression_ms"] == 10000.0
        assert event["upstream_headers_ms"] == 20000.0
        assert event["first_body_byte_ms"] == 30000.0
        assert event["total_ms"] == 90000.0

    async def test_failure_event_records_exact_millisecond_values(self, settings, request_body):
        request_body["model"] = "missing"
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), self.Stepped())
        with pytest.raises(GatewayError):
            await gateway.open(request_body, {}, "req-15")
        assert gateway.events.records[-1]["total_ms"] == 20000.0


@contextmanager
def _restored_disable_level() -> Iterator[None]:
    """Restore the global disable level even when an assertion fails."""
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


def test_silencing_scope_restores_prior_level_and_reenables_logging() -> None:
    with _restored_disable_level():
        logging.disable(logging.WARNING)
        records: list[str] = []

        class Recording(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        logger = logging.getLogger("switchyard.test.silencer")
        handler = Recording()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with silence_dependency_logs():
                assert logging.root.manager.disable == logging.CRITICAL
                logger.error("hidden")
            assert logging.root.manager.disable == logging.WARNING
            logger.error("visible")
        finally:
            logger.removeHandler(handler)
        assert records == ["visible"]


def test_bare_call_silences_until_the_returned_handle_is_released() -> None:
    with _restored_disable_level():
        logging.disable(logging.INFO)
        silencer = silence_dependency_logs()
        assert logging.root.manager.disable == logging.CRITICAL
        with silencer:
            assert logging.root.manager.disable == logging.CRITICAL
        assert logging.root.manager.disable == logging.INFO


def test_nested_silencing_scopes_restore_each_level() -> None:
    with _restored_disable_level():
        logging.disable(logging.ERROR)
        with silence_dependency_logs():
            assert logging.root.manager.disable == logging.CRITICAL
            with silence_dependency_logs():
                assert logging.root.manager.disable == logging.CRITICAL
            assert logging.root.manager.disable == logging.CRITICAL
        assert logging.root.manager.disable == logging.ERROR


def test_reentering_one_handle_silences_until_the_outermost_exit() -> None:
    with _restored_disable_level():
        logging.disable(logging.WARNING)
        silencer = silence_dependency_logs()
        with silencer:
            with silencer:
                assert logging.root.manager.disable == logging.CRITICAL
            assert logging.root.manager.disable == logging.CRITICAL
        assert logging.root.manager.disable == logging.WARNING


def test_release_without_entry_leaves_logging_unchanged() -> None:
    with _restored_disable_level():
        logging.disable(logging.ERROR)
        DependencyLogSilencer().__exit__()
        assert logging.root.manager.disable == logging.ERROR


def test_silencing_scope_restores_when_the_body_raises() -> None:
    with _restored_disable_level():
        logging.disable(logging.ERROR)
        with pytest.raises(RuntimeError):
            with silence_dependency_logs():
                raise RuntimeError("private dependency failure")
        assert logging.root.manager.disable == logging.ERROR


@given(prior=st.integers())
def test_property_scope_restores_exactly_the_prior_disable_level(prior: int) -> None:
    with _restored_disable_level():
        logging.disable(prior)
        with silence_dependency_logs():
            assert logging.root.manager.disable == logging.CRITICAL
        assert logging.root.manager.disable == prior


@given(prior=st.integers())
def test_property_nested_scopes_restore_exactly_the_prior_disable_level(prior: int) -> None:
    with _restored_disable_level():
        logging.disable(prior)
        with silence_dependency_logs():
            with silence_dependency_logs():
                assert logging.root.manager.disable == logging.CRITICAL
            assert logging.root.manager.disable == logging.CRITICAL
        assert logging.root.manager.disable == prior
