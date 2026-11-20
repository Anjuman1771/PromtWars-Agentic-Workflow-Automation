"""Tests for safe task-output variable interpolation."""

import pytest

from backend.app.core.interpolation import interpolate_variables
from backend.app.core.models import TaskExecutionContext
from backend.app.safety.exceptions import (
    ExecutionLimitExceededException,
    VariableInterpolationError,
)
from backend.app.safety.limits import ExecutionLimits


def context(outputs=None):
    workflow = {
        "tasks": [
            {
                "id": "producer",
                "name": "Producer",
                "tool": "transform",
                "outputs": ["result"],
            },
            {
                "id": "consumer",
                "name": "Consumer",
                "tool": "transform",
                "dependencies": ["producer"],
            },
        ]
    }
    return TaskExecutionContext(
        workflow=workflow,
        task_id="consumer",
        task_outputs=outputs or {},
    )


def test_nested_output_interpolation_preserves_typed_values():
    execution_context = context(
        {"producer": {"result": {"nested": {"value": "resolved"}, "count": 3}}}
    )
    result = interpolate_variables(
        {
            "message": "Value: {{tasks.producer.output.result.nested.value}}",
            "whole": "{{tasks.producer.output.result}}",
            "items": ["count={{tasks.producer.output.result.count}}"],
        },
        execution_context,
    )

    assert result == {
        "message": "Value: resolved",
        "whole": {"nested": {"value": "resolved"}, "count": 3},
        "items": ["count=3"],
    }


def test_missing_task_reference_is_rejected():
    with pytest.raises(VariableInterpolationError, match="unknown task"):
        interpolate_variables("{{tasks.absent.output.result}}", context())


def test_missing_or_undeclared_output_is_rejected():
    with pytest.raises(VariableInterpolationError, match="not declared"):
        interpolate_variables(
            "{{tasks.producer.output.secret}}",
            context({"producer": {"secret": "not declared"}}),
        )
    with pytest.raises(VariableInterpolationError, match="was not found"):
        interpolate_variables(
            "{{tasks.producer.output.result.missing}}",
            context({"producer": {"result": {"present": True}}}),
        )


def test_non_dependency_output_is_rejected():
    workflow = {
        "tasks": [
            {"id": "source", "name": "Source", "tool": "transform", "outputs": ["result"]},
            {"id": "consumer", "name": "Consumer", "tool": "transform"},
        ]
    }
    execution_context = TaskExecutionContext(
        workflow=workflow,
        task_id="consumer",
        task_outputs={"source": {"result": "hidden"}},
    )
    with pytest.raises(VariableInterpolationError, match="has not declared"):
        interpolate_variables("{{tasks.source.output.result}}", execution_context)


@pytest.mark.parametrize(
    "expression",
    [
        "{{tasks.producer.output.result",
        "tasks.producer.output.result}}",
        "{{env.API_KEY}}",
        "{{tasks.producer.output.result[0]}}",
    ],
)
def test_malformed_and_unsupported_expressions_are_rejected(expression):
    with pytest.raises(VariableInterpolationError):
        interpolate_variables(expression, context({"producer": {"result": "safe"}}))


def test_interpolation_expressions_in_mapping_keys_are_rejected():
    with pytest.raises(VariableInterpolationError, match="mapping keys"):
        interpolate_variables(
            {"{{malformed": "value"},
            context({"producer": {"result": "safe"}}),
        )


def test_recursive_interpolation_is_rejected():
    with pytest.raises(VariableInterpolationError, match="Recursive"):
        interpolate_variables(
            "{{tasks.producer.output.result}}",
            context({"producer": {"result": "{{tasks.other.output.value}}"}}),
        )


def test_oversized_input_and_output_payloads_are_rejected():
    execution_context = context({"producer": {"result": "x" * 100}})
    limits = ExecutionLimits(max_payload_size_bytes=32)

    with pytest.raises(ExecutionLimitExceededException):
        interpolate_variables("x" * 40, execution_context, limits=limits)

    with pytest.raises(ExecutionLimitExceededException):
        interpolate_variables(
            "{{tasks.producer.output.result}}",
            execution_context,
            limits=limits,
        )


def test_interpolation_does_not_evaluate_python_expressions():
    expression = "{{__import__('os').system('echo not executed')}}"

    with pytest.raises(VariableInterpolationError):
        interpolate_variables(expression, context())
