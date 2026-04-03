"""
Structured logging configuration for the executor.

JSON mode activates when ALPHA_ENGINE_JSON_LOGS=1 (set on EC2 via systemd env).
Text mode (default) preserves the current human-readable format for local dev.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "func": record.funcName,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exc"] = self.formatException(record.exc_info)
        # Merge extra context if provided via logger.info("msg", extra={"ctx": {...}})
        if hasattr(record, "ctx"):
            log_entry["ctx"] = record.ctx
        return json.dumps(log_entry, default=str)


def setup_logging(name: str = "executor") -> None:
    """
    Configure root logger.

    JSON mode: ALPHA_ENGINE_JSON_LOGS=1 (for EC2 / production)
    Text mode: default (for local dev / dry-run)
    """
    json_mode = os.environ.get("ALPHA_ENGINE_JSON_LOGS", "0") == "1"

    handler = logging.StreamHandler()
    if json_mode:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s %(levelname)s [{name}] %(message)s"
        ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
