"""FlowPilot AI Configuration Module.

Uses Pydantic Settings for type-safe environment configuration with sensible defaults.
"""

from pathlib import Path
from typing import List, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable overrides."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="FLOWPILOT_",
        hide_input_in_errors=True,
    )

    # Application Environment
    env: str = Field(default="development", description="Environment: development, test, production")
    debug: bool = Field(default=False, description="Enable debug logging and stacktraces")
    host: str = Field(default="127.0.0.1", description="API host address")
    port: int = Field(default=8000, description="API port")

    # Security & CORS
    cors_origins: List[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="Allowed CORS origins",
    )
    secret_key: str = Field(
        default="flowpilot-insecure-secret-key-must-change-in-prod",
        description="Development-only default; production requires a strong FLOWPILOT_SECRET_KEY.",
        repr=False,
    )

    # AI Configuration (supports direct GEMINI_API_KEY from environment)
    gemini_api_key: Optional[str] = Field(
        default=None,
        validation_alias="GEMINI_API_KEY",
        description="Google Gemini API Key. If empty, platform runs in offline mock mode.",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        validation_alias="GEMINI_MODEL",
        description="Gemini model identifier",
    )

    # Execution & Safety Limits
    max_workflow_steps: int = Field(default=30, ge=1, le=200, description="Max allowed tasks per workflow DAG")
    max_run_time_seconds: int = Field(default=300, ge=10, le=3600, description="Max total workflow execution duration")
    default_task_timeout_seconds: int = Field(default=30, ge=1, le=300, description="Default timeout per task")
    max_concurrent_tasks: int = Field(default=5, ge=1, le=50, description="Max concurrent tasks dispatched in parallel")
    max_retries_per_task: int = Field(default=3, ge=0, le=10, description="Max retry attempts per task")
    max_response_size_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, description="Max allowed response size for HTTP and tool outputs (2 MB)"
    )
    max_payload_size_bytes: int = Field(
        default=1024 * 1024, ge=1024, description="Max allowed JSON payload size (1 MB)"
    )
    max_file_size_bytes: int = Field(
        default=1024 * 1024, ge=1, description="Maximum size for an individual tool file operation"
    )
    tool_http_connect_timeout_seconds: float = Field(
        default=5.0, gt=0, le=60, description="Maximum time allowed to connect to a tool HTTP destination"
    )
    tool_http_read_timeout_seconds: float = Field(
        default=10.0, gt=0, le=120, description="Maximum time allowed to read a tool HTTP response"
    )
    tool_http_max_redirects: int = Field(
        default=3, ge=0, le=10, description="Maximum redirects followed by the tool HTTP client"
    )

    # Circuit Breaker Limits
    circuit_breaker_failure_threshold: int = Field(
        default=3, ge=1, le=50, description="Failures before circuit breaker trips to OPEN"
    )
    circuit_breaker_recovery_timeout_seconds: float = Field(
        default=30.0, ge=1.0, le=600.0, description="Recovery cooldown period in seconds before testing HALF_OPEN"
    )
    circuit_breaker_half_open_max_trials: int = Field(
        default=1, ge=1, le=10, description="Max trial requests allowed in HALF_OPEN"
    )

    # Human-in-the-Loop Settings
    approval_ttl_seconds: int = Field(
        default=600, ge=60, le=86400, description="Approval token expiration duration in seconds (10 mins)"
    )

    # Storage Settings
    db_path: str = Field(default="flowpilot.db", description="Path to SQLite database file")

    # Workspace Path Containment
    workspace_root: Path = Field(
        default_factory=lambda: Path.cwd().resolve(),
        description="Root directory boundary for file operations",
    )

    @field_validator("workspace_root", mode="after")
    @classmethod
    def resolve_workspace_root(cls, v: Path) -> Path:
        """Ensure workspace root is resolved to an absolute path."""
        return v.resolve()

    @model_validator(mode="after")
    def validate_production_secret_key(self) -> "Settings":
        """Reject absent, weak, or known development secrets in production."""
        if self.env.strip().lower() not in {"production", "prod"}:
            return self

        secret = self.secret_key.strip()
        known_defaults = {
            "flowpilot-insecure-secret-key-must-change-in-prod",
            "change-this-in-production-to-a-secure-random-string",
            "replace-this-with-a-secure-random-secret",
            "secret",
            "changeme",
            "change-me",
            "your-secret-key",
            "development",
        }
        if (
            len(secret) < 32
            or secret.lower() in known_defaults
            or len(set(secret)) < 8
        ):
            raise ValueError(
                "Production requires a strong FLOWPILOT_SECRET_KEY of at least 32 characters."
            )
        return self

    @property
    def is_mock_ai(self) -> bool:
        """Check if platform is operating in offline mock/heuristic AI mode."""
        return not bool(self.gemini_api_key and self.gemini_api_key.strip())


# Global settings singleton
settings = Settings()
