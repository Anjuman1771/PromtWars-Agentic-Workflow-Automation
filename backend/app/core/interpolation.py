"""Safe interpolation of explicitly declared workflow task outputs."""

import json
import re
from typing import Any

from ..safety.exceptions import VariableInterpolationError
from ..safety.limits import ExecutionLimits, execution_limits
from .models import TaskExecutionContext

_REFERENCE = re.compile(
    r"\{\{tasks\.([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.output\."
    r"([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\}\}"
)


def _contains_expression_marker(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "}}" in value
    if isinstance(value, dict):
        return any(_contains_expression_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_expression_marker(item) for item in value)
    return False


def _resolve_reference(
    task_id: str,
    output_path: str,
    context: TaskExecutionContext,
) -> Any:
    tasks = {task.id: task for task in context.workflow.tasks}
    referenced_task = tasks.get(task_id)
    if referenced_task is None:
        raise VariableInterpolationError(
            f"Interpolation references unknown task '{task_id}'.",
            details={"task_id": task_id},
        )

    current_task = tasks[context.task_id]
    dependency_ids = {dependency.task_id for dependency in current_task.dependencies}
    if task_id not in dependency_ids:
        raise VariableInterpolationError(
            f"Task '{context.task_id}' has not declared '{task_id}' as a dependency.",
            details={"task_id": context.task_id, "dependency_id": task_id},
        )

    parts = output_path.split(".")
    if parts[0] not in referenced_task.outputs:
        raise VariableInterpolationError(
            f"Output '{parts[0]}' is not declared by task '{task_id}'.",
            details={"task_id": task_id, "output": parts[0]},
        )

    if task_id not in context.task_outputs:
        raise VariableInterpolationError(
            f"No output is available for task '{task_id}'.",
            details={"task_id": task_id},
        )

    value: Any = context.task_outputs[task_id]
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            raise VariableInterpolationError(
                f"Output path '{output_path}' was not found for task '{task_id}'.",
                details={"task_id": task_id, "output_path": output_path},
            )
        value = value[part]

    if _contains_expression_marker(value):
        raise VariableInterpolationError(
            "Recursive interpolation in task outputs is not supported.",
            details={"task_id": task_id, "output_path": output_path},
        )
    return value


def _interpolate_string(value: str, context: TaskExecutionContext) -> Any:
    matches = list(_REFERENCE.finditer(value))
    if not matches:
        if "{{" in value or "}}" in value:
            raise VariableInterpolationError("Malformed or unsupported interpolation expression.")
        return value

    if value.startswith("{{") and matches[0].start() == 0 and matches[0].end() == len(value):
        task_id, output_path = matches[0].groups()
        return _resolve_reference(task_id, output_path, context)

    cursor = 0
    parts: list[str] = []
    for match in matches:
        prefix = value[cursor : match.start()]
        if "{{" in prefix or "}}" in prefix:
            raise VariableInterpolationError("Malformed or unsupported interpolation expression.")
        parts.append(prefix)
        task_id, output_path = match.groups()
        output = _resolve_reference(task_id, output_path, context)
        if isinstance(output, (dict, list, tuple)):
            raise VariableInterpolationError(
                "Object or array outputs can only be interpolated as a complete value."
            )
        parts.append(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
        cursor = match.end()

    suffix = value[cursor:]
    if "{{" in suffix or "}}" in suffix:
        raise VariableInterpolationError("Malformed or unsupported interpolation expression.")
    parts.append(suffix)
    return "".join(parts)


def _interpolate_value(value: Any, context: TaskExecutionContext) -> Any:
    if isinstance(value, str):
        return _interpolate_string(value, context)
    if isinstance(value, dict):
        interpolated: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and ("{{" in key or "}}" in key):
                raise VariableInterpolationError(
                    "Interpolation expressions are not supported in mapping keys."
                )
            interpolated[key] = _interpolate_value(item, context)
        return interpolated
    if isinstance(value, list):
        return [_interpolate_value(item, context) for item in value]
    if isinstance(value, tuple):
        return tuple(_interpolate_value(item, context) for item in value)
    return value


def interpolate_variables(
    value: Any,
    context: TaskExecutionContext,
    limits: ExecutionLimits | None = None,
) -> Any:
    """Interpolate declared dependency outputs, enforcing payload-size limits.

    No environment lookup or Python expression evaluation is performed.
    """
    active_limits = limits or execution_limits
    active_limits.validate_payload_size(value)
    active_limits.validate_payload_size(context.task_outputs)
    resolved = _interpolate_value(value, context)
    active_limits.validate_payload_size(resolved)
    return resolved
