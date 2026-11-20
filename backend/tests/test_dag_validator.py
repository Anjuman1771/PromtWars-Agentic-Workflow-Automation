"""Tests for workflow schema and dependency graph validation."""

import pytest

from backend.app.core.models import WorkflowDefinition
from backend.app.core.validator import WorkflowValidator, validate_workflow
from backend.app.safety.exceptions import (
    CyclicDependencyError,
    ExecutionLimitExceededException,
    InvalidWorkflowSchemaError,
)
from backend.app.safety.limits import ExecutionLimits


def task(task_id, dependencies=None):
    return {
        "id": task_id,
        "name": task_id,
        "tool": "transform",
        "dependencies": dependencies or [],
    }


def test_valid_workflow_returns_validation_result():
    result = validate_workflow(
        {"name": "valid", "tasks": [task("first"), task("second", ["first"])]}
    )

    assert result.valid is True
    assert result.task_count == 2
    assert result.topological_order == ["first", "second"]


def test_validator_accepts_workflow_model():
    workflow = WorkflowDefinition.model_validate({"tasks": [task("first")]})
    assert WorkflowValidator().validate(workflow).task_count == 1


def test_duplicate_task_ids_are_rejected():
    with pytest.raises(InvalidWorkflowSchemaError, match="unique"):
        validate_workflow({"tasks": [task("same"), task("same")]})


def test_missing_dependency_is_rejected():
    with pytest.raises(InvalidWorkflowSchemaError, match="unknown task"):
        validate_workflow({"tasks": [task("first", ["absent"])]})


def test_self_dependency_is_rejected():
    with pytest.raises(InvalidWorkflowSchemaError, match="cannot depend on itself"):
        validate_workflow({"tasks": [task("first", ["first"])]})


def test_duplicate_dependency_entries_are_rejected():
    with pytest.raises(InvalidWorkflowSchemaError, match="duplicate dependencies"):
        validate_workflow({"tasks": [task("first"), task("second", ["first", "first"])]})


def test_circular_dependencies_are_rejected():
    with pytest.raises(CyclicDependencyError, match="circular"):
        validate_workflow(
            {"tasks": [task("first", ["second"]), task("second", ["first"])]}
        )


def test_configured_workflow_step_limit_is_enforced():
    validator = WorkflowValidator(limits=ExecutionLimits(max_workflow_steps=2))
    with pytest.raises(ExecutionLimitExceededException, match="maximum steps"):
        validator.validate({"tasks": [task("one"), task("two"), task("three")]})


def test_configured_workflow_payload_limit_is_enforced():
    validator = WorkflowValidator(limits=ExecutionLimits(max_payload_size_bytes=100))
    with pytest.raises(ExecutionLimitExceededException, match="Payload size"):
        validator.validate({"tasks": [task("one",)] , "metadata": "x" * 200})
