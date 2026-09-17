"""Headroom structural compression with serialized access to its shared pipeline."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor

from headroom import CompressConfig, compress

from ..domain import CompressionResult, Model, Payload


class HeadroomCompressor:
    """Isolate synchronous Headroom calls, SDK types, and fail-open results."""

    def __init__(self, workers: int = 2) -> None:
        # Headroom caches a process-global mutable pipeline. One execution thread avoids
        # cross-request state races; the semaphore bounds running plus queued submissions.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compression")
        self._slots = asyncio.Semaphore(workers)

    @staticmethod
    def _compress(messages: list[Payload], model: Model) -> CompressionResult:
        original = copy.deepcopy(messages)
        try:
            result = compress(
                copy.deepcopy(messages),
                model=model.model_id,
                model_limit=model.context_window,
                config=CompressConfig(
                    kompress_model="disabled",
                    compress_system_messages=False,
                    compress_user_messages=True,
                    protect_recent=0,
                ),
            )
            if not result.tokens_before:
                return CompressionResult(original, "failed_unknown")
            if result.tokens_after > result.tokens_before:
                return CompressionResult(original, "failed_unknown")
            return CompressionResult(
                copy.deepcopy(result.messages),
                "savings" if result.tokens_saved else "no_savings",
                result.tokens_before,
                result.tokens_after,
                result.tokens_saved,
            )
        except Exception:
            return CompressionResult(original, "failed_unknown")

    async def compress(self, messages: list[Payload], model: Model) -> CompressionResult:
        """Compress a private copy without blocking the HTTP event loop."""
        await self._slots.acquire()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, self._compress, copy.deepcopy(messages), model
            )
        except BaseException:
            self._slots.release()
            raise
        # Cancellation does not stop a running thread; release capacity only when it exits.
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.shield(future)

    async def close(self) -> None:
        """Wait for remaining compression work during orderly shutdown."""
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
