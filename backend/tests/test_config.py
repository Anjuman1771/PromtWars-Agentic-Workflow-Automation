"""Unit tests for FlowPilot AI Configuration."""

from pathlib import Path
import secrets
import pytest
from backend.app.config import Settings


def test_default_settings():
    """Verify default configuration parameters are secure and sensible."""
    cfg = Settings()
    assert cfg.env == "development"
    assert cfg.debug is False
    assert cfg.port == 8000
    assert cfg.max_workflow_steps == 30
    assert cfg.max_run_time_seconds == 300
    assert cfg.max_concurrent_tasks == 5
    assert cfg.max_retries_per_task == 3
    assert cfg.circuit_breaker_failure_threshold == 3
    assert cfg.circuit_breaker_recovery_timeout_seconds == 30.0
    assert cfg.circuit_breaker_half_open_max_trials == 1
    assert cfg.approval_ttl_seconds == 600
    assert cfg.is_mock_ai is True
    assert isinstance(cfg.workspace_root, Path)
    assert cfg.workspace_root.is_absolute()


def test_mock_ai_property(monkeypatch):
    """Verify is_mock_ai flag reflects presence/absence of GEMINI_API_KEY."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg1 = Settings()
    assert cfg1.is_mock_ai is True

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyFakeKeyForTesting1234567890123")
    cfg2 = Settings()
    assert cfg2.is_mock_ai is False
    assert cfg2.gemini_api_key == "AIzaSyFakeKeyForTesting1234567890123"

    # Empty string should still be considered mock AI
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    cfg3 = Settings()
    assert cfg3.is_mock_ai is True


def test_custom_environment_overrides(monkeypatch):
    """Verify environment variable overrides using FLOWPILOT_ prefix."""
    monkeypatch.setenv("FLOWPILOT_PORT", "9999")
    monkeypatch.setenv("FLOWPILOT_DEBUG", "true")
    monkeypatch.setenv("FLOWPILOT_MAX_WORKFLOW_STEPS", "50")

    cfg = Settings()
    assert cfg.port == 9999
    assert cfg.debug is True
    assert cfg.max_workflow_steps == 50


def test_limits_validation():
    """Verify boundaries are enforced on configuration values."""
    with pytest.raises(Exception):
        Settings(max_workflow_steps=0)  # Must be >= 1

    with pytest.raises(Exception):
        Settings(approval_ttl_seconds=10)  # Must be >= 60 seconds


def test_development_secret_default_is_allowed():
    cfg = Settings(
        _env_file=None,
        env="development",
        secret_key="flowpilot-insecure-secret-key-must-change-in-prod",
    )
    assert cfg.secret_key == "flowpilot-insecure-secret-key-must-change-in-prod"


@pytest.mark.parametrize(
    "secret",
    [
        "flowpilot-insecure-secret-key-must-change-in-prod",
        "change-this-in-production-to-a-secure-random-string",
        "short",
        "x" * 64,
    ],
)
def test_production_rejects_missing_or_weak_secret(secret):
    with pytest.raises(Exception) as exc_info:
        Settings(_env_file=None, env="production", secret_key=secret)
    assert secret not in str(exc_info.value)
    assert "FLOWPILOT_SECRET_KEY" in str(exc_info.value)


def test_production_rejects_missing_secret(monkeypatch):
    monkeypatch.delenv("FLOWPILOT_SECRET_KEY", raising=False)
    with pytest.raises(Exception) as exc_info:
        Settings(_env_file=None, env="production")
    assert "FLOWPILOT_SECRET_KEY" in str(exc_info.value)
    assert "flowpilot-insecure-secret-key-must-change-in-prod" not in str(exc_info.value)


def test_production_accepts_strong_secret():
    secret = secrets.token_urlsafe(32)
    cfg = Settings(_env_file=None, env="production", secret_key=secret)
    assert cfg.secret_key == secret
