"""Application contracts exercised through replaceable port implementations."""

import asyncio
import copy
from dataclasses import replace

import pytest
from conftest import Compressor, Events, Response, Router, Transport

from switchyard_gateway.application import Gateway, eligible_indices
from switchyard_gateway.domain import (
    CompressionResult,
    ConnectFailure,
    Endpoint,
    GatewayError,
    Model,
    Pair,
    RoutingResult,
    Settings,
)

pytestmark = pytest.mark.unit


class TestRoutingAndCompression:
    async def test_routes_pristine_then_compresses_private_history(self, gateway, history):
        original = copy.deepcopy(history)
        exchange = await gateway.open(history, {}, "test")
        sent = gateway.transport.calls[0][1]
        assert gateway.router.calls == [original]
        assert history == original
        assert sent["messages"][0] == original["messages"][0]
        assert sent["messages"][4:] == original["messages"][4:]
        assert sent["messages"][3]["content"] == "compressed"
        assert sent["messages"][2]["tool_calls"] == original["messages"][2]["tool_calls"]
        await gateway.finish(exchange, "completed")
        assert gateway.events.records[-1]["tokens_saved"] == 90

    async def test_bypass_and_direct_model_route(self, gateway, history):
        history["model"] = "cheap"
        exchange = await gateway.open(history, {"x-headroom-bypass": "true"}, "test")
        assert gateway.router.calls == []
        assert gateway.compressor.calls == []
        assert exchange.event["tier"] == "direct"
        await gateway.finish(exchange, "completed")

    async def test_compression_error_preserves_history(self, gateway, history):
        gateway.compressor.fail = True
        exchange = await gateway.open(history, {}, "test")
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        assert exchange.event["compression"] == "failed_unknown"
        await gateway.finish(exchange, "completed")

    async def test_unknown_model_has_sanitized_error(self, gateway, request_body):
        request_body["model"] = "secret-client-value"
        with pytest.raises(GatewayError, match="unknown_model"):
            await gateway.open(request_body, {}, "test")
        assert "secret-client-value" not in str(gateway.events.records)

    async def test_independent_pairs_reuse_model(self, gateway, request_body):
        gateway.settings.pairs["second"] = Pair("second", "cheap", "expensive")
        request_body["model"] = "second"
        exchange = await gateway.open(request_body, {}, "test")
        assert gateway.transport.calls[0][0].name == "c"
        await gateway.finish(exchange, "completed")

    def test_protects_cache_rows_and_cross_boundary_tool_exchanges(self, history):
        history["messages"][1]["cache_control"] = {"type": "ephemeral"}
        history["messages"][-1]["tool_call_id"] = "old-call"
        assert eligible_indices(history["messages"]) == []

    async def test_rejects_structure_mutation_from_adapter(self, gateway, history):
        class BrokenCompressor:
            async def compress(self, messages, model):
                from switchyard_gateway.domain import CompressionResult

                return CompressionResult([], "savings", 100, 1, 99)

        gateway.compressor = BrokenCompressor()
        exchange = await gateway.open(history, {}, "test")
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        assert exchange.event["compression"] == "failed_unknown"
        await gateway.finish(exchange, "completed")


