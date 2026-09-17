"""Real pinned adapter contracts and HTTP transport failure behavior."""

import asyncio
import copy
import importlib.metadata
import importlib.util
import json

import httpx
import pytest

from switchyard_gateway.adapters.headroom import HeadroomCompressor
from switchyard_gateway.adapters.httpx import HttpxTransport
from switchyard_gateway.adapters.switchyard import SwitchyardRouter
from switchyard_gateway.domain import ConnectFailure, GatewayError, StagePolicy

pytestmark = pytest.mark.integration


class TestSwitchyardContract:
    async def test_first_turn_and_patch_preserve_extensions(self, settings, request_body):
        request_body["custom_extension"] = {"enabled": True}
        request_body["messages"][0]["name"] = "caller"
        original = copy.deepcopy(request_body)
        result = await SwitchyardRouter(StagePolicy()).route(
            request_body, settings.pairs["switchyard"]
        )
        assert result.tier == "efficient"
        assert result.request["messages"][0]["content"] == StagePolicy().efficient_system_prompt
        assert result.request["messages"][-1] == original["messages"][0]
        assert result.request["custom_extension"] == {"enabled": True}
        assert request_body == original

    async def test_oom_escalates_preserving_tool_exchange(self, settings, history):
        history["messages"][-1]["content"] = "torch.cuda.OutOfMemoryError: CUDA out of memory"
        original = copy.deepcopy(history)
        result = await SwitchyardRouter(StagePolicy()).route(history, settings.pairs["switchyard"])
        assert result.tier == "capable"
        assert all(row in result.request["messages"] for row in original["messages"])
        assert history == original

    async def test_concurrent_requests_have_isolated_capture(self, settings, request_body):
        router = SwitchyardRouter(StagePolicy())
        results = await asyncio.gather(
            *[
                router.route(request_body | {"tag": i}, settings.pairs["switchyard"])
                for i in range(8)
            ]
        )
        assert [result.request["tag"] for result in results] == list(range(8))

    async def test_unsupported_content_is_gateway_error(self, settings, request_body):
        request_body["messages"][0]["content"] = [{"type": "image_url", "image_url": "private"}]
        with pytest.raises(GatewayError, match="unsupported_message_content"):
            await SwitchyardRouter(StagePolicy()).route(request_body, settings.pairs["switchyard"])

    async def test_zero_or_multiple_capture_calls_rejected(
        self, settings, request_body, monkeypatch
    ):
        class InvalidAlgorithm:
            async def run(self, request):
                return [], {}

        monkeypatch.setattr(
            "switchyard_gateway.adapters.switchyard.algorithms.stage_router",
            lambda *args, **kwargs: InvalidAlgorithm(),
        )
        with pytest.raises(GatewayError, match="invalid_routing_result"):
            await SwitchyardRouter(StagePolicy()).route(request_body, settings.pairs["switchyard"])


class TestHeadroomContract:
    async def test_structural_savings_without_input_mutation(self, settings):
        compressor = HeadroomCompressor()
        messages = [
            {
                "role": "user",
                "content": json.dumps(
                    [{"id": i, "status": "ok", "region": "west", "count": 1} for i in range(300)]
                ),
            }
        ]
        original = copy.deepcopy(messages)
        try:
            result = await compressor.compress(messages, settings.models["cheap"])
            assert result.outcome == "savings"
            assert result.tokens_saved > 0
            assert messages == original
        finally:
            await compressor.close()

    async def test_short_message_is_unchanged(self, settings):
        compressor = HeadroomCompressor()
        try:
            result = await compressor.compress(
                [{"role": "user", "content": "hi"}], settings.models["cheap"]
            )
            assert result.messages == [{"role": "user", "content": "hi"}]
        finally:
            await compressor.close()

    async def test_swallowed_failure_is_not_success(self, settings, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "switchyard_gateway.adapters.headroom.compress",
            lambda *a, **k: SimpleNamespace(tokens_before=0),
        )
        compressor = HeadroomCompressor()
        try:
            result = await compressor.compress(
                [{"role": "user", "content": "private"}], settings.models["cheap"]
            )
            assert result.outcome == "failed_unknown"
            assert result.tokens_saved is None
        finally:
            await compressor.close()

    async def test_ml_is_disabled_and_concurrent_calls_isolated(self, settings, monkeypatch):
        from types import SimpleNamespace

        seen = []

        def compress(messages, **kwargs):
            seen.append(kwargs["config"].kompress_model)
            return SimpleNamespace(
                messages=messages, tokens_before=10, tokens_after=10, tokens_saved=0
            )

        monkeypatch.setattr("switchyard_gateway.adapters.headroom.compress", compress)
        compressor = HeadroomCompressor()
        try:
            results = await asyncio.gather(
                *[
                    compressor.compress(
                        [{"role": "user", "content": str(i)}], settings.models["cheap"]
                    )
                    for i in range(6)
                ]
            )
            assert seen == ["disabled"] * 6
            assert [r.messages[0]["content"] for r in results] == list(map(str, range(6)))
        finally:
            await compressor.close()


