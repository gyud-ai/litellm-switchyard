"""Small interfaces that isolate the application from external APIs."""

from collections.abc import AsyncIterator
from typing import Protocol

from .domain import CompressionResult, Endpoint, Model, Pair, Payload, RoutingResult


class TierRouter(Protocol):
    """Select a tier without performing backend inference or mutating input."""

    async def route(self, request: Payload, pair: Pair) -> RoutingResult:
        """Return a gateway-owned prepared request and selected tier."""
        ...


class HistoryCompressor(Protocol):
    """Compress eligible history without changing its message structure."""

    async def compress(self, messages: list[Payload], model: Model) -> CompressionResult:
        """Return compression results without mutating the supplied messages."""
        ...


class BackendResponse(Protocol):
    """An open response whose owner must close it, including on cancellation."""

    @property
    def status(self) -> int:
        """Return the HTTP status."""
        ...

    @property
    def headers(self) -> dict[str, str]:
        """Return response headers."""
        ...

    def chunks(self) -> AsyncIterator[bytes]:
        """Iterate the response with backpressure."""
        ...

    async def close(self) -> None:
        """Release the connection."""
        ...


class BackendTransport(Protocol):
    """Send a single attempt, without hidden retries or tier selection."""

    async def send(
        self, endpoint: Endpoint, request: Payload, headers: dict[str, str]
    ) -> BackendResponse:
        """Open the upstream response or raise a gateway-owned failure."""
        ...


class EventSink(Protocol):
    """Consume sanitized gateway events."""

    def emit(self, event: Payload) -> None:
        """Write an event without interpreting request payloads."""
        ...
