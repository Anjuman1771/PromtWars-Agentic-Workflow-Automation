"""Unit tests for FlowPilot AI Secret Redaction Engine."""

import json
from backend.app.safety.redaction import redact_object, redact_text, REDACTED_SUBSTITUTE


def test_redact_google_api_key():
    """Verify Google API keys are scrubbed."""
    fake_key = "AIzaSyD-1234567890abcdefghijklmnopqrstuv"
    raw = f"Error communicating with Gemini: invalid key {fake_key} received."
    sanitized = redact_text(raw)
    assert fake_key not in sanitized
    assert REDACTED_SUBSTITUTE in sanitized


def test_redact_jwt_token():
    """Verify standard JWT tokens are scrubbed."""
    fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    raw = f"User session active: {fake_jwt}"
    sanitized = redact_text(raw)
    assert fake_jwt not in sanitized
    assert REDACTED_SUBSTITUTE in sanitized


def test_redact_bearer_token():
    """Verify Bearer tokens are scrubbed."""
    raw = "Request header: Bearer abc123def456_mySecretToken=="
    sanitized = redact_text(raw)
    assert "abc123def456_mySecretToken==" not in sanitized
    assert f"Bearer {REDACTED_SUBSTITUTE}" in sanitized


def test_redact_aws_credentials():
    """Verify AWS access key and secret access key are scrubbed."""
    raw = "AWS config: AKIAIOSFODNN7EXAMPLE and aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'"
    sanitized = redact_text(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in sanitized
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in sanitized
    assert REDACTED_SUBSTITUTE in sanitized


def test_redact_password_key_value():
    """Verify password patterns in strings are scrubbed."""
    raw1 = 'Database connection failed: password="super_secret_password_123"'
    sanitized1 = redact_text(raw1)
    assert "super_secret_password_123" not in sanitized1
    assert REDACTED_SUBSTITUTE in sanitized1

    raw2 = "login params: passwd: myPassword999, user: admin"
    sanitized2 = redact_text(raw2)
    assert "myPassword999" not in sanitized2
    assert "admin" in sanitized2  # Safe non-secret is preserved


def test_redact_authorization_header():
    """Verify basic authorization headers are scrubbed."""
    raw = "Headers: {'Authorization': 'Basic YWxhZGRpbjpvcGVuc2VzYW1l'}"
    sanitized = redact_text(raw)
    assert "YWxhZGRpbjpvcGVuc2VzYW1l" not in sanitized
    assert REDACTED_SUBSTITUTE in sanitized


def test_redact_private_key():
    """Verify PEM private keys are completely scrubbed."""
    raw = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA04q6...FAKE_KEY_DATA...
-----END RSA PRIVATE KEY-----"""
    sanitized = redact_text(raw)
    assert "FAKE_KEY_DATA" not in sanitized
    assert REDACTED_SUBSTITUTE in sanitized


def test_redact_nested_dict():
    """Verify recursive redaction over nested dictionaries and sensitive keys."""
    payload = {
        "user": "alice",
        "api_key": "raw_secret_key_value",
        "nested": {
            "token": "token_value",
            "safe_field": "visible_data",
            "password_hash": "hash_secret",
            "list_items": [
                "Bearer my_super_token",
                {"deep_secret": "sensitive_data"},
                "safe string",
            ],
        },
    }
    sanitized = redact_object(payload)

    assert sanitized["user"] == "alice"
    assert sanitized["api_key"] == REDACTED_SUBSTITUTE
    assert sanitized["nested"]["token"] == REDACTED_SUBSTITUTE
    assert sanitized["nested"]["safe_field"] == "visible_data"
    assert sanitized["nested"]["password_hash"] == REDACTED_SUBSTITUTE
    assert sanitized["nested"]["list_items"][0] == f"Bearer {REDACTED_SUBSTITUTE}"
    assert sanitized["nested"]["list_items"][1]["deep_secret"] == REDACTED_SUBSTITUTE
    assert sanitized["nested"]["list_items"][2] == "safe string"


def test_non_sensitive_text_preserved():
    """Verify standard diagnostic text is untouched."""
    text = "Task 'fetch_summary' completed successfully in 240ms with 4 items."
    assert redact_text(text) == text


def test_redaction_preserves_json_text_structure():
    raw = '{"api_key":"private-value-123","safe":"ordinary prompt text"}'
    sanitized = redact_text(raw)
    parsed = json.loads(sanitized)

    assert parsed == {
        "api_key": REDACTED_SUBSTITUTE,
        "safe": "ordinary prompt text",
    }
