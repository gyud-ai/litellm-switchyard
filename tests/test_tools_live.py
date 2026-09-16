"""Agentic tool-use checks against a running stack (`pytest tests/ -m live`).

Spans from a single echo-tool round trip to a multi-step calculator task
executed as a real tool-calling loop: the test plays the tool runtime,
the model decides which tools to call. Every turn must stay inside the
switchyard pair (whatever tier stage routing picks).
"""

from __future__ import annotations

import json

import pytest

from conftest import api_post

pytestmark = pytest.mark.live

MAX_TOKENS_PER_TURN = 3000
MAX_TURNS = 8


def _routed_to(headers: dict) -> str | None:
    for key, value in headers.items():
        if key.lower() == "x-litellm-model-name":
            return value
    return None


def _chat(proxy_url, master_key, messages, tools, model="switchyard"):
    status, headers, body = api_post(
        f"{proxy_url}/v1/chat/completions",
        master_key,
        {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": MAX_TOKENS_PER_TURN,
        },
    )
    assert status == 200, body
    return headers, body


def _run_tool_loop(
    proxy_url, master_key, expected_pair, messages, tools, runtime, model="switchyard"
):
    """Drive turns until the model stops calling tools. Returns transcript info."""
    tiers = []
    calls_executed = 0
    for _ in range(MAX_TURNS):
        headers, body = _chat(proxy_url, master_key, messages, tools, model=model)
        tier = _routed_to(headers)
        assert tier in (expected_pair["cheap"], expected_pair["expensive"]), (
            f"turn routed outside the switchyard pair: {tier!r}"
        )
        tiers.append(tier)
        message = body["choices"][0]["message"]
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            }
        )
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return {
                "tiers": tiers,
                "calls_executed": calls_executed,
                "final": message.get("content") or "",
                "transcript": messages,
            }
        for call in tool_calls:
            func = call["function"]
            result = runtime[func["name"]](**json.loads(func["arguments"]))
            calls_executed += 1
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": str(result)}
            )
    raise AssertionError(
        f"model kept calling tools after {MAX_TURNS} turns; transcript: "
        + json.dumps(messages)[:3000]
    )


def test_echo_tool_round_trip(proxy_url, master_key, expected_pair, expected_group):
    """Simplest tool use: one call, one result, final answer."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo back the given text exactly",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]
    outcome = _run_tool_loop(
        proxy_url,
        master_key,
        expected_pair,
        messages=[
            {
                "role": "user",
                "content": "Call the echo tool with text hello switchyard. "
                "Only call the tool, do not write anything else.",
            }
        ],
        tools=tools,
        runtime={"echo": lambda text: text},
        model=expected_group,
    )
    assert outcome["calls_executed"] == 1
    first_call = outcome["transcript"][1]["tool_calls"][0]
    assert first_call["function"]["name"] == "echo"
    assert json.loads(first_call["function"]["arguments"]) == {
        "text": "hello switchyard"
    }
    assert outcome["final"], "empty final answer after tool result"


def test_multi_step_calculator_task(proxy_url, master_key, expected_pair, expected_group):
    """Real-world shape: word problem needing multiply, multiply, then add."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "multiply",
                "description": "Multiply two numbers",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    "required": ["a", "b"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two numbers",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    "required": ["a", "b"],
                },
            },
        },
    ]
    outcome = _run_tool_loop(
        proxy_url,
        master_key,
        expected_pair,
        messages=[
            {
                "role": "user",
                "content": "A bookstore sells notebooks for 4 dollars each and pens "
                "for 2 dollars each. What is the total price for 3 notebooks "
                "and 5 pens? Use the provided tools for every arithmetic step. "
                "Do not compute anything in your head.",
            }
        ],
        tools=tools,
        runtime={"multiply": lambda a, b: a * b, "add": lambda a, b: a + b},
        model=expected_group,
    )
    assert outcome["calls_executed"] >= 2, (
        f"expected a multi-step solution, only {outcome['calls_executed']} tool call(s)"
    )
    assert "22" in outcome["final"], (
        f"wrong total, expected 22 somewhere in: {outcome['final'][:500]!r}"
    )
