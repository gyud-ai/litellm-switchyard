"""Property-based checks for routing, history protection, and compression fallbacks."""

import asyncio
import copy

import pytest
from conftest import Events, Router, Transport
from hypothesis import given
from hypothesis import strategies as st

from switchyard_gateway.application import Gateway, _same_structure, eligible_indices
from switchyard_gateway.domain import (
    CompressionResult,
    Endpoint,
    GatewayError,
    Model,
    Pair,
    Settings,
)

pytestmark = pytest.mark.unit

_TEXT = st.text(max_size=16)
_ROLES = st.sampled_from(["system", "developer", "user", "assistant", "tool"])


def _has_cache_control(value: object) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    return isinstance(value, list) and any(_has_cache_control(v) for v in value)


@st.composite
def histories(draw: st.DrawFn) -> list[dict]:
    """Generate conversations with optional tool calls, results, and cached content."""
    rows: list[dict] = []
    for index in range(draw(st.integers(min_value=1, max_value=8))):
        role = draw(_ROLES)
        row: dict = {"role": role}
        if role == "assistant":
            row["content"] = draw(st.none() | _TEXT)
            if draw(st.booleans()):
                row["tool_calls"] = [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "tool", "arguments": draw(_TEXT)},
                    }
                ]
        elif role == "tool":
            row["content"] = draw(_TEXT)
            row["tool_call_id"] = f"call-{draw(st.integers(min_value=0, max_value=9))}"
        else:
            row["content"] = draw(_TEXT)
        if draw(st.booleans()):
            if draw(st.booleans()):
                row["cache_control"] = {"type": "ephemeral"}
            else:
                row["meta"] = {"annotations": [{"cache_control": {"type": "ephemeral"}}]}
        rows.append(row)
    return rows


@st.composite
def assistant_only_histories(draw: st.DrawFn) -> list[dict]:
    return [
        {"role": "assistant", "content": draw(_TEXT)}
        for _ in range(draw(st.integers(min_value=1, max_value=4)))
    ]


@st.composite
def structured_rows(draw: st.DrawFn) -> list[dict]:
    """Rows with a non-content marker so structural comparisons have something to bite on."""
    return [
        {"role": "user", "content": draw(_TEXT), "marker": draw(st.integers())}
        for _ in range(draw(st.integers(min_value=1, max_value=6)))
    ]


@st.composite
def eligible_histories(draw: st.DrawFn) -> dict:
    """A conversation with at least one old, compressible user turn."""
    old = [{"role": "user", "content": draw(_TEXT)} for _ in range(draw(st.integers(1, 4)))]
    rows = [
        *old,
        {"role": "assistant", "content": draw(_TEXT)},
        {"role": "user", "content": draw(_TEXT)},
    ]
    return {"model": "direct", "messages": rows}


def _model(name: str, endpoints: int = 1) -> Model:
    return Model(
        name,
        f"{name}-backend",
        tuple(Endpoint(f"{name}-{i}", f"http://{name}-{i}/v1") for i in range(endpoints)),
    )


def _settings(
    models: dict[str, Model] | None = None, pairs: dict[str, Pair] | None = None
) -> Settings:
    return Settings(models or {"direct": _model("direct")}, pairs or {}, "client-key")


class _ChangingCompressor:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def compress(self, messages: list[dict], model: Model) -> CompressionResult:
        self.calls.append(copy.deepcopy(messages))
        result = copy.deepcopy(messages)
        for row in result:
            if isinstance(row.get("content"), str):
                row["content"] = row["content"].upper()
        return CompressionResult(result, "savings", 100, 10, 90)


class _TrimmingCompressor:
    async def compress(self, messages: list[dict], model: Model) -> CompressionResult:
        return CompressionResult([], "savings", 100, 1, 99)


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


@given(histories())
def test_eligible_indices_are_ordered_unique_and_in_range(rows):
    result = eligible_indices(rows)
    assert result == sorted(result)
    assert len(set(result)) == len(result)
    assert all(0 <= index < len(rows) for index in result)


@given(histories())
def test_protected_rows_are_never_eligible(rows):
    result = eligible_indices(rows)
    for index in result:
        assert rows[index]["role"] not in {"system", "developer"}
        assert not _has_cache_control(rows[index])


@given(histories())
def test_last_user_and_assistant_rows_are_protected(rows):
    result = set(eligible_indices(rows))
    for role in ("user", "assistant"):
        positions = [i for i, row in enumerate(rows) if row.get("role") == role]
        if positions:
            assert max(positions) not in result


@given(histories())
def test_eligible_indices_does_not_mutate_input(rows):
    before = copy.deepcopy(rows)
    eligible_indices(rows)
    assert rows == before


@given(assistant_only_histories())
def test_history_without_a_user_turn_has_no_eligible_rows(rows):
    assert eligible_indices(rows) == []


