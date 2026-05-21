"""Structured logging configuration for lark-agent-bridge.

Provides hierarchical loggers under the ``bridge`` namespace so that each
subsystem can be independently configured::

    bridge.app      – main BridgeApp orchestration
    bridge.agents   – agent runners (bug, claude, omlx, …)
    bridge.server   – HTTP report server
    bridge.state    – persistence stores
    bridge.download – log/file downloader

Default behaviour:
  * ``INFO`` level to *stderr* in a compact human-readable format.
  * ``--verbose`` / env ``BRIDGE_LOG_LEVEL=DEBUG`` for debug output.
  * Optionally ``BRIDGE_LOG_JSON=1`` for machine-readable JSON lines.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

_ROOT_LOGGER_NAME = "bridge"
_configured = False


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record for structured log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _CompactFormatter(logging.Formatter):
    """Human-friendly single-line format for terminal output."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        # Strip the root prefix for brevity: "bridge.app" → "app"
        short_name = record.name
        if short_name.startswith(_ROOT_LOGGER_NAME + "."):
            short_name = short_name[len(_ROOT_LOGGER_NAME) + 1 :]
        base = f"{ts} [{record.levelname[0]}] {short_name}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(*, level: str | None = None, json_output: bool | None = None) -> None:
    """Configure the ``bridge`` logger hierarchy.

    Parameters
    ----------
    level:
        Log level name (``DEBUG``, ``INFO``, …).  Defaults to the
        ``BRIDGE_LOG_LEVEL`` env var, then ``INFO``.
    json_output:
        If *True*, emit JSON lines.  Defaults to
        ``BRIDGE_LOG_JSON=1`` env var.
    """
    global _configured
    if _configured:
        return

    resolved_level = (level or os.environ.get("BRIDGE_LOG_LEVEL") or "INFO").upper()
    use_json = json_output if json_output is not None else os.environ.get("BRIDGE_LOG_JSON") == "1"

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    root.setLevel(resolved_level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if use_json else _CompactFormatter())
    root.addHandler(handler)

    # Prevent propagation to the Python root logger
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``bridge`` namespace.

    Usage::

        from .log import get_logger
        logger = get_logger("app")  # → logging.getLogger("bridge.app")
    """
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
