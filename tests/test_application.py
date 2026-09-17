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

    def test_nested_tool_chain_protects_the_earliest_call(self):
        rows = [
            {"role": "user", "content": "u0"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
            {"role": "user", "content": "u4"},
            {"role": "user", "content": "u5"},
            {"role": "tool", "tool_call_id": "t", "content": "r6"},
            {"role": "user", "content": "u7"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "k"}]},
            {"role": "user", "content": "u9"},
            {"role": "assistant", "content": "a10"},
            {"role": "user", "content": "u11"},
            {"role": "tool", "tool_call_id": "k", "content": "r12"},
        ]
        assert eligible_indices(rows) == [0, 1, 2, 3, 4, 5, 6, 7]

    async def test_direct_route_does_not_mutate_the_request(self, gateway, history):
        history["model"] = "cheap"
        original = copy.deepcopy(history)
        exchange = await gateway.open(history, {}, "private")
        assert gateway.compressor.calls
        assert history == original
        assert gateway.transport.calls[0][1]["messages"][3]["content"] == "compressed"
        await gateway.finish(exchange, "completed")

    async def test_model_defaults_are_not_mutated_by_compression(self, settings):
        model = settings.models["cheap"]
        settings.models["cheap"] = replace(
            model,
            defaults={
                "messages": [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "old"},
                    {"role": "user", "content": "current"},
                ]
            },
        )
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events())
        exchange = await gateway.open({"model": "cheap"}, {}, "private")
        assert settings.models["cheap"].defaults["messages"][0]["content"] == "old"
        assert transport.calls[0][1]["messages"][0]["content"] == "compressed"
        await gateway.finish(exchange, "completed")

    async def test_in_place_compressor_structure_mutation_is_rejected(self, gateway, history):
        class InPlace:
            async def compress(self, messages, model):
                messages[0]["extra"] = True
                return CompressionResult(messages, "savings", 1, 1, 0)

        gateway.compressor = InPlace()
        exchange = await gateway.open(history, {}, "private")
        assert exchange.event["compression"] == "failed_unknown"
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        await gateway.finish(exchange, "completed")

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


