"""Structured planning result models and the bounded planning tool vocabulary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..core.models import WorkflowDefinition

ALLOWED_PLANNING_TOOLS = frozenset({"transform"})


class PlanningMetadata(BaseModel):
    """Non-sensitive metadata about how a workflow was planned."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    fallback_reason: Literal[
        "missing_api_key",
        "timeout",
        "unavailable",
        "rate_limit",
        "api_error",
        "invalid_response",
        "forced",
    ] | None = None


class PlanningResult(BaseModel):
    """Validated workflow and its planning provider metadata."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["gemini", "fallback"]
    workflow: WorkflowDefinition
    metadata: PlanningMetadata
