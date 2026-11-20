"""Tests for strict workflow model definitions."""

import pytest
from pydantic import ValidationError

from backend.app.core.models import TaskDefinition, TaskDependency, WorkflowDefinition


def test_workflow_models_accept_a_valid_typed_workflow():
    workflow = WorkflowDefinition.model_validate(
        {
            "id": "wf-1",
            "name": "Example workflow",
            "tasks": [
                {
                    "id": "fetch",
                    "name": "Fetch data",
                    "tool": "http_client",
                    "inputs": {"url": "https://example.test/data"},
                    "outputs": ["result"],
                    "requires_approval": False,
                    "retries": 2,
                    "timeout_seconds": 15,
                },
                {
                    "id": "summarize",
                    "name": "Summarize",
                    "tool_name": "transform.summary",
                    "dependencies": [{"task_id": "fetch"}],
                },
            ],
        }
    )

    assert workflow.tasks[0].tool_name == "http_client"
    assert workflow.tasks[0].input_parameters == {"url": "https://example.test/data"}
    assert workflow.tasks[1].dependencies == [TaskDependency(task_id="fetch")]
    assert workflow.tasks[1].retries == 0
    assert workflow.tasks[1].input_parameters == {}


def test_task_model_accepts_string_dependency_alias():
    task = TaskDefinition(
        id="next",
        name="Next task",
        tool="transform",
        depends_on=["first"],
    )

    assert task.dependencies == [TaskDependency(task_id="first")]


@pytest.mark.parametrize(
    "task_data",
    [
        {"tool": "invalid tool name"},
        {"requires_approval": "yes"},
        {"retries": -1},
        {"timeout_seconds": 0},
        {"unexpected": True},
    ],
)
def test_task_model_rejects_invalid_or_unknown_fields(task_data):
    valid = {"id": "task", "name": "Task", "tool": "transform"}
    with pytest.raises(ValidationError):
        TaskDefinition.model_validate({**valid, **task_data})


def test_models_do_not_share_mutable_defaults():
    first = TaskDefinition(id="one", name="One", tool="transform")
    second = TaskDefinition(id="two", name="Two", tool="transform")

    first.input_parameters["value"] = 1
    first.outputs.append("result")

    assert second.input_parameters == {}
    assert second.outputs == []
