"""JSON stdout events with scoped dependency-log silencing to prevent payload leakage."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

from ..domain import Payload


class JsonEvents:
    """Serialize gateway-owned records; no raw exception or request serialization."""

    def __init__(self, output: TextIO | None = None) -> None:
        self.output = output or sys.stdout

    def emit(self, event: Payload) -> None:
        """Write one JSON record with a UTC timestamp."""
        record = {"timestamp": datetime.now(UTC).isoformat(), **event}
        self.output.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        self.output.flush()


class DependencyLogSilencer:
    """Silence dependency logging and restore the level captured on first activation.

    The level is captured once. Re-entering an active silencer, including
    nesting ``with`` blocks on the same handle, keeps silencing; the captured
    level is restored exactly when the outermost block exits. Each silencer
    belongs to the caller that created it, and handles from separate callers
    must be released in reverse entry order, so overlapping scopes cannot
    strand logging at ``CRITICAL``.
    """

    def __init__(self) -> None:
        self._previous: int | None = None
        self._depth = 0

    def __enter__(self) -> DependencyLogSilencer:
        """Silence dependency logs and count this scope."""
        self._activate()
        self._depth += 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Restore the captured level when the outermost scope exits."""
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth:
            return
        previous, self._previous = self._previous, None
        if previous is not None:
            logging.disable(previous)

    def _activate(self) -> None:
        if self._previous is None:
            self._previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)


def silence_dependency_logs() -> DependencyLogSilencer:
    """Route observability through gateway events, never dependency exception strings.

    Calling this bare silences dependency logs for the process lifetime, which is
    what the CLI wants. ``with silence_dependency_logs():`` restores the previous
    ``logging.root.manager.disable`` level on exit so in-process callers cannot
    leak the suppression into their host process.
    """
    silencer = DependencyLogSilencer()
    silencer._activate()
    return silencer