def test_cross_boundary_tool_exchange_protects_older_call():
    rows = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        },
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "recent"},
        {"role": "tool", "tool_call_id": "x", "content": "live result"},
        {"role": "user", "content": "next"},
    ]
    assert eligible_indices(rows) == []


@given(structured_rows())
def test_same_structure_ignores_content_only_changes(rows):
    after = copy.deepcopy(rows)
    for row in after:
        row["content"] = "replaced"
    assert _same_structure(rows, after)


@given(structured_rows())
def test_same_structure_detects_non_content_changes(rows):
    after = copy.deepcopy(rows)
    after[-1]["marker"] = after[-1]["marker"] + 1
    assert not _same_structure(rows, after)


@given(st.lists(structured_rows(), max_size=3), st.lists(structured_rows(), max_size=3))
def test_same_structure_detects_length_changes(before, after):
    if len(before) == len(after):
        return
    assert not _same_structure(before, after)


@given(
    st.integers(min_value=1, max_value=5),
    st.integers(min_value=1, max_value=4),
)
def test_round_robin_is_even_without_failures(endpoints, rounds):
    model = _model("m", endpoints)
    transport = Transport()
    gateway = Gateway(_settings({"m": model}), Router(), _ChangingCompressor(), transport, Events())

    async def exercise() -> None:
        for _ in range(endpoints * rounds):
            exchange = await gateway.open({"model": "m", "messages": []}, {}, "id")
            await gateway.finish(exchange, "completed")

    _run(exercise())
    counts = {f"m-{index}": 0 for index in range(endpoints)}
    for call in transport.calls:
        counts[call[0].name] += 1
    assert set(counts.values()) == {rounds}


@given(st.text(max_size=8).filter(lambda value: value not in {"capable", "efficient"}))
def test_invalid_routing_tier_is_rejected(tier):
    class WrongRouter:
        async def route(self, request, pair):
            from switchyard_gateway.domain import RoutingResult

            return RoutingResult(tier, request)

    settings = _settings(
        {"capable": _model("capable"), "efficient": _model("efficient")},
        {"pair": Pair("pair", "capable", "efficient")},
    )
    gateway = Gateway(settings, WrongRouter(), _ChangingCompressor(), Transport(), Events())
    with pytest.raises(GatewayError) as caught:
        _run(gateway.open({"model": "pair", "messages": []}, {}, "id"))
    assert caught.value.code == "invalid_routing_result"


@given(eligible_histories())
def test_compression_only_rewrites_eligible_rows(body):
    transport = Transport()
    gateway = Gateway(_settings(), Router(), _ChangingCompressor(), transport, Events())
    original = copy.deepcopy(body["messages"])
    eligible = set(eligible_indices(original))
    exchange = _run(gateway.open(copy.deepcopy(body), {}, "id"))
    _run(gateway.finish(exchange, "completed"))
    sent = transport.calls[0][1]["messages"]
    for index, row in enumerate(original):
        if index not in eligible:
            assert sent[index] == row
    assert len(sent) == len(original)


@given(eligible_histories())
def test_structurally_invalid_compression_falls_back(body):
    transport = Transport()
    gateway = Gateway(_settings(), Router(), _TrimmingCompressor(), transport, Events())
    original = copy.deepcopy(body)
    exchange = _run(gateway.open(original, {}, "id"))
    _run(gateway.finish(exchange, "completed"))
    assert transport.calls[0][1]["messages"] == body["messages"]
    assert exchange.event["compression"] == "failed_unknown"


@given(st.floats(min_value=-1_000_000, max_value=1_000_000, allow_nan=False, allow_infinity=False))
def test_numeric_retry_after_never_shortens_cooldown(value):
    now = [1000.0]
    gateway = Gateway(
        _settings(), Router(), _ChangingCompressor(), Transport(), Events(), lambda: now[0]
    )
    gateway._cooldown("m", "e", repr(value))
    assert gateway._cooldowns[("m", "e")] >= now[0] + gateway.settings.cooldown_seconds


@given(st.text(max_size=16))
def test_invalid_retry_after_uses_configured_cooldown(retry_after):
    if retry_after and _parses(retry_after):
        return
    now = [1000.0]
    gateway = Gateway(
        _settings(), Router(), _ChangingCompressor(), Transport(), Events(), lambda: now[0]
    )
    gateway._cooldown("m", "e", retry_after)
    assert gateway._cooldowns[("m", "e")] == now[0] + gateway.settings.cooldown_seconds


def _parses(value: str) -> bool:
    import math
    from email.utils import parsedate_to_datetime

    try:
        if math.isfinite(float(value)):
            return True
    except ValueError:
        pass
    try:
        parsedate_to_datetime(value)
        return True
    except ValueError, TypeError, OverflowError:
        return False
