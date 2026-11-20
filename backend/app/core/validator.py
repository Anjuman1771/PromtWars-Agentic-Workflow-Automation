"""Workflow schema and DAG validation."""

from collections import defaultdict
import heapq
from typing import Any

from pydantic import ValidationError

from ..safety.exceptions import (
    CyclicDependencyError,
    ExecutionLimitExceededException,
    InvalidWorkflowSchemaError,
)
from ..safety.limits import ExecutionLimits, execution_limits
from .models import WorkflowDefinition, WorkflowValidationResult


class WorkflowValidator:
    """Validates workflow structure, graph integrity, and configured limits."""

    def __init__(self, limits: ExecutionLimits | None = None):
        self.limits = limits or execution_limits

    def validate(
        self,
        definition: WorkflowDefinition | dict[str, Any],
    ) -> WorkflowValidationResult:
        """Validate a workflow or raise a clear domain exception."""
        if isinstance(definition, WorkflowDefinition):
            workflow = definition
        elif isinstance(definition, dict):
            self.limits.validate_payload_size(definition)
            try:
                workflow = WorkflowDefinition.model_validate(definition)
            except ValidationError as exc:
                raise InvalidWorkflowSchemaError(
                    "Workflow definition does not match the required schema.",
                    details={"errors": exc.errors(include_input=False)},
                ) from exc
        else:
            raise InvalidWorkflowSchemaError(
                "Workflow definition must be a mapping or WorkflowDefinition."
            )

        serialized = workflow.model_dump(mode="json", exclude_none=True)
        try:
            self.limits.validate_workflow_definition(serialized)
        except ExecutionLimitExceededException:
            raise

        task_ids = [task.id for task in workflow.tasks]
        id_set = set(task_ids)
        if len(id_set) != len(task_ids):
            raise InvalidWorkflowSchemaError("Task IDs must be unique.")

        indegree = {task_id: 0 for task_id in task_ids}
        dependents: dict[str, list[str]] = defaultdict(list)

        for task in workflow.tasks:
            dependency_ids = [dependency.task_id for dependency in task.dependencies]
            if len(set(dependency_ids)) != len(dependency_ids):
                raise InvalidWorkflowSchemaError(
                    f"Task '{task.id}' contains duplicate dependencies.",
                    details={"task_id": task.id},
                )
            for dependency_id in dependency_ids:
                if dependency_id == task.id:
                    raise InvalidWorkflowSchemaError(
                        f"Task '{task.id}' cannot depend on itself.",
                        details={"task_id": task.id},
                    )
                if dependency_id not in id_set:
                    raise InvalidWorkflowSchemaError(
                        f"Task '{task.id}' depends on unknown task '{dependency_id}'.",
                        details={"task_id": task.id, "dependency_id": dependency_id},
                    )
                indegree[task.id] += 1
                dependents[dependency_id].append(task.id)

        ready = [task_id for task_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            task_id = heapq.heappop(ready)
            ordered.append(task_id)
            for dependent_id in sorted(dependents[task_id]):
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    heapq.heappush(ready, dependent_id)

        if len(ordered) != len(task_ids):
            cyclic_tasks = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
            raise CyclicDependencyError(
                "Workflow contains a circular task dependency.",
                details={"task_ids": cyclic_tasks},
            )

        return WorkflowValidationResult(
            task_count=len(task_ids),
            topological_order=ordered,
        )


def validate_workflow(
    definition: WorkflowDefinition | dict[str, Any],
    limits: ExecutionLimits | None = None,
) -> WorkflowValidationResult:
    """Convenience function for validating a workflow definition."""
    return WorkflowValidator(limits=limits).validate(definition)