class TestFinishContracts:
    async def test_close_failure_cannot_replace_a_completed_response(self, gateway, request_body):
        upstream = Response(close_error=RuntimeError("private close diagnostic"))
        gateway.transport.responses = [upstream]
        exchange = await gateway.open(request_body, {}, "close-1")
        await gateway.finish(exchange, "completed")
        (event,) = gateway.events.records
        assert event["outcome"] == "failed"
        assert event["close_failed"] is True
        assert "private" not in str(event)
        assert upstream.closed

    @pytest.mark.parametrize("outcome", ["failed", "cancelled", "interrupted"])
    async def test_close_failure_keeps_an_already_failed_outcome(
        self, gateway, request_body, outcome
    ):
        gateway.transport.responses = [Response(close_error=RuntimeError("private"))]
        exchange = await gateway.open(request_body, {}, "close-2")
        await gateway.finish(exchange, outcome)
        (event,) = gateway.events.records
        assert event["outcome"] == outcome
        assert event["close_failed"] is True

    async def test_close_success_records_no_close_failure(self, gateway, request_body):
        exchange = await gateway.open(request_body, {}, "close-3")
        await gateway.finish(exchange, "completed")
        (event,) = gateway.events.records
        assert event["outcome"] == "completed"
        assert "close_failed" not in event

    async def test_close_failure_still_emits_exactly_one_event(self, gateway, request_body):
        gateway.transport.responses = [Response(close_error=RuntimeError("private"))]
        exchange = await gateway.open(request_body, {}, "close-4")
        await gateway.finish(exchange, "completed")
        await gateway.finish(exchange, "completed")
        assert exchange.finished is True
        assert len(gateway.events.records) == 1

    async def test_close_cancellation_emits_then_propagates(self, gateway, request_body):
        gateway.transport.responses = [Response(close_error=asyncio.CancelledError())]
        exchange = await gateway.open(request_body, {}, "close-5")
        with pytest.raises(asyncio.CancelledError):
            await gateway.finish(exchange, "completed")
        (event,) = gateway.events.records
        assert event["outcome"] == "failed"
        assert event["close_failed"] is True


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

    async def test_reconstructed_gateway_forgets_cooldowns(self, settings, request_body):
        """Restart is a documented single-worker trade-off: cooldowns are process-local."""
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [ConnectFailure(), Response()]
        exchange = await gateway.open(request_body, {}, "before")
        await gateway.finish(exchange, "completed")
        assert gateway._cooldowns[("cheap", "a")] > now[0]

        restarted = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        assert restarted._cooldowns == {}
        exchange = await restarted.open(request_body, {}, "after")
        assert transport.calls[-1][0].name == "a"
        await restarted.finish(exchange, "completed")

    async def test_reconstructed_gateway_resets_round_robin_position(self, settings, request_body):
        """Restart is a documented single-worker trade-off: positions are process-local."""
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events())
        exchange = await gateway.open(request_body, {}, "before")
        await gateway.finish(exchange, "completed")
        assert gateway._positions["cheap"] == 1

        restarted = Gateway(settings, Router(), Compressor(), transport, Events())
        assert restarted._positions == {}
        exchange = await restarted.open(request_body, {}, "after")
        assert transport.calls[-1][0].name == "a"
        await restarted.finish(exchange, "completed")

    async def test_fresh_alternate_is_available_at_a_zero_clock(self, settings, request_body):
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: 0.0)
        transport.responses = [Response(503), Response()]
        exchange = await gateway.open(request_body, {}, "id")
        assert exchange.event["endpoint"] == "b"
        assert exchange.event["attempts"] == 2
        await gateway.finish(exchange, "completed")

    async def test_cooled_alternate_keeps_the_retryable_response(self, settings, request_body):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        transport.responses = [ConnectFailure(), Response(), Response(503)]
        exchange = await gateway.open(request_body, {}, "one")
        await gateway.finish(exchange, "completed")
        assert gateway._cooldowns[("cheap", "a")] > now[0]

        exchange = await gateway.open(request_body, {}, "two")
        assert exchange.response.status == 503
        assert len(transport.calls) == 3
        await gateway.finish(exchange, "failed")

    async def test_cooldown_expiry_allows_failover_off_a_rejected_replica(
        self, settings, request_body
    ):
        now = [100.0]
        transport = Transport()
        gateway = Gateway(settings, Router(), Compressor(), transport, Events(), lambda: now[0])
        gateway._cooldown("cheap", "b", "30")
        now[0] = 130.0
        transport.responses = [Response(503), Response()]
        exchange = await gateway.open(request_body, {}, "id")
        assert exchange.response.status == 200
        assert [call[0].name for call in transport.calls] == ["a", "b"]
        await gateway.finish(exchange, "completed")

    async def test_single_replica_is_never_reused_within_a_request(self, settings, request_body):
        model = settings.models["cheap"]
        settings.models["cheap"] = replace(model, endpoints=model.endpoints[:1])
        single = replace(settings, cooldown_seconds=0)
        transport = Transport()
        transport.responses = [Response(503), Response()]
        gateway = Gateway(single, Router(), Compressor(), transport, Events(), lambda: 0.0)
        exchange = await gateway.open(request_body, {}, "id")
        assert exchange.response.status == 503
        assert len(transport.calls) == 1
        await gateway.finish(exchange, "failed")

    async def test_round_robin_skips_a_cooled_start_endpoint(self):
        model = Model(
            "m",
            "m",
            tuple(Endpoint(name, f"http://{name}/v1") for name in ("a", "b", "c")),
        )
        transport = Transport()
        gateway = Gateway(
            Settings({"m": model}, {}, "key"),
            Router(),
            Compressor(),
            transport,
            Events(),
            lambda: 100.0,
        )
        gateway._cooldown("m", "a", "60")
        exchange = await gateway.open({"model": "m", "messages": []}, {}, "id")
        assert transport.calls[-1][0].name == "b"
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
        assert exchange.event["tokens_before"] is None
        assert gateway.transport.calls[0][1]["messages"] == history["messages"]
        await gateway.finish(exchange, "completed")
