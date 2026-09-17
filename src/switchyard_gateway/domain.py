"""Gateway-owned configuration, results, and failure vocabulary."""

from dataclasses import dataclass, field
from typing import Any, Literal

# JSON extensions remain opaque to the application to preserve provider-specific fields.
type Payload = dict[str, Any]
type Tier = Literal["capable", "efficient", "direct"]


class GatewayError(Exception):
    """A sanitized failure safe to expose to a client."""

    def __init__(self, code: str, status: int = 502) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ConnectFailure(GatewayError):
    """Connection establishment failed before a request could be delivered."""

    def __init__(self) -> None:
        super().__init__("upstream_connect_failed")


@dataclass(frozen=True)
class Endpoint:
    """A labelled replica and its private connection settings."""

    name: str
    base_url: str = field(repr=False)
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class Model:
    """A reusable model with one or more equivalent replicas."""

    name: str
    model_id: str
    endpoints: tuple[Endpoint, ...]
    context_window: int = 131072
    defaults: Payload = field(default_factory=dict)


@dataclass(frozen=True)
class Pair:
    """A public route with explicit tier roles."""

    name: str
    capable: str
    efficient: str


@dataclass(frozen=True)
class StagePolicy:
    """Stable gateway settings translated by the routing adapter."""

    picker: str = "efficient_first"
    confidence_threshold: float = 0.5
    recent_window: int = 3
    only_on_wrong_signal_escalation: bool = True
    escalation_note: str = "The efficient tier failed; continue from its work."
    deescalation_note: str = "The capable tier completed the recovery."
    capable_system_prompt: str = "Handle this request as the capable tier."
    efficient_system_prompt: str = "Handle this request as the efficient tier."


@dataclass(frozen=True)
class Settings:
    """Validated configuration consumed by the application."""

    models: dict[str, Model]
    pairs: dict[str, Pair]
    api_key: str = field(repr=False)
    stage: StagePolicy = field(default_factory=StagePolicy)
    compression: bool = True
    compression_workers: int = 2
    cooldown_seconds: float = 30
    connect_timeout: float = 5
    read_timeout: float = 300
    forward_headers: tuple[str, ...] = ("x-session-id", "x-opencode-session")
    host: str = "127.0.0.1"
    port: int = 4000
    max_request_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class RoutingResult:
    """A tier selection with the complete prepared outgoing request."""

    tier: Tier
    request: Payload


@dataclass(frozen=True)
class CompressionResult:
    """A compression outcome; unavailable token counts remain unknown."""

    messages: list[Payload]
    outcome: str
    tokens_before: int | None = None
    tokens_after: int | None = None
    tokens_saved: int | None = None
