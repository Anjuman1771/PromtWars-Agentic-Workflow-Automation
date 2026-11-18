"""FlowPilot AI Domain Exception Hierarchy.

Provides structured, specific exceptions for security, approval, workflow,
and execution failures.
"""

from typing import Any, Dict, Optional


class FlowPilotException(Exception):
    """Base exception for all FlowPilot AI domain errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


# ------------------------------------------------------------------------------
# Security Exceptions
# ------------------------------------------------------------------------------

class SecurityException(FlowPilotException):
    """Base exception for security boundary violations."""
    pass


class SSRFSecurityViolation(SecurityException):
    """Raised when an HTTP request attempts to access a private, loopback, or reserved IP."""
    pass


class PathTraversalViolation(SecurityException):
    """Raised when a file operation attempts to escape the designated workspace boundary."""
    pass


class RestrictedExecutionViolation(SecurityException):
    """Raised when an unauthorized command, shell call, or arbitrary code execution is attempted."""
    pass


# ------------------------------------------------------------------------------
# Human-in-the-Loop (HITL) & Approval Exceptions
# ------------------------------------------------------------------------------

class ApprovalException(FlowPilotException):
    """Base exception for approval token operations."""
    pass


class ApprovalNotFoundException(ApprovalException):
    """Raised when an approval token cannot be found."""
    pass


class ApprovalExpiredException(ApprovalException):
    """Raised when an approval token has passed its expiration timestamp."""
    pass


class ApprovalConsumedException(ApprovalException):
    """Raised when an approval token has already been consumed (single-use enforcement)."""
    pass


class ApprovalTamperingException(ApprovalException):
    """Raised when an approval payload hash does not match the original action payload."""
    pass


# ------------------------------------------------------------------------------
# Workflow & DAG Specification Exceptions
# ------------------------------------------------------------------------------

class WorkflowException(FlowPilotException):
    """Base exception for workflow validation and structure errors."""
    pass


class CyclicDependencyError(WorkflowException):
    """Raised when a circular dependency is detected in the task DAG."""
    pass


class InvalidWorkflowSchemaError(WorkflowException):
    """Raised when a generated or provided workflow definition fails schema validation."""
    pass


class VariableInterpolationError(WorkflowException):
    """Raised when a variable reference (e.g., {{tasks.a.output}}) cannot be resolved."""
    pass


# ------------------------------------------------------------------------------
# Runtime & Execution Exceptions
# ------------------------------------------------------------------------------

class ExecutionException(FlowPilotException):
    """Base exception for workflow runtime execution failures."""
    pass


class ExecutionLimitExceededException(ExecutionException):
    """Raised when hard resource bounds (max steps, total runtime) are exceeded."""
    pass


class TaskTimeoutException(ExecutionException):
    """Raised when a task exceeds its allocated execution timeout."""
    pass


class CircuitBreakerOpenException(ExecutionException):
    """Raised when execution is halted because consecutive failures tripped the circuit breaker."""
    pass


class TaskExecutionError(ExecutionException):
    """Raised when a task fails execution after exhaustion of its retry policy."""
    pass


class ToolNotFoundError(ExecutionException):
    """Raised when an unknown tool name is referenced in a task."""
    pass


class ToolValidationError(ExecutionException):
    """Raised when tool arguments fail Pydantic input schema validation."""
    pass


class DuplicateExecutionError(ExecutionException):
    """Raised when an idempotent task is executed multiple times unexpectedly."""
    pass


class RecoveryError(ExecutionException):
    """Raised when the crash recovery process fails to restore interrupted workflows."""
    pass
