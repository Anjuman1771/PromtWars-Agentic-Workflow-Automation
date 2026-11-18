"""Unit tests for FlowPilot AI Observability & Logger."""

import io
import json
import logging
from backend.app.observability.logger import RedactingJSONFormatter, get_logger
from backend.app.safety.redaction import REDACTED_SUBSTITUTE


def test_logger_json_format_and_redaction():
    """Verify logger formats output as JSON and scrubs secret tokens."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJSONFormatter())

    test_logger = logging.getLogger("test_redacting_logger")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    fake_api_key = "AIzaSyFakeKey1234567890abcdefghijklmnop"
    test_logger.info(
        f"API call with key {fake_api_key}",
        extra={
            "run_id": "run-xyz-123",
            "task_id": "task-abc-456",
            "data": {"token": "sensitive_session_token", "status": "ok"},
        },
    )

    output = stream.getvalue().strip()
    data = json.loads(output)

    assert data["level"] == "INFO"
    assert data["run_id"] == "run-xyz-123"
    assert data["task_id"] == "task-abc-456"
    assert fake_api_key not in data["message"]
    assert REDACTED_SUBSTITUTE in data["message"]
    assert data["data"]["token"] == REDACTED_SUBSTITUTE
    assert data["data"]["status"] == "ok"


def test_logger_singleton_helper():
    """Verify get_logger returns an initialized logger with RedactingJSONFormatter."""
    logger = get_logger("flowpilot_test_instance")
    assert logger.name == "flowpilot_test_instance"
    assert any(isinstance(h.formatter, RedactingJSONFormatter) for h in logger.handlers)
