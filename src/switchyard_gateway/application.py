"""Request orchestration, compression protection, and replica policy."""

import asyncio
import copy
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .domain import ConnectFailure, GatewayError, Model, Payload, Settings, Tier
from .ports import BackendResponse, BackendTransport, EventSink, HistoryCompressor, TierRouter


@dataclass
class Exchange:
    """An open upstream exchange and its sanitized completion record."""

    response: BackendResponse
    event: Payload
    started: float
    upstream_started: float
    finished: bool = False


def eligible_indices(messages: list[Payload]) -> list[int]:
    """Find old history while retaining live exchanges and cached/instruction rows."""
    recent = [
        max((i for i, row in enumerate(messages) if row.get("role") == role), default=0)
        for role in ("user", "assistant")
    ]
    boundary = min(recent)
    # If a live tool result refers to an older call, protect that entire exchange too.
    while True:
        live_ids = {row.get("tool_call_id") for row in messages[boundary:]}
        earlier = [
            i
            for i, row in enumerate(messages[:boundary])
            if any(call.get("id") in live_ids for call in row.get("tool_calls", []))
        ]
        if not earlier:
            break
        boundary = min(earlier)

    def cached(value: object) -> bool:
        if isinstance(value, dict):
            return "cache_control" in value or any(cached(v) for v in value.values())
        return isinstance(value, list) and any(cached(v) for v in value)

    return [
        i
        for i, row in enumerate(messages[:boundary])
        if row.get("role") not in {"system", "developer"} and not cached(row)
    ]


def _same_structure(before: list[Payload], after: list[Payload]) -> bool:
    if len(before) != len(after):
        return False
    return all(
        {k: v for k, v in old.items() if k != "content"}
        == {k: v for k, v in new.items() if k != "content"}
        for old, new in zip(before, after, strict=True)
    )