class TestReplicaSelection:
    async def test_round_robin_and_forward_header_allowlist(self, gateway, request_body):
        for _ in range(4):
            exchange = await gateway.open(
                request_body,
                {
                    "x-session-id": "session",
                    "authorization": "private",
                    "x-unlisted": "secret",
                },
                "test",
            )
            await gateway.finish(exchange, "completed")
        assert [call[0].name for call in gateway.transport.calls] == ["a", "b", "a", "b"]
        assert gateway.transport.calls[0][2] == {"x-session-id": "session"}

    @pytest.mark.parametrize("status", [429, 502, 503, 504])
    async def test_retryable_status_uses_other_replica(self, gateway, request_body, status):
        failed = Response(status, headers={"retry-after": "60"})
        gateway.transport.responses = [failed, Response()]
        exchange = await gateway.open(request_body, {}, "test")
        assert failed.closed
        assert exchange.event["endpoint"] == "b"
        assert exchange.event["attempts"] == 2
        await gateway.finish(exchange, "completed")

    async def test_connection_failure_cooldown_expires(self, settings, request_body):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [ConnectFailure(), Response()]
        exchange = await gateway.open(request_body, {}, "test")
        await gateway.finish(exchange, "completed")
        exchange = await gateway.open(request_body, {}, "test")
        assert transport.calls[-1][0].name == "b"
        await gateway.finish(exchange, "completed")
        now[0] = 131
        exchange = await gateway.open(request_body, {}, "test")
        assert transport.calls[-1][0].name == "a"
        await gateway.finish(exchange, "completed")

    async def test_no_eligible_replica_returns_503(self, gateway, request_body):
        gateway.transport.responses = [ConnectFailure(), ConnectFailure()]
        with pytest.raises(ConnectFailure):
            await gateway.open(request_body, {}, "one")
        with pytest.raises(GatewayError) as error:
            await gateway.open(request_body, {}, "two")
        assert error.value.status == 503
        assert len(gateway.transport.calls) == 2

    async def test_read_failure_is_not_retried(self, gateway, request_body):
        gateway.transport.responses = [GatewayError("upstream_read_failed")]
        with pytest.raises(GatewayError, match="upstream_read_failed"):
            await gateway.open(request_body, {}, "test")
        assert len(gateway.transport.calls) == 1

    async def test_single_replica_keeps_rejected_response(self, settings, request_body):
        model = settings.models["cheap"]
        settings.models["cheap"] = replace(model, endpoints=model.endpoints[:1])
        transport = Transport()
        transport.responses = [Response(429)]
        gateway = Gateway(settings, Router(), Compressor(), transport, Events())
        exchange = await gateway.open(request_body, {}, "test")
        assert exchange.response.status == 429
        await gateway.finish(exchange, "failed")

    async def test_cancelled_read_closes_once(self, gateway, request_body):
        exchange = await gateway.open(request_body, {}, "test")
        await gateway.finish(exchange, "cancelled")
        await gateway.finish(exchange, "cancelled")
        assert exchange.response.closed
        assert len(gateway.events.records) == 1

    async def test_cancelled_open_is_logged(self, gateway, request_body):
        class BlockingTransport:
            async def send(self, *args):
                await asyncio.sleep(60)

        gateway.transport = BlockingTransport()
        task = asyncio.create_task(gateway.open(request_body, {}, "test"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.events.records[-1]["outcome"] == "cancelled"


class TestReadiness:
    def test_ready_when_any_replica_is_eligible(self, settings):
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events())
        assert gateway.ready()
        gateway._cooldown("cheap", "a")
        assert gateway.ready()
        gateway._cooldown("cheap", "b")
        assert gateway.ready()
        gateway._cooldown("expensive", "c")
        assert not gateway.ready()

    def test_ready_without_configured_models(self):
        gateway = Gateway(Settings({}, {}, "key"), Router(), Compressor(), Transport(), Events())
        assert not gateway.ready()

    def test_ready_at_zero_clock_without_recorded_cooldowns(self, settings):
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), lambda: 0.0)
        assert gateway.ready()

    def test_readiness_recovers_at_exact_cooldown_expiry(self, settings):
        now = [100.0]
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), lambda: now[0])
        gateway._cooldown("cheap", "a")
        gateway._cooldown("cheap", "b")
        gateway._cooldown("expensive", "c")
        assert not gateway.ready()
        now[0] = 130.0
        assert gateway.ready()

    def test_readiness_probe_emits_no_events(self, gateway):
        assert gateway.ready()
        assert gateway.events.records == []


