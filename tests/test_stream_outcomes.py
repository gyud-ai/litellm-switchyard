"""Terminal stream-outcome labeling through fake ports and property invariants."""

import asyncio
import json

import anyio
import pytest
from conftest import Compressor, Events, Response, Router, Transport
from hypothesis import given
from hypothesis import strategies as st

from switchyard_gateway.adapters.ingress import OwnedStream, _usage, sse_body
from switchyard_gateway.application import Exchange, Gateway
from switchyard_gateway.domain import Endpoint, GatewayError, Model, Pair, Settings

pytestmark = pytest.mark.unit

_DONE_FRAME = b"data: [DONE]\n\n"
_INTERRUPTED_FRAME = (
    b'data: {"error":{"message":"upstream_stream_interrupted",'
    b'"type":"gateway_error","code":"upstream_stream_interrupted"}}\n\n'
)
_INTERRUPTED_BODY = {
    "error": {
        "message": "upstream_stream_interrupted",
        "type": "gateway_error",
        "code": "upstream_stream_interrupted",
    }
}
_SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}
_OUTCOMES = {"completed", "interrupted", "cancelled"}


def _model(name: str = "direct") -> Model:
    return Model(name, f"{name}-backend", (Endpoint(f"{name}-a", f"http://{name}-a/v1"),))


def _gateway() -> Gateway:
    settings = Settings(
        {"direct": _model(), "expensive": _model("expensive"), "cheap": _model("cheap")},
        {"switchyard": Pair("switchyard", "expensive", "cheap")},
        "client-key",
    )
    return Gateway(settings, Router(), Compressor(), Transport(), Events())


async def _open(gateway: Gateway, upstream: Response) -> Exchange:
    gateway.transport.responses = [upstream]
    request = {"model": "direct", "messages": [{"role": "user", "content": "hello"}]}
    return await gateway.open(request, {}, "test")


async def _collect(gateway: Gateway, exchange: Exchange) -> list[bytes]:
    return [frame async for frame in sse_body(gateway, exchange, "direct")]


class _SlowClose(Response):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.closing = anyio.Event()
        self.release = anyio.Event()

    async def close(self) -> None:
        self.closing.set()
        await self.release.wait()
        self.closed = True


class TestSseBodyOutcomes:
    async def test_completion_records_completed_with_exactly_one_done(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames.count(_DONE_FRAME) == 1
        assert _INTERRUPTED_FRAME not in frames
        assert exchange.event["stream_outcome"] == "completed"

    async def test_parse_failure_records_interrupted_and_frames_terminal_error(self):
        gateway = _gateway()
        upstream = Response(chunks=[b"data: {bad}\n\n"])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames == [_INTERRUPTED_FRAME]
        assert exchange.event["stream_outcome"] == "interrupted"
        terminal = json.loads(_INTERRUPTED_FRAME.removeprefix(b"data: ").removesuffix(b"\n\n"))
        assert terminal == _INTERRUPTED_BODY

    async def test_eof_without_done_records_interrupted(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames[:-1] == [b'data: {"choices":[]}\n\n']
        assert frames[-1] == _INTERRUPTED_FRAME
        assert exchange.event["stream_outcome"] == "interrupted"

    async def test_midstream_read_failure_records_interrupted(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\n'])
        upstream.error = GatewayError("upstream_read_failed")
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames[-1] == _INTERRUPTED_FRAME
        assert _DONE_FRAME not in frames
        assert exchange.event["stream_outcome"] == "interrupted"

    async def test_oversized_event_records_interrupted(self, monkeypatch):
        monkeypatch.setattr("switchyard_gateway.adapters.ingress._MAX_RESPONSE", 4)
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames == [_INTERRUPTED_FRAME]
        assert exchange.event["stream_outcome"] == "interrupted"

    async def test_cancellation_records_cancelled_without_a_terminator(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\n'])
        upstream.error = asyncio.CancelledError()
        exchange = await _open(gateway, upstream)
        frames = []
        with pytest.raises(asyncio.CancelledError):
            async for frame in sse_body(gateway, exchange, "direct"):
                frames.append(frame)
        assert frames == [b'data: {"choices":[]}\n\n']
        assert exchange.event["stream_outcome"] == "cancelled"


class TestUsageAllowlist:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"prompt_tokens": 1}, {"prompt_tokens": 1}),
            ({"completion_tokens": 0}, {"completion_tokens": 0}),
            ({"total_tokens": 7}, {"total_tokens": 7}),
            ({"prompt_tokens": 2, "other": "kept out"}, {"prompt_tokens": 2}),
            ({"prompt_tokens": True}, {}),
            ({"prompt_tokens": "5"}, {}),
            ({"prompt_tokens": -1}, {}),
            ({"other": 3}, {}),
            ({}, {}),
            ("not-a-mapping", None),
        ],
    )
    def test_only_nonnegative_integer_usage_fields_are_recognized(self, value, expected):
        assert _usage(value) == expected