class Gateway:
    """Coordinate routing, compression, and forwarding through injected ports."""

    def __init__(
        self,
        settings: Settings,
        router: TierRouter,
        compressor: HistoryCompressor,
        transport: BackendTransport,
        events: EventSink,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.router = router
        self.compressor = compressor
        self.transport = transport
        self.events = events
        self.clock = clock
        self._positions: dict[str, int] = {}
        self._cooldowns: dict[tuple[str, str], float] = {}

    def ready(self) -> bool:
        """Report whether any configured replica is currently eligible for selection."""
        now = self.clock()
        return any(
            self._cooldowns.get((model.name, endpoint.name), 0) <= now
            for model in self.settings.models.values()
            for endpoint in model.endpoints
        )

    def _select(self, model: Model, excluded: set[str]) -> int:
        start = self._positions.get(model.name, 0)
        for offset in range(len(model.endpoints)):
            index = (start + offset) % len(model.endpoints)
            endpoint = model.endpoints[index]
            if (
                endpoint.name not in excluded
                and self._cooldowns.get((model.name, endpoint.name), 0) <= self.clock()
            ):
                self._positions[model.name] = (index + 1) % len(model.endpoints)
                return index
        raise GatewayError("no_available_replica", 503)

    def _cooldown(self, model: str, endpoint: str, retry_after: str = "") -> None:
        seconds = self.settings.cooldown_seconds
        try:
            parsed_seconds = float(retry_after)
            if math.isfinite(parsed_seconds):
                seconds = max(seconds, parsed_seconds)
        except ValueError:
            try:
                date = parsedate_to_datetime(retry_after)
                seconds = max(seconds, (date - datetime.now(UTC)).total_seconds())
            except ValueError, TypeError, OverflowError:
                pass
        self._cooldowns[model, endpoint] = self.clock() + seconds

    async def open(self, request: Payload, headers: dict[str, str], request_id: str) -> Exchange:
        """Prepare a request and open one upstream response, with bounded failover."""
        started = self.clock()
        event: Payload = {"event": "request", "request_id": request_id, "attempts": 0}
        try:
            return await self._open(request, headers, event, started)
        except BaseException as error:
            event.update(
                outcome="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                status=error.status if isinstance(error, GatewayError) else 500,
                error=error.code if isinstance(error, GatewayError) else "request_failed",
                total_ms=(self.clock() - started) * 1000,
            )
            self.events.emit(event)
            raise

    async def _open(
        self, request: Payload, headers: dict[str, str], event: Payload, started: float
    ) -> Exchange:
        alias = request["model"]
        prepared = copy.deepcopy(request)
        route_started = self.clock()
        tier: Tier
        if alias in self.settings.pairs:
            pair = self.settings.pairs[alias]
            event["route"] = pair.name
            decision = await self.router.route(prepared, pair)
            if decision.tier not in {"capable", "efficient"}:
                raise GatewayError("invalid_routing_result")
            model_name = pair.capable if decision.tier == "capable" else pair.efficient
            prepared = decision.request
            tier = decision.tier
        elif alias in self.settings.models:
            event["route"] = alias
            model_name, tier = alias, "direct"
        else:
            raise GatewayError("unknown_model", 404)
        model = self.settings.models[model_name]
        event.update(tier=tier, model=model.name, routing_ms=(self.clock() - route_started) * 1000)
        # Client values override configured defaults; the selected backend model is authoritative.
        prepared = copy.deepcopy(model.defaults) | prepared
        prepared["model"] = model.model_id
        compress_started = self.clock()
        event["compression"] = "bypass"
        if self.settings.compression and headers.get("x-headroom-bypass", "").lower() != "true":
            indices = eligible_indices(prepared["messages"])
            event["compression"] = "no_eligible_history"
            if indices:
                original = [copy.deepcopy(prepared["messages"][i]) for i in indices]
                try:
                    result = await self.compressor.compress(copy.deepcopy(original), model)
                    if result.outcome not in {"savings", "no_savings", "failed_unknown"}:
                        raise GatewayError("invalid_compression_result")
                    if not _same_structure(original, result.messages):
                        raise GatewayError("compression_structure_changed")
                    if result.outcome != "failed_unknown":
                        for i, row in zip(indices, result.messages, strict=True):
                            prepared["messages"][i] = row
                    event.update(
                        compression=result.outcome,
                        tokens_before=result.tokens_before,
                        tokens_after=result.tokens_after,
                        tokens_saved=result.tokens_saved,
                    )
                except Exception:
                    event["compression"] = "failed_unknown"
        event["compression_ms"] = (self.clock() - compress_started) * 1000
        forwarded = {k: v for k, v in headers.items() if k in self.settings.forward_headers}
        excluded: set[str] = set()
        upstream_started = self.clock()
        for attempt in range(2):
            endpoint = model.endpoints[self._select(model, excluded)]
            excluded.add(endpoint.name)
            event.update(endpoint=endpoint.name, attempts=attempt + 1)
            if attempt:
                self.events.emit(
                    {
                        "event": "retry",
                        "request_id": event["request_id"],
                        "model": model.name,
                        "endpoint": endpoint.name,
                    }
                )
            try:
                response = await self.transport.send(endpoint, prepared, forwarded)
            except ConnectFailure:
                self._cooldown(model.name, endpoint.name)
                self.events.emit(
                    {
                        "event": "replica_failure",
                        "request_id": event["request_id"],
                        "model": model.name,
                        "endpoint": endpoint.name,
                        "error": "upstream_connect_failed",
                    }
                )
                if attempt == 0:
                    continue
                raise
            event["status"] = response.status
            retryable = response.status in {429, 502, 503, 504}
            if retryable:
                self._cooldown(model.name, endpoint.name, response.headers.get("retry-after", ""))
                self.events.emit(
                    {
                        "event": "replica_failure",
                        "request_id": event["request_id"],
                        "model": model.name,
                        "endpoint": endpoint.name,
                        "status": response.status,
                    }
                )
            if retryable and attempt == 0:
                # Only discard a real response if another replica is eligible.
                available = any(
                    ep.name not in excluded
                    and self._cooldowns.get((model.name, ep.name), 0) <= self.clock()
                    for ep in model.endpoints
                )
                if available:
                    await response.close()
                    continue
            event["upstream_headers_ms"] = (self.clock() - upstream_started) * 1000
            return Exchange(response, event, started, upstream_started)
        raise GatewayError("no_available_replica", 503)

    async def body(self, exchange: Exchange) -> AsyncIterator[bytes]:
        """Read upstream bytes while measuring first body-byte latency."""
        async for chunk in exchange.response.chunks():
            if chunk and "first_body_byte_ms" not in exchange.event:
                exchange.event["first_body_byte_ms"] = (
                    self.clock() - exchange.upstream_started
                ) * 1000
            yield chunk

    async def finish(self, exchange: Exchange, outcome: str) -> None:
        """Close an exchange and emit exactly one terminal event.

        A close failure must never escape and replace an already-built response,
        so it is recorded on the event instead. Cancellation still propagates
        after the terminal event is emitted.
        """
        if exchange.finished:
            return
        exchange.finished = True
        failure: BaseException | None = None
        try:
            await exchange.response.close()
        except BaseException as error:
            failure = error
        if failure is not None:
            exchange.event["close_failed"] = True
            if outcome == "completed":
                outcome = "failed"
        exchange.event.update(outcome=outcome, total_ms=(self.clock() - exchange.started) * 1000)
        self.events.emit(exchange.event)
        if failure is not None and not isinstance(failure, Exception):
            raise failure
