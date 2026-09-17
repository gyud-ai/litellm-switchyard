"""Repeatable local preparation benchmark; no backend requests or paid tokens."""

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import AsyncIterator

from switchyard_gateway.adapters.headroom import HeadroomCompressor
from switchyard_gateway.adapters.logging import silence_dependency_logs
from switchyard_gateway.adapters.switchyard import SwitchyardRouter
from switchyard_gateway.application import Gateway
from switchyard_gateway.domain import Endpoint, Model, Pair, Payload, Settings, StagePolicy


class Sink:
    """Discard benchmark events."""

    def emit(self, event: Payload) -> None:
        """Avoid including stdout serialization in preparation measurements."""


class FakeResponse:
    """Immediate fake backend response."""

    status = 200
    headers: dict[str, str] = {}

    async def chunks(self) -> AsyncIterator[bytes]:
        """Produce no generated output."""
        yield b""

    async def close(self) -> None:
        """No resources to close."""


class FakeTransport:
    """Exclude network and inference time from measurements."""

    async def send(
        self, endpoint: Endpoint, request: Payload, headers: dict[str, str]
    ) -> FakeResponse:
        """Return immediately."""
        return FakeResponse()


async def main(iterations: int) -> None:
    """Report warm p50/p95 preparation latency for fixed short and long inputs."""
    silence_dependency_logs()
    models = {
        name: Model(name, "test-model", (Endpoint(name, "http://unused.invalid/v1"),))
        for name in ("cheap", "capable")
    }
    settings = Settings(models, {"pair": Pair("pair", "capable", "cheap")}, "unused")
    compressor = HeadroomCompressor()
    gateway = Gateway(
        settings, SwitchyardRouter(StagePolicy()), compressor, FakeTransport(), Sink()
    )
    rows = [{"id": i, "status": "ok", "region": "west", "count": 1} for i in range(300)]
    short = [{"role": "user", "content": "hello"}]
    long = [
        {"role": "user", "content": "Inspect these results"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call", "type": "function", "function": {"name": "fetch", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": json.dumps(rows)},
        {"role": "assistant", "content": "Results received"},
        {"role": "user", "content": "Summarize the results"},
    ]
    try:
        for name, messages in (("short", short), ("long_tool_history", long)):
            times: list[float] = []
            compression: list[float] = []
            savings = None
            for i in range(iterations + 1):
                start = time.perf_counter()
                exchange = await gateway.open({"model": "pair", "messages": messages}, {}, "bench")
                elapsed = (time.perf_counter() - start) * 1000
                await gateway.finish(exchange, "completed")
                if i:
                    times.append(elapsed)
                    compression.append(exchange.event["compression_ms"])
                    savings = exchange.event.get("tokens_saved")
            ordered = sorted(times)
            print(
                json.dumps(
                    {
                        "scenario": name,
                        "iterations": iterations,
                        "preparation_p50_ms": statistics.median(times),
                        "preparation_p95_ms": ordered[
                            min(len(ordered) - 1, int(0.95 * len(ordered)))
                        ],
                        "compression_p50_ms": statistics.median(compression),
                        "tokens_saved": savings,
                    }
                )
            )
    finally:
        await compressor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    asyncio.run(main(args.iterations))
