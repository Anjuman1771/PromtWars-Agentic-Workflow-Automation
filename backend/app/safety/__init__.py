"""FlowPilot AI Safety and Security Package."""

from importlib import import_module
from typing import Any

from .circuit_breaker import CircuitBreaker, CircuitBreakerState
from .containment import validate_workspace_path
from .exceptions import (
    ApprovalConsumedException,
    ApprovalException,
    ApprovalExpiredException,
    ApprovalNotFoundException,
    ApprovalTamperingException,
    CircuitBreakerOpenException,
    CyclicDependencyError,
    DuplicateExecutionError,
    ExecutionException,
    ExecutionLimitExceededException,
    FlowPilotException,
    InvalidWorkflowSchemaError,
    PathTraversalViolation,
    RecoveryError,
    RestrictedExecutionViolation,
    SecurityException,
    SSRFSecurityViolation,
    TaskExecutionError,
    TaskTimeoutException,
    TransientToolExecutionError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
    VariableInterpolationError,
    WorkflowException,
)
from .limits import ExecutionLimits, execution_limits
from .redaction import redact_object, redact_text

__all__ = [
    # Approval
    "ApprovalTokenManager",
    "approval_manager",
    "hash_payload",
    "hash_token",
    # Circuit Breaker
    "CircuitBreaker",
    "CircuitBreakerState",
    # Containment
    "validate_workspace_path",
    # Limits
    "ExecutionLimits",
    "execution_limits",
    # Redaction
    "redact_object",
    "redact_text",
    # Exceptions
    "FlowPilotException",
    "SecurityException",
    "SSRFSecurityViolation",
    "PathTraversalViolation",
    "RestrictedExecutionViolation",
    "ApprovalException",
    "ApprovalNotFoundException",
    "ApprovalExpiredException",
    "ApprovalConsumedException",
    "ApprovalTamperingException",
    "WorkflowException",
    "CyclicDependencyError",
    "InvalidWorkflowSchemaError",
    "VariableInterpolationError",
    "ExecutionException",
    "ExecutionLimitExceededException",
    "TaskTimeoutException",
    "ToolExecutionError",
    "TransientToolExecutionError",
    "CircuitBreakerOpenException",
    "TaskExecutionError",
    "ToolNotFoundError",
    "ToolValidationError",
    "DuplicateExecutionError",
    "RecoveryError",
]


def __getattr__(name: str) -> Any:
    if name in {"ApprovalTokenManager", "approval_manager", "hash_payload", "hash_token"}:
        approval_module = import_module(".approval", __name__)
        value = getattr(approval_module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