class TestReplicaPolicyContracts:
    async def test_round_robin_advances_in_configured_order(self):
        model = Model(
            "m",
            "m",
            tuple(Endpoint(name, f"http://{name}/v1") for name in ("a", "b", "c")),
        )
        transport = Transport()
        gateway = Gateway(
            Settings({"m": model}, {}, "key"), Router(), Compressor(), transport, Events()
        )
        for _ in range(6):
            exchange = await gateway.open({"model": "m", "messages": []}, {}, "id")
            await gateway.finish(exchange, "completed")
        assert [call[0].name for call in transport.calls] == ["a", "b", "c", "a", "b", "c"]

    async def test_retry_is_capped_at_two_attempts(self, settings, request_body):
        model = settings.models["cheap"]
        settings.models["cheap"] = replace(
            model, endpoints=(*model.endpoints, Endpoint("d", "http://d/v1"))
        )
        transport = Transport()
        transport.responses = [ConnectFailure(), ConnectFailure(), Response()]
        gateway = Gateway(settings, Router(), Compressor(), transport, Events())
        with pytest.raises(ConnectFailure):
            await gateway.open(request_body, {}, "id")
        assert len(transport.calls) == 2

    async def test_fresh_cooldown_table_keeps_replicas_eligible(self, settings, request_body):
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: 0.0)
        exchange = await gateway.open(request_body, {}, "id")
        assert transport.calls[-1][0].name == "a"
        await gateway.finish(exchange, "completed")

    async def test_endpoint_is_eligible_at_exact_cooldown_expiry(self, settings, request_body):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [ConnectFailure(), Response()]
        exchange = await gateway.open(request_body, {}, "id")
        await gateway.finish(exchange, "completed")
        now[0] = 130.0
        exchange = await gateway.open(request_body, {}, "id")
        assert transport.calls[-1][0].name == "a"
        await gateway.finish(exchange, "completed")

    def test_http_date_retry_after_extends_cooldown(self, settings):
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        now = [1000.0]
        gateway = Gateway(settings, Router(), Compressor(), Transport(), Events(), lambda: now[0])
        retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=120))
        gateway._cooldown("cheap", "a", retry_at)
        assert gateway._cooldowns[("cheap", "a")] >= now[0] + 119

    async def test_replica_exhaustion_error_contract(self, gateway, request_body):
        gateway.transport.responses = [ConnectFailure(), ConnectFailure()]
        with pytest.raises(ConnectFailure) as first:
            await gateway.open(request_body, {}, "one")
        assert first.value.code == "upstream_connect_failed"
        with pytest.raises(GatewayError) as error:
            await gateway.open(request_body, {}, "two")
        assert error.value.code == "no_available_replica"
        assert error.value.status == 503

    async def test_capable_tier_selects_capable_model(self, settings, request_body):
        seen = []

        class CapableRouter:
            async def route(self, request, pair):
                seen.append(pair)
                return RoutingResult("capable", copy.deepcopy(request))

        transport = Transport()
        gateway = Gateway(settings, CapableRouter(), Compressor(), transport, Events())
        exchange = await gateway.open(request_body, {}, "id")
        assert seen == [settings.pairs["switchyard"]]
        assert exchange.event["tier"] == "capable"
        assert exchange.event["model"] == "expensive"
        assert transport.calls[0][0].name == "c"
        await gateway.finish(exchange, "completed")

    async def test_retryable_status_cools_failed_endpoint(self, settings, request_body):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [Response(503), Response(), Response()]
        exchange = await gateway.open(request_body, {}, "id")
        await gateway.finish(exchange, "completed")
        exchange = await gateway.open(request_body, {}, "id2")
        assert transport.calls[-1][0].name == "b"
        await gateway.finish(exchange, "completed")

    async def test_retry_after_header_extends_cooldown(self, settings, request_body):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [
            Response(503, headers={"retry-after": "120"}),
            Response(),
            Response(),
        ]
        exchange = await gateway.open(request_body, {}, "id")
        await gateway.finish(exchange, "completed")
        now[0] = 140.0
        exchange = await gateway.open(request_body, {}, "id2")
        assert transport.calls[-1][0].name == "b"
        await gateway.finish(exchange, "completed")


class TestCompressionOutcomeContracts:
    def test_gateway_error_defaults_are_stable(self):
        error = GatewayError("some_code")
        assert error.status == 502
        assert error.code == "some_code"
        assert str(error) == "some_code"
        failure = ConnectFailure()
        assert failure.code == "upstream_connect_failed"
        assert failure.status == 502

    async def test_compressor_receives_selected_model(self, gateway, history):
        seen = []

        class Capture:
            async def compress(self, messages, model):
                seen.append(model)
                return CompressionResult(copy.deepcopy(messages), "savings", 1, 1, 0)

        gateway.compressor = Capture()
        exchange = await gateway.open(history, {}, "id")
        assert [model.name for model in seen] == ["cheap"]
        await gateway.finish(exchange, "completed")

    async def test_no_savings_outcome_is_accepted(self, gateway, history):
        class NoSavings:
            async def compress(self, messages, model):
                return CompressionResult(copy.deepcopy(messages), "no_savings", 10, 10, 0)

        gateway.compressor = NoSavings()
        exchange = await gateway.open(history, {}, "id")
        assert exchange.event["compression"] == "no_savings"
        await gateway.finish(exchange, "completed")

    async def test_unknown_compression_outcome_is_a_failure(self, gateway, history):
        class Weird:
            async def compress(self, messages, model):
                changed = copy.deepcopy(messages)
                for row in changed:
                    row["content"] = "changed"
                return CompressionResult(changed, "unexpected", 1, 1, 0)

        gateway.compressor = Weird()
        exchange = await gateway.open(history, {}, "id")
        assert exchange.event["compression"] == "failed_unknown"
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        await gateway.finish(exchange, "completed")

    async def test_failed_unknown_outcome_discards_reported_messages(self, gateway, history):
        class FailedUnknown:
            async def compress(self, messages, model):
                changed = copy.deepcopy(messages)
                for row in changed:
                    row["content"] = "changed"
                return CompressionResult(changed, "failed_unknown", None, None, None)

        gateway.compressor = FailedUnknown()
        exchange = await gateway.open(history, {}, "id")
        assert exchange.event["compression"] == "failed_unknown"
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        await gateway.finish(exchange, "completed")
