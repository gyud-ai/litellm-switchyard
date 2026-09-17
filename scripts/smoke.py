"""Check exact dependencies and real adapter behavior without network inference."""

import asyncio
import importlib.metadata
import importlib.util
import json

from switchyard_gateway.adapters.headroom import HeadroomCompressor
from switchyard_gateway.adapters.logging import silence_dependency_logs
from switchyard_gateway.adapters.switchyard import SwitchyardRouter
from switchyard_gateway.domain import Endpoint, Model, Pair, StagePolicy


async def main() -> None:
    """Fail if dependencies drift, LiteLLM appears, or either adapter stops working."""
    with silence_dependency_logs():
        assert importlib.util.find_spec("litellm") is None
        assert importlib.metadata.version("nemo-switchyard") == "0.2.0"
        assert importlib.metadata.version("headroom-ai") == "0.37.0"
        result = await SwitchyardRouter(StagePolicy()).route(
            {"model": "pair", "messages": [{"role": "user", "content": "hello"}]},
            Pair("pair", "capable", "efficient"),
        )
        assert result.tier == "efficient"
        model = Model("efficient", "test-model", (Endpoint("unused", "http://unused.invalid/v1"),))
        compressor = HeadroomCompressor()
        try:
            compression = await compressor.compress(
                [
                    {
                        "role": "user",
                        "content": json.dumps(
                            [
                                {"id": i, "status": "ok", "region": "west", "count": 1}
                                for i in range(300)
                            ]
                        ),
                    }
                ],
                model,
            )
            assert compression.outcome == "savings"
            assert compression.tokens_saved is not None and compression.tokens_saved > 0
        finally:
            await compressor.close()
    print(json.dumps({"smoke": "passed", "litellm_installed": False}))


if __name__ == "__main__":
    asyncio.run(main())
