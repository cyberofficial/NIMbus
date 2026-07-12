"""Loguru-based structured logging configuration.

Each server run writes to a new timestamped log file (server.YYYY-MM-DD_HH-MM-SS_XXXXXX.log).
No rotation during runtime - one log file per session.
Stdlib logging is intercepted and funneled to loguru.
Context vars (request_id, node_id, chat_id) from contextualize() are
included at top level for easy grep/filter.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

_configured = False

# Context keys we promote to top-level JSON for traceability
_CONTEXT_KEYS = ("request_id", "node_id", "chat_id")


def _serialize_with_context(record) -> str:
    """Format record as JSON with context vars at top level.
    Returns a format template; we inject _json into record for output.
    """
    extra = record.get("extra", {})
    out = {
        "time": str(record["time"]),
        "level": record["level"].name,
        "message": record["message"],
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
    }
    for key in _CONTEXT_KEYS:
        if key in extra and extra[key] is not None:
            out[key] = extra[key]
    record["_json"] = json.dumps(out, default=str)
    return "{_json}\n"


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _generate_log_filename() -> str:
    """Generate a timestamped log filename for this session."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    return f"server.{timestamp}.log"


def configure_logging(log_file: str | None = None, *, force: bool = False) -> str:
    """Configure loguru with JSON output to a timestamped log file and intercept stdlib logging.

    Idempotent: skips if already configured (e.g. hot reload).
    Use force=True to reconfigure (e.g. in tests with a different log path).

    Args:
        log_file: Ignored; kept for backward compatibility. A new timestamped
                  filename is generated on each call.
        force: Force reconfiguration.

    Returns:
        The actual log file path being used.
    """
    global _configured
    if _configured and not force:
        return log_file or ""

    _configured = True

    # Generate timestamped log filename for this session
    actual_log_file = _generate_log_filename()

    # Remove default loguru handler (writes to stderr)
    logger.remove()

    # Add console sink: human-readable colored output at INFO+ level so
    # connection failures and retries are visible in real-time.
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # Add file sink: JSON lines, DEBUG level, context vars at top level
    # No rotation - one log file per server session
    logger.add(
        actual_log_file,
        level="DEBUG",
        format=_serialize_with_context,
        encoding="utf-8",
        mode="w",  # overwrite on fresh start
        retention="7 days",
    )

    # Intercept stdlib logging: route all root logger output to loguru
    intercept = InterceptHandler()
    logging.root.handlers = [intercept]
    logging.root.setLevel(logging.DEBUG)

    return actual_log_file
