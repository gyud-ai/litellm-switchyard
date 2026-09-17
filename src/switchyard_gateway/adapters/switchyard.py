"""Switchyard 0.2.0 translation and decision capture, with no network access."""

import copy
import json
from dataclasses import asdict

from switchyard.libsy import LlmTarget, algorithms

from ..domain import GatewayError, Pair, Payload, RoutingResult, StagePolicy, Tier


def _content(value: object) -> list[Payload]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if isinstance(value, list) and all(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        for block in value
    ):
        return [{"type": "text", "text": block["text"]} for block in value]
    raise GatewayError("unsupported_message_content", 400)


def _normalize(message: Payload) -> Payload:
    role = message["role"]
    content = _content(message.get("content"))
    if role == "assistant":
        for call in message.get("tool_calls", []):
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise GatewayError("invalid_tool_arguments", 400)
            content.append(
                {
                    "type": "tool_call",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": arguments,
                }
            )
    if role == "tool":
        content = [
            {
                "type": "tool_result",
                "tool_call_id": message["tool_call_id"],
                "content": content,
                "is_error": None,
            }
        ]
    return {"role": role, "content": content}


def _restore(message: Payload) -> Payload:
    # Stage-generated messages are text-only. Original messages are restored verbatim below.
    content = message.get("content", [])
    if not all(block.get("type") == "text" for block in content):
        raise GatewayError("unsupported_routing_rewrite")
    return {"role": message["role"], "content": "\n".join(block["text"] for block in content)}


class _CaptureClient:
    def __init__(self, tier: Tier) -> None:
        self.tier = tier
        self.requests: list[Payload] = []

    async def call(self, request: Payload) -> Payload:
        self.requests.append(copy.deepcopy(request))
        return {
            "model": self.tier,
            "outputs": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stop_reason": "end_turn",
                }
            ],
        }


class SwitchyardRouter:
    """Implement tier selection using the pinned library's client-target interface."""

    def __init__(self, policy: StagePolicy) -> None:
        self.policy = policy

    async def route(self, request: Payload, pair: Pair) -> RoutingResult:
        """Capture exactly one stage-selected request and discard the synthetic response."""
        try:
            original = copy.deepcopy(request)
            normalized = [_normalize(row) for row in original["messages"]]
        except GatewayError:
            raise
        except KeyError, TypeError, ValueError:
            raise GatewayError("invalid_routing_request", 400) from None
        try:
            capable, efficient = _CaptureClient("capable"), _CaptureClient("efficient")
            router = algorithms.stage_router(
                LlmTarget(pair.capable, capable),
                LlmTarget(pair.efficient, efficient),
                **asdict(self.policy),
            )
            await router.run({"model": "auto", "messages": normalized})
            selected = [client for client in (capable, efficient) if client.requests]
            if len(selected) != 1 or len(selected[0].requests) != 1:
                raise GatewayError("invalid_routing_result")
            client = selected[0]
            captured = client.requests[0]
            rewritten: list[Payload] = []
            for row in captured.get("instructions", []):
                rewritten.append(_restore(row))
            # Preserve extension fields, content block annotations and argument strings.
            remaining = list(zip(normalized, original["messages"], strict=True))
            for row in captured["messages"]:
                match = next(
                    (i for i, (canonical, _) in enumerate(remaining) if canonical == row), None
                )
                if match is None:
                    rewritten.append(_restore(row))
                else:
                    _, source = remaining.pop(match)
                    rewritten.append(source)
            if remaining:
                raise GatewayError("unsupported_routing_rewrite")
            original["messages"] = rewritten
            return RoutingResult(client.tier, original)
        except GatewayError:
            raise
        except Exception:
            raise GatewayError("routing_failed") from None
