"""JSON stdout events with dependency logs disabled to prevent payload leakage."""

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


def silence_dependency_logs() -> None:
    """Route observability through gateway events, never dependency exception strings."""
    logging.disable(logging.CRITICAL)
