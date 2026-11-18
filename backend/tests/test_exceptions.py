"""Unit tests for FlowPilot AI Domain Exception Hierarchy."""

import pytest
from backend.app.safety.exceptions import (
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
    ToolNotFoundError,
    ToolValidationError,
    VariableInterpolationError,
    WorkflowException,
)


def test_base_exception_structure():
    """Verify base FlowPilotException captures message and details."""
    exc = FlowPilotException("Operation failed", details={"run_id": "r1", "code": 500})
    assert str(exc) == "Operation failed"
    assert exc.message == "Operation failed"
    assert exc.details == {"run_id": "r1", "code": 500}


def test_security_hierarchy():
    """Verify security violations inherit from SecurityException and FlowPilotException."""
    for cls in (SSRFSecurityViolation, PathTraversalViolation, RestrictedExecutionViolation):
        err = cls("Security boundary violated")
        assert isinstance(err, SecurityException)
        assert isinstance(err, FlowPilotException)


def test_approval_hierarchy():
    """Verify approval violations inherit from ApprovalException."""
    for cls in (
        ApprovalNotFoundException,
        ApprovalExpiredException,
        ApprovalConsumedException,
        ApprovalTamperingException,
    ):
        err = cls("Token invalid")
        assert isinstance(err, ApprovalException)
        assert isinstance(err, FlowPilotException)


def test_workflow_hierarchy():
    """Verify workflow/DAG errors inherit from WorkflowException."""
    for cls in (CyclicDependencyError, InvalidWorkflowSchemaError, VariableInterpolationError):
        err = cls("DAG validation error")
        assert isinstance(err, WorkflowException)
        assert isinstance(err, FlowPilotException)


def test_execution_hierarchy():
    """Verify execution errors inherit from ExecutionException."""
    for cls in (
        ExecutionLimitExceededException,
        TaskTimeoutException,
        CircuitBreakerOpenException,
        TaskExecutionError,
        ToolNotFoundError,
        ToolValidationError,
        DuplicateExecutionError,
        RecoveryError,
    ):
        err = cls("Execution failure")
        assert isinstance(err, ExecutionException)
        assert isinstance(err, FlowPilotException)