class TestHttpxContract:
    async def test_endpoint_credentials_and_unknown_fields(self, settings):
        seen = []

        async def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"choices": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await HttpxTransport(client).send(
                settings.models["cheap"].endpoints[0], {"extra": "value"}, {"x-session-id": "id"}
            )
            assert seen[0].headers["authorization"] == "Bearer backend-key"
            assert seen[0].headers["x-session-id"] == "id"
            assert str(seen[0].url) == "http://replica-a/v1/chat/completions"
            assert json.loads(seen[0].content) == {"extra": "value"}
            await response.close()

    @pytest.mark.parametrize(
        "failure, expected",
        [
            (httpx.ConnectError("private URL"), ConnectFailure),
            (httpx.ReadTimeout("private URL"), GatewayError),
        ],
    )
    async def test_normalizes_failures(self, settings, failure, expected):
        async def handler(request):
            raise failure

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(expected) as caught:
                await HttpxTransport(client).send(settings.models["cheap"].endpoints[0], {}, {})
            assert "private" not in str(caught.value)

    def test_exact_pins_and_no_litellm(self):
        assert importlib.metadata.version("nemo-switchyard") == "0.2.0"
        assert importlib.metadata.version("headroom-ai") == "0.37.0"
        assert importlib.util.find_spec("litellm") is None


class TestCompressionCancellation:
    async def test_cancelled_worker_keeps_capacity_until_completion(self, settings, monkeypatch):
        import threading
        from types import SimpleNamespace

        running = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_compress(messages, **kwargs):
            calls.append(messages)
            running.set()
            release.wait(timeout=5)
            return SimpleNamespace(
                messages=messages, tokens_before=10, tokens_after=10, tokens_saved=0
            )

        monkeypatch.setattr("switchyard_gateway.adapters.headroom.compress", blocking_compress)
        compressor = HeadroomCompressor(workers=1)
        first = asyncio.create_task(
            compressor.compress([{"role": "user", "content": "one"}], settings.models["cheap"])
        )
        try:
            await asyncio.to_thread(running.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(
                compressor.compress([{"role": "user", "content": "two"}], settings.models["cheap"])
            )
            await asyncio.sleep(0)
            assert len(calls) == 1
            assert not second.done()
            release.set()
            result = await asyncio.wait_for(second, 5)
            assert result.messages[0]["content"] == "two"
        finally:
            release.set()
            await compressor.close()


class TestDependencyDrift:
    async def test_sdk_type_error_is_server_failure(self, settings, request_body, monkeypatch):
        def broken_api(*args, **kwargs):
            raise TypeError("private SDK diagnostic")

        monkeypatch.setattr(
            "switchyard_gateway.adapters.switchyard.algorithms.stage_router", broken_api
        )
        with pytest.raises(GatewayError) as caught:
            await SwitchyardRouter(StagePolicy()).route(request_body, settings.pairs["switchyard"])
        assert caught.value.status == 502
        assert str(caught.value) == "routing_failed"
