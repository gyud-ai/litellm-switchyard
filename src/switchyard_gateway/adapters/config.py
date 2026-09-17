"""JSONC parsing and validation, including explicit environment references."""

import os
from pathlib import Path
from typing import Annotated, Any

import json5
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..domain import Endpoint, GatewayError, Model, Pair, Settings, StagePolicy

Label = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StrictConfig(BaseModel):
    """Reject misspelled configuration keys rather than silently ignoring them."""

    model_config = ConfigDict(extra="forbid", strict=True)


class EndpointConfig(StrictConfig):
    """Connection settings for one named replica."""

    name: Label
    base_url: str
    api_key: str = ""

    @model_validator(mode="after")
    def validate_url(self) -> EndpointConfig:
        """Accept only absolute HTTP URLs without embedded credentials or query strings."""
        from urllib.parse import urlsplit

        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid endpoint URL")
        return self


class ModelConfig(StrictConfig):
    """Public model label, backend ID, and equivalent replicas."""

    model_id: str = Field(min_length=1)
    endpoints: list[EndpointConfig] = Field(min_length=1)
    context_window: int = Field(default=131072, gt=0)
    defaults: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_endpoints(self) -> ModelConfig:
        """Reject duplicate replica names and gateway-owned request defaults."""
        names = [ep.name for ep in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("duplicate endpoint names")
        if {"model", "messages", "stream"} & self.defaults.keys():
            raise ValueError("invalid model defaults")
        return self


class PairConfig(StrictConfig):
    """Explicit references to model labels."""

    capable: Label
    efficient: Label


class StageConfig(StrictConfig):
    """Supported stage policy, independent of Switchyard SDK classes."""

    picker: str = Field(default="efficient_first", pattern=r"^(efficient_first|capable_first)$")
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    recent_window: int = Field(default=3, gt=0)
    only_on_wrong_signal_escalation: bool = True
    escalation_note: str = "The efficient tier failed; continue from its work."
    deescalation_note: str = "The capable tier completed the recovery."
    capable_system_prompt: str = "Handle this request as the capable tier."
    efficient_system_prompt: str = "Handle this request as the efficient tier."


class CompressionConfig(StrictConfig):
    """Structural compression controls; ML compression is intentionally unavailable."""

    enabled: bool = True
    workers: int = Field(default=2, ge=1, le=32)


class ServerConfig(StrictConfig):
    """Listener and authentication settings."""

    api_key: str = Field(min_length=1)
    host: str = "127.0.0.1"
    port: int = Field(default=4000, ge=1, le=65535)
    max_request_bytes: int = Field(default=16 * 1024 * 1024, gt=0)


class Config(StrictConfig):
    """Validated JSONC document."""

    server: ServerConfig
    models: dict[Label, ModelConfig] = Field(min_length=1)
    pairs: dict[Label, PairConfig] = Field(default_factory=dict)
    stage: StageConfig = Field(default_factory=StageConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    cooldown_seconds: Positive = 30.0
    connect_timeout: Positive = 5.0
    read_timeout: Positive = 300.0
    forward_headers: list[str] = Field(
        default_factory=lambda: ["x-session-id", "x-opencode-session"]
    )

    @model_validator(mode="after")
    def validate_references(self) -> Config:
        """Resolve pair references and prohibit forwarding credential/transport headers."""
        if self.models.keys() & self.pairs.keys():
            raise ValueError("model and pair labels collide")
        for pair in self.pairs.values():
            if pair.capable == pair.efficient:
                raise ValueError("pair tiers must differ")
            if pair.capable not in self.models or pair.efficient not in self.models:
                raise ValueError("unknown pair model")
        forbidden = {
            "authorization",
            "proxy-authorization",
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "cookie",
            "content-type",
            "accept-encoding",
            "x-headroom-bypass",
        }
        self.forward_headers = [header.lower() for header in self.forward_headers]
        if any(
            not header.startswith("x-") or header in forbidden for header in self.forward_headers
        ):
            raise ValueError("only application x- headers may be forwarded")
        return self

    def settings(self) -> Settings:
        """Construct framework-free application settings."""
        return Settings(
            models={
                name: Model(
                    name,
                    model.model_id,
                    tuple(Endpoint(**ep.model_dump()) for ep in model.endpoints),
                    model.context_window,
                    model.defaults,
                )
                for name, model in self.models.items()
            },
            pairs={
                name: Pair(name, pair.capable, pair.efficient) for name, pair in self.pairs.items()
            },
            api_key=self.server.api_key,
            stage=StagePolicy(**self.stage.model_dump()),
            compression=self.compression.enabled,
            compression_workers=self.compression.workers,
            cooldown_seconds=self.cooldown_seconds,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            forward_headers=tuple(self.forward_headers),
            host=self.server.host,
            port=self.server.port,
            max_request_bytes=self.server.max_request_bytes,
        )


def _resolve(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"env"}:
            name = value["env"]
            if not isinstance(name, str) or not os.environ.get(name):
                raise GatewayError("missing_configuration_environment", 500)
            return os.environ[name]
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def load_config(path: Path) -> Settings:
    """Load a JSONC file without including private values in errors."""
    try:
        parsed = json5.loads(path.read_text(), allow_duplicate_keys=False)
        return Config.model_validate(_resolve(parsed)).settings()
    except GatewayError:
        raise
    except OSError, ValueError, TypeError, ValidationError:
        raise GatewayError("invalid_configuration", 500) from None
