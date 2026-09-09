"""Structured (JSON) logging setup.

Ported from the org's fastapi-boilerplate pattern and adapted to emit real
log records instead of `print()`. Every request gets a `request_id` (see
`core.middleware.RequestLoggingMiddleware`) that downstream pipeline code
should include via the `extra={"request_id": ...}` kwarg on log calls, so a
single Teams message's full trail (extraction -> resolution -> card
creation) can be reconstructed from log output alone.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Renders each log record as a single-line JSON object.

    Includes standard fields (timestamp, level, logger name, message) plus
    any extra fields passed via `logging.Logger.*(..., extra={...})` (e.g.
    `request_id`, `workflow_id`). Exception info, if present, is rendered as
    an `exception` string field.
    """

    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the given log record to a single-line JSON string."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger to emit structured JSON to stdout.

    Call once at process startup (see `src/bot.py`). Idempotent: safe to
    call more than once, since it replaces rather than appends handlers.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