class TestSseRewriting:
    async def test_alias_is_rewritten_and_json_is_compact(self):
        gateway = _gateway()
        upstream = Response(
            chunks=[b'data: {"model":"backend","choices":[],"usage":{"total_tokens":4}}\n\n']
        )
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        expected = b'data: {"model":"direct","choices":[],"usage":{"total_tokens":4}}\n\n'
        assert frames[0] == expected
        assert exchange.event["usage"] == {"total_tokens": 4}

    async def test_multiline_data_fields_join_with_newlines(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"model":\ndata: "backend"}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames[0] == b'data: {"model":"direct"}\n\n'

    async def test_non_data_lines_are_preserved(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'event: update\ndata: {"choices":[]}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames[0] == b'event: update\ndata: {"choices":[]}\n\n'

    async def test_data_without_a_space_after_the_colon_is_parsed(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data:{"model":"backend"}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames[0] == b'data: {"model":"direct"}\n\n'

    @pytest.mark.parametrize("wire", [b"data:\t[DONE]\n\n", b"data: X [DONE]\n\n"])
    async def test_near_miss_terminators_are_interrupted(self, wire):
        gateway = _gateway()
        upstream = Response(chunks=[wire])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames == [_INTERRUPTED_FRAME]

    async def test_upstream_error_frames_are_not_forwarded(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"error":{"message":"private upstream detail"}}\n\n'])
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames == [_INTERRUPTED_FRAME]
        assert b"private" not in b"".join(frames)


class TestEventSizeLimit:
    @pytest.mark.parametrize("split", [False, True])
    async def test_exact_event_and_buffer_limits_are_accepted(self, monkeypatch, split):
        monkeypatch.setattr("switchyard_gateway.adapters.ingress._MAX_RESPONSE", 12)
        chunks = [b"hello world!", b"\n\ndata: [DONE]\n\n"]
        if not split:
            chunks = [b"".join(chunks)]
        gateway = _gateway()
        upstream = Response(chunks=chunks)
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        assert frames == [b"hello world!\n\n", _DONE_FRAME]
        assert exchange.event["stream_outcome"] == "completed"


class TestOwnedStreamOutcomes:
    async def test_completion_finishes_completed_once(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})
        messages: list[dict] = []

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            messages.append(message)

        await stream(_SCOPE, receive, send)
        body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
        assert body.count(_DONE_FRAME) == 1
        assert exchange.response.closed
        assert len(gateway.events.records) == 1
        assert gateway.events.records[-1]["outcome"] == "completed"

    async def test_media_type_headers_and_alias_are_applied(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"model":"backend"}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {"x-request-id": "rid"})
        assert stream.media_type == "text/event-stream"
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["x-request-id"] == "rid"
        messages: list[dict] = []

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            messages.append(message)

        await stream(_SCOPE, receive, send)
        body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
        assert b'"model":"direct"' in body
        assert gateway.events.records[-1]["outcome"] == "completed"

    async def test_unexpected_exit_after_first_frame_is_interrupted(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})
        calls = 0

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("private upstream detail")

        with pytest.raises(RuntimeError):
            await stream(_SCOPE, receive, send)
        assert exchange.response.closed
        assert gateway.events.records[-1]["outcome"] == "interrupted"
        assert "private" not in json.dumps(gateway.events.records)

    async def test_cancellation_before_iteration_finishes_cancelled(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await stream(_SCOPE, receive, send)
        assert exchange.response.closed
        assert gateway.events.records[-1]["outcome"] == "cancelled"

    async def test_legacy_asgi_spec_uses_receive_for_disconnect(self):
        gateway = _gateway()
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})
        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        finished = asyncio.Event()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            await finished.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            await asyncio.sleep(0)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                finished.set()

        await stream(scope, receive, send)
        assert receive_calls == 1
        assert exchange.response.closed
        assert gateway.events.records[-1]["outcome"] == "completed"

    async def test_cancellation_during_finish_still_closes_upstream(self):
        gateway = _gateway()
        upstream = _SlowClose(chunks=[b'data: {"choices":[]}\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            await asyncio.sleep(0)

        async def drive():
            await stream(_SCOPE, receive, send)

        async with anyio.create_task_group() as group:
            group.start_soon(drive)
            await upstream.closing.wait()
            group.cancel_scope.cancel()
            upstream.release.set()
        assert upstream.closed
        assert len(gateway.events.records) == 1
        assert gateway.events.records[-1]["outcome"] == "interrupted"


def _run_stream(chunks: list[bytes]) -> tuple[Gateway, Exchange, list[bytes]]:
    gateway = _gateway()

    async def exercise():
        upstream = Response(chunks=chunks)
        exchange = await _open(gateway, upstream)
        frames = await _collect(gateway, exchange)
        return exchange, frames

    exchange, frames = asyncio.run(exercise())
    return gateway, exchange, frames


@given(st.lists(st.binary(max_size=64), max_size=8))
def test_any_upstream_wire_ends_with_exactly_one_terminal_frame(chunks):
    gateway, exchange, frames = _run_stream(chunks)
    done = frames.count(_DONE_FRAME)
    interrupted = frames.count(_INTERRUPTED_FRAME)
    outcome = exchange.event["stream_outcome"]
    assert outcome in _OUTCOMES
    assert done + interrupted == 1
    if done:
        assert (outcome, interrupted) == ("completed", 0)
    else:
        assert (outcome, interrupted) == ("interrupted", 1)
    assert gateway.transport.calls


@given(st.booleans(), st.booleans())
def test_fragmentation_does_not_change_emitted_frames(split_first, split_second):
    wire = (
        b'data: {"model":"backend","choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: {"usage":{"total_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )
    cuts = {0, len(wire)}
    if split_first:
        cuts.add(len(wire) // 3)
    if split_second:
        cuts.add(2 * len(wire) // 3)
    points = sorted(cuts)
    chunks = [wire[lo:hi] for lo, hi in zip(points[:-1], points[1:], strict=True)]
    _, whole_exchange, whole = _run_stream([wire])
    _, split_exchange, split = _run_stream(chunks)
    assert b"".join(split) == b"".join(whole)
    assert split_exchange.event["stream_outcome"] == whole_exchange.event["stream_outcome"]
    assert (
        split_exchange.event.get("usage")
        == whole_exchange.event.get("usage")
        == {"total_tokens": 3}
    )


@given(st.sampled_from(["completed", "runtime", "cancelled"]))
def test_owned_stream_only_labels_cancellation_as_cancelled(behavior):
    gateway = _gateway()

    async def exercise():
        upstream = Response(chunks=[b'data: {"choices":[]}\n\ndata: [DONE]\n\n'])
        exchange = await _open(gateway, upstream)
        stream = OwnedStream(gateway, exchange, "direct", {})

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            if behavior == "runtime":
                raise RuntimeError("private")
            if behavior == "cancelled":
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        raised = None
        try:
            await stream(_SCOPE, receive, send)
        except BaseException as error:
            raised = error
        return exchange, raised

    exchange, raised = asyncio.run(exercise())
    outcome = gateway.events.records[-1]["outcome"]
    assert outcome in _OUTCOMES
    assert (outcome == "cancelled") == (behavior == "cancelled")
    assert (raised is None) == (behavior == "completed")
    assert exchange.response.closed
    assert len(gateway.events.records) == 1
