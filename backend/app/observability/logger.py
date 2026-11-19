"""FlowPilot AI Structured Logging Module.

Emits structured JSON logs with automatic secret redaction.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class RedactingJSONFormatter(logging.Formatter):
    """Custom logging formatter that outputs JSON and scrubs secrets."""

    def format(self, record: logging.LogRecord) -> str:
        from ..safety.redaction import redact_object, redact_text

        # Standard structured log record fields
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        raw_message = record.getMessage()
        
        # Redact the log message itself
        sanitized_message = redact_text(raw_message)

        log_payload: Dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": sanitized_message,
        }

        # Include custom execution context if provided in extra
        if hasattr(record, "run_id") and record.run_id:
            log_payload["run_id"] = str(record.run_id)
        if hasattr(record, "task_id") and record.task_id:
            log_payload["task_id"] = str(record.task_id)
        if hasattr(record, "event_type") and record.event_type:
            log_payload["event_type"] = str(record.event_type)

        # Include and sanitize additional structured extra data if present
        if hasattr(record, "data") and isinstance(record.data, (dict, list)):
            log_payload["data"] = redact_object(record.data)

        # Handle exception tracebacks if present
        if record.exc_info:
            log_payload["exception"] = redact_text(self.formatException(record.exc_info))

        return json.dumps(redact_object(log_payload))


def get_logger(name: str = "flowpilot", level: int = logging.INFO) -> logging.Logger:
    """Create or retrieve a structured, redacting logger."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if already configured
    if not any(isinstance(h.formatter, RedactingJSONFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(RedactingJSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False

    return logger
