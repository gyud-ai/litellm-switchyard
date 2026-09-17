"""Composition root and CLI for the single-process gateway."""

import argparse
import importlib.util
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from .adapters.config import load_config
from .adapters.headroom import HeadroomCompressor
from .adapters.httpx import HttpxTransport
from .adapters.ingress import create_app
from .adapters.logging import JsonEvents, silence_dependency_logs
from .adapters.switchyard import SwitchyardRouter
from .application import Gateway
from .domain import GatewayError, Settings


def build_app(settings: Settings) -> FastAPI:
    """Wire concrete adapters; all resource lifetimes belong to this composition root."""
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
        follow_redirects=False,
        trust_env=False,
    )
    compressor = HeadroomCompressor(settings.compression_workers)
    events = JsonEvents()
    gateway = Gateway(
        settings, SwitchyardRouter(settings.stage), compressor, HttpxTransport(client), events
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.emit(
            {"event": "startup", "models": len(settings.models), "pairs": len(settings.pairs)}
        )
        try:
            yield
        finally:
            await client.aclose()
            await compressor.close()

    return create_app(gateway, lifespan)


def main() -> None:
    """Load JSONC configuration and start one Uvicorn worker."""
    parser = argparse.ArgumentParser(description="Switchyard + Headroom gateway")
    parser.add_argument("--config", type=Path, default=Path("config.jsonc"))
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    args = parser.parse_args()
    silence_dependency_logs()
    os.environ["HEADROOM_BEACON"] = "off"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HEADROOM_TELEMETRY"] = "off"
    try:
        if importlib.util.find_spec("litellm") is not None:
            raise GatewayError("litellm_must_not_be_installed", 500)
        settings = load_config(args.config)
    except GatewayError as error:
        JsonEvents().emit({"event": "startup_failed", "error": error.code})
        raise SystemExit(1) from None
    if args.check:
        JsonEvents().emit({"event": "configuration_valid"})
        return
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        log_config=None,
    )
