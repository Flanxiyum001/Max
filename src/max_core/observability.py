"""Structured access logs with a fixed allowlist of fields."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class SafeJsonFormatter(logging.Formatter):
    """Never format arbitrary record messages, headers, URLs or exceptions."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", "internal")
        if event not in {"http_request", "http_failure"}:
            event = "internal"
        payload: dict[str, str | int] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": event,
        }
        for key in ("request_id", "method", "route", "status_code"):
            value = getattr(record, key, None)
            if isinstance(value, (str, int)):
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_access_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("max_core.access")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(SafeJsonFormatter())
        logger.addHandler(handler)
    return logger
