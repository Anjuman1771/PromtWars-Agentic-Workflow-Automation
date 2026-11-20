"""Deterministic topological workflow planning without task execution."""

from typing import Iterable

from ..safety.exceptions import InvalidWorkflowSchemaError
from ..safety.limits import ExecutionLimits, execution_limits
from .models import ExecutionPlan, WorkflowDefinition
from .validator import WorkflowValidator


class ExecutionPlanner:
    """Produces deterministic topological orders and ready task batches."""

    def __init__(self, limits: ExecutionLimits | None = None):
        self.limits = limits or execution_limits
        self.validator = WorkflowValidator(limits=self.limits)

    def plan(self, definition: WorkflowDefinition | dict) -> ExecutionPlan:
        """Return task order and parallel-ready groups without executing tasks."""
        validation = self.validator.validate(definition)
        workflow = (
            definition
            if isinstance(definition, WorkflowDefinition)
            else WorkflowDefinition.model_validate(definition)
        )
        tasks = {task.id: task for task in workflow.tasks}
        completed: set[str] = set()
        remaining = set(tasks)
        groups: list[list[str]] = []
        concurrency_limit = self.limits.max_concurrent_tasks

        while remaining:
            ready = sorted(
                task_id
                for task_id in remaining
                if {dependency.task_id for dependency in tasks[task_id].dependencies}
                .issubset(completed)
            )
            if not ready:
                raise InvalidWorkflowSchemaError(
                    "No executable task is available after workflow validation."
                )
            for offset in range(0, len(ready), concurrency_limit):
                groups.append(ready[offset : offset + concurrency_limit])
            completed.update(ready)
            remaining.difference_update(ready)

        return ExecutionPlan(
            topological_order=validation.topological_order,
            initial_ready_tasks=sorted(
                task_id
                for task_id, task in tasks.items()
                if not task.dependencies
            ),
            parallel_groups=groups,
            max_concurrent_tasks=concurrency_limit,
        )

    def get_ready_tasks(
        self,
        definition: WorkflowDefinition | dict,
        completed_task_ids: Iterable[str] = (),
    ) -> list[str]:
        """Find sorted tasks whose dependencies are all in the completed set."""
        self.validator.validate(definition)
        workflow = (
            definition
            if isinstance(definition, WorkflowDefinition)
            else WorkflowDefinition.model_validate(definition)
        )
        tasks = {task.id: task for task in workflow.tasks}
        completed = set(completed_task_ids)
        unknown = completed - tasks.keys()
        if unknown:
            raise InvalidWorkflowSchemaError(
                "Completed task IDs must belong to the workflow.",
                details={"task_ids": sorted(unknown)},
            )
        return sorted(
            task_id
            for task_id, task in tasks.items()
            if task_id not in completed
            and {dependency.task_id for dependency in task.dependencies}.issubset(completed)
        )
