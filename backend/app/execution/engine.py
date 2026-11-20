"""Persistent asynchronous workflow execution with approval and recovery gates."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import uuid
from typing import Any

from pydantic import BaseModel

from ..core.models import TaskDefinition, WorkflowDefinition
from ..core.validator import WorkflowValidator
from ..observability.logger import get_logger
from ..safety.approval import ApprovalTokenManager, hash_token
from ..safety.circuit_breaker import CircuitBreaker
from ..safety.exceptions import (
    ApprovalException,
    ApprovalExpiredException,
    ApprovalTamperingException,
    DuplicateExecutionError,
    InvalidWorkflowSchemaError,
    RecoveryError,
)
from ..safety.limits import ExecutionLimits, execution_limits
from ..safety.redaction import redact_object, redact_text
from ..storage.repository import WorkflowRepository, repository as default_repository
from ..tools.registry import ToolRegistry, default_tool_registry
from .models import ApprovalRequest, ExecutionResult, RunState, TaskState
from .recovery import (
    load_outputs,
    load_workflow,
    reconcile_pending_approvals,
    recover_interrupted_runs,
    recover_running_task,
)
from .task_runner import TaskRunner

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TASK_DB_TO_STATE = {
    "PENDING": TaskState.PENDING,
    "RUNNING": TaskState.RUNNING,
    "PAUSED_APPROVAL": TaskState.WAITING_APPROVAL,
    "COMPLETED": TaskState.COMPLETED,
    "FAILED": TaskState.FAILED,
    "CANCELLED": TaskState.CANCELLED,
    "SKIPPED": TaskState.SKIPPED,
}
_RUN_DB_TO_STATE = {
    "PENDING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "PAUSED_APPROVAL": RunState.WAITING_APPROVAL,
    "COMPLETED": RunState.COMPLETED,
    "FAILED": RunState.FAILED,
    "CANCELLED": RunState.CANCELLED,
    "TIMED_OUT": RunState.FAILED,
}
_ACTIVE_TASK_STATUSES = ("PENDING", "RUNNING", "PAUSED_APPROVAL")


class ExecutionEngine:
    """Execute only authorized registry tools and persist every lifecycle boundary.

    A RUNNING task with no idempotency result is considered ambiguous after a
    process crash. Recovery fails it closed rather than invoking a possible
    external side effect a second time.
    """

    def __init__(
        self,
        repository: WorkflowRepository | None = None,
        tool_registry: ToolRegistry | None = None,
        approval_manager: ApprovalTokenManager | None = None,
        limits: ExecutionLimits | None = None,
        circuit_breakers: dict[str, CircuitBreaker] | None = None,
        logger: logging.Logger | None = None,
        *,
        retry_base_delay_seconds: float | None = None,
        retry_max_delay_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.repository = repository or default_repository
        self.tool_registry = tool_registry or default_tool_registry
        self.approval_manager = approval_manager or ApprovalTokenManager(
            repository=self.repository
        )
        self.limits = limits or execution_limits
        self.validator = WorkflowValidator(limits=self.limits)
        self.logger = logger or get_logger("flowpilot.execution.engine")
        self.retry_base_delay_seconds = (
            self._configured_retry_base()
            if retry_base_delay_seconds is None
            else retry_base_delay_seconds
        )
        self.retry_max_delay_seconds = (
            self._configured_retry_max()
            if retry_max_delay_seconds is None
            else retry_max_delay_seconds
        )
        if self.retry_base_delay_seconds <= 0 or self.retry_max_delay_seconds <= 0:
            raise ValueError("Retry delays must be positive.")
        self.sleep = sleep
        self.circuit_breakers = circuit_breakers if circuit_breakers is not None else {}
        self._active_runs: dict[str, asyncio.Task[Any]] = {}
        self._tool_workers: set[asyncio.Task[Any]] = set()
        self._interrupted_tool_tasks: set[tuple[str, str]] = set()
        self._workflow_cache: dict[str, WorkflowDefinition] = {}
        self._output_cache: dict[str, dict[str, Any]] = {}
        self.task_runner = TaskRunner(self)

    @staticmethod
    def _configured_retry_base() -> float:
        from ..config import settings

        return settings.retry_base_delay_seconds

    @staticmethod
    def _configured_retry_max() -> float:
        from ..config import settings

        return settings.retry_max_delay_seconds

    async def execute(
        self,
        workflow: WorkflowDefinition,
        *,
        run_id: str | None = None,
    ) -> ExecutionResult:
        """Create a durable run and execute its validated workflow."""
        if not isinstance(workflow, WorkflowDefinition):
            raise InvalidWorkflowSchemaError(
                "The execution engine requires a validated WorkflowDefinition."
            )
        self.validator.validate(workflow)
        self.tool_registry.validate_workflow(workflow)
        resolved_run_id = run_id or f"run_{uuid.uuid4().hex}"
        if not _RUN_ID_PATTERN.fullmatch(resolved_run_id):
            raise InvalidWorkflowSchemaError("The workflow run ID has an invalid format.")

        if await self.repository.get_run(resolved_run_id) is not None:
            raise DuplicateExecutionError("The workflow run ID has already been used.")
        workflow_id = f"wf_{uuid.uuid4().hex}"
        stored_workflow = await self.repository.create_workflow(
            workflow_id=workflow_id,
            title=workflow.name or "Workflow execution",
            description=workflow.description,
            raw_prompt="Created by the execution engine.",
            definition=workflow.model_dump(mode="json", exclude_none=True),
        )
        await self.repository.create_run(
            run_id=resolved_run_id,
            workflow_id=str(stored_workflow["id"]),
        )
        for task in workflow.tasks:
            await self.repository.create_task(
                run_id=resolved_run_id,
                task_id=task.id,
                name=task.name,
                tool_name=task.tool_name,
                requires_approval=task.requires_approval,
                depends_on=[dependency.task_id for dependency in task.dependencies],
                input_params=task.input_parameters,
                input_redacted=self._was_redacted(task.input_parameters),
            )

        self._workflow_cache[resolved_run_id] = workflow
        self._output_cache[resolved_run_id] = {}
        self._log_event("RUN_STARTED", resolved_run_id)
        await self._audit(resolved_run_id, "INFO", "RUN_STARTED", "Workflow run started.")
        return await self._continue(
            resolved_run_id,
            workflow,
            outputs={},
            unavailable_outputs=set(),
            input_overrides={},
            allow_original_inputs=True,
        )

    async def resume(self, run_id: str) -> ExecutionResult:
        """Resume safe pending work; ambiguous in-flight work requires recovery first."""
        cached_workflow = self._workflow_cache.get(run_id)
        workflow = cached_workflow
        if workflow is None:
            workflow = await load_workflow(self, run_id)
        self.validator.validate(workflow)
        self.tool_registry.validate_workflow(workflow)
        outputs, unavailable_outputs = await load_outputs(self, run_id)
        self._workflow_cache[run_id] = workflow
        return await self._continue(
            run_id,
            workflow,
            outputs=outputs,
            unavailable_outputs=unavailable_outputs,
            input_overrides={},
            allow_original_inputs=cached_workflow is not None,
        )

    async def decide_approval(
        self,
        run_id: str,
        task_id: str,
        raw_token: str,
        action_payload: dict[str, Any],
        *,
        decision: str = "APPROVE",
        decision_by: str = "operator",
    ) -> ExecutionResult:
        """Consume an exact, single-use approval and continue or reject its task.

        The caller returns the original action payload alongside the raw token.
        This permits a waiting approval to survive a restart without persisting
        secret arguments in SQLite. The token manager verifies the payload hash.
        """
        run = await self.repository.get_run(run_id)
        task_row = await self.repository.get_task(run_id, task_id)
        if run is None or task_row is None:
            raise ApprovalException("The approval does not identify a stored run task.")
        if task_row["status"] != "PAUSED_APPROVAL":
            raise ApprovalException("The task is not waiting for approval.")

        approval = await self.repository.get_approval(hash_token(raw_token))
        if approval is not None and (
            approval.get("run_id") != run_id or approval.get("task_id") != task_id
        ):
            raise ApprovalTamperingException(
                "The approval token is not bound to the requested run and task."
            )

        cached_workflow = self._workflow_cache.get(run_id)
        workflow = cached_workflow
        if workflow is None:
            workflow = await load_workflow(self, run_id)
        task = self._task_from_workflow(workflow, task_id)
        arguments = self._validate_approval_payload(
            action_payload,
            run_id=run_id,
            task=task,
        )
        decision_value = decision.strip().upper()
        if decision_value not in {"APPROVE", "REJECT"}:
            raise ApprovalException("Approval decision must be APPROVE or REJECT.")

        try:
            consumed = await self.approval_manager.verify_and_consume(
                raw_token,
                action=decision_value,
                decision_by=redact_text(decision_by[:128]),
                expected_payload=action_payload,
            )
        except ApprovalExpiredException:
            await self._fail_expired_approval(run_id, task_id)
            raise

        if consumed.get("run_id") != run_id or consumed.get("task_id") != task_id:
            raise ApprovalTamperingException(
                "The approval token is not bound to the requested run and task."
            )
        fingerprint = self._fingerprint(run_id, task, arguments)
        if task_row.get("idempotency_key") != fingerprint:
            raise ApprovalTamperingException(
                "The approval does not match the persisted task input fingerprint."
            )

        if decision_value == "REJECT":
            await self.repository.transition_task_status(
                run_id,
                task_id,
                ("PAUSED_APPROVAL",),
                "FAILED",
                error_message="Approval was rejected; the tool was not invoked.",
                completed=True,
            )
            await self._audit(
                run_id,
                "WARNING",
                "APPROVAL_REJECTED",
                "Approval was rejected; the task was not executed.",
                task_id,
            )
            await self.repository.update_run_status(run_id, "PENDING")
            return await self._continue(
                run_id,
                workflow,
                outputs=(await load_outputs(self, run_id))[0],
                unavailable_outputs=set(),
                input_overrides={},
                allow_original_inputs=cached_workflow is not None,
            )

        validated_inputs = self.tool_registry.validate_inputs(task.tool_name, arguments)
        changed = await self.repository.transition_task_status(
            run_id,
            task_id,
            ("PAUSED_APPROVAL",),
            "PENDING",
        )
        if not changed:
            raise DuplicateExecutionError(
                "The approved task changed state before execution could resume."
            )
        await self.repository.update_run_status(run_id, "PENDING")
        await self._audit(
            run_id,
            "INFO",
            "APPROVAL_APPROVED",
            "Approval was accepted; the task may now execute.",
            task_id,
        )
        outputs, unavailable_outputs = await load_outputs(self, run_id)
        input_overrides = {task_id: (arguments, validated_inputs)}
        return await self._continue(
            run_id,
            workflow,
            outputs=outputs,
            unavailable_outputs=unavailable_outputs,
            input_overrides=input_overrides,
            allow_original_inputs=cached_workflow is not None,
        )

    async def recover_interrupted_runs(self) -> list[ExecutionResult]:
        """Reconcile persisted runs after restart without repeating ambiguous work.

        Call once during process startup, before accepting new execution requests.
        Completed idempotency results are recovered; tasks left RUNNING without a
        cached result fail and require operator reconciliation.
        """
        return await recover_interrupted_runs(self)

    async def cancel(self, run_id: str) -> ExecutionResult:
        """Cancel an active run or persist cancellation for a not-yet-active run."""
        active = self._active_runs.get(run_id)
        current = asyncio.current_task()
        if active is not None and active is not current and not active.done():
            if active.cancelling() == 0:
                active.cancel()
            try:
                await active
            except asyncio.CancelledError:
                pass
            result = await self._result(run_id)
            self._release_run_cache(run_id)
            return result

        task_rows = await self.repository.list_tasks(run_id)
        cancellation_received = False
        for row in task_rows:
            task_id = str(row["task_id"])
            key = (run_id, task_id)
            reconciliation_required = bool(
                row.get("reconciliation_required")
                or key in self._interrupted_tool_tasks
            )
            if (
                key in self._interrupted_tool_tasks
                and row["status"] == "COMPLETED"
                and row.get("output_data_json") is not None
                and not row.get("reconciliation_required")
            ):
                self._interrupted_tool_tasks.discard(key)
                continue
            if row["status"] in _ACTIVE_TASK_STATUSES:
                expected_statuses = (str(row["status"]),)
                status = "CANCELLED"
            elif key in self._interrupted_tool_tasks and not row.get("reconciliation_required"):
                expected_statuses = (str(row["status"]),)
                status = str(row["status"])
            else:
                continue

            changed, interrupted = await self._await_cleanup(
                self.repository.transition_task_status(
                    run_id,
                    task_id,
                    expected_statuses,
                    status,
                    error_message=(
                        "Tool invocation was interrupted before its result was "
                        "durably recorded; external side effects may have occurred "
                        "and require manual reconciliation."
                        if reconciliation_required
                        else "Workflow run was cancelled."
                    ),
                    reconciliation_required=reconciliation_required,
                    completed=True,
                )
            )
            cancellation_received = cancellation_received or interrupted
            if changed and reconciliation_required:
                self._interrupted_tool_tasks.discard(key)
            if changed:
                self._log_event("TASK_CANCELLED", run_id, task_id)
                await self._audit(
                    run_id,
                    "WARNING",
                    "TASK_CANCELLED",
                    "Task cancellation requires manual reconciliation."
                    if reconciliation_required
                    else "Task execution was cancelled.",
                    task_id,
                )
        _, interrupted = await self._await_cleanup(
            self.repository.update_run_status(
                run_id,
                "CANCELLED",
                error_message="Workflow run was cancelled.",
                completed=True,
            )
        )
        cancellation_received = cancellation_received or interrupted
        self._log_event("RUN_CANCELLED", run_id)
        _, interrupted = await self._await_cleanup(
            self._audit(run_id, "WARNING", "RUN_CANCELLED", "Workflow run was cancelled.")
        )
        cancellation_received = cancellation_received or interrupted
        result = await self._result(run_id)
        self._release_run_cache(run_id)
        if cancellation_received:
            raise asyncio.CancelledError
        return result

    async def _continue(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
        *,
        outputs: dict[str, Any],
        unavailable_outputs: set[str],
        input_overrides: dict[str, tuple[dict[str, Any], BaseModel]],
        allow_original_inputs: bool,
    ) -> ExecutionResult:
        if run_id in self._active_runs:
            raise DuplicateExecutionError("A workflow run is already active.")
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Workflow execution requires an asyncio task.")
        if not await self.repository.claim_run(run_id):
            run = await self.repository.get_run(run_id)
            if run is not None and run["status"] in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            }:
                result = await self._result(run_id)
                self._release_run_cache(run_id)
                return result
            raise DuplicateExecutionError(
                "The workflow run is already active or is not in a resumable state."
            )

        self._active_runs[run_id] = current
        self._workflow_cache[run_id] = workflow
        self._output_cache[run_id] = outputs
        approval_requests: list[ApprovalRequest] = []
        try:
            deadline = await self._workflow_deadline(run_id)
            await self._ensure_task_rows(run_id, workflow)
            while True:
                for task_row in await self.repository.list_tasks(run_id):
                    if task_row["status"] == "RUNNING":
                        await recover_running_task(self, run_id, task_row)
                await reconcile_pending_approvals(
                    self,
                    run_id,
                    approved_task_ids=set(input_overrides),
                )
                task_rows = await self.repository.list_tasks(run_id)
                for task_row in task_rows:
                    task_id = str(task_row["task_id"])
                    if task_row["status"] == "COMPLETED" and task_id not in outputs:
                        outputs[task_id] = task_row.get("output_data_json") or {}
                        if task_row.get("output_redacted"):
                            unavailable_outputs.add(task_id)
                await self._skip_failed_dependencies(run_id, workflow, task_rows)
                task_rows = await self.repository.list_tasks(run_id)
                states = {str(row["task_id"]): str(row["status"]) for row in task_rows}

                if "FAILED" in states.values() or "CANCELLED" in states.values():
                    await self._skip_pending_after_failure(run_id, task_rows)
                    await self._finish_run_from_tasks(run_id)
                    break

                pending = [
                    task
                    for task in workflow.tasks
                    if states.get(task.id) == "PENDING"
                    and all(
                        states.get(dependency.task_id) == "COMPLETED"
                        for dependency in task.dependencies
                    )
                ]
                if not pending:
                    if states and all(
                        status in {"COMPLETED", "SKIPPED"} for status in states.values()
                    ):
                        await self._finish_run_from_tasks(run_id)
                    elif "PAUSED_APPROVAL" in states.values():
                        await self.repository.update_run_status(run_id, "PAUSED_APPROVAL")
                        self._log_event("RUN_WAITING_APPROVAL", run_id)
                    else:
                        await self._skip_pending_after_failure(run_id, task_rows)
                        await self._finish_run_from_tasks(run_id)
                    break

                if deadline <= asyncio.get_running_loop().time():
                    for task in pending:
                        await self._fail_task(
                            run_id,
                            task.id,
                            "Workflow execution deadline exceeded.",
                            expected_statuses=("PENDING",),
                            event_type="TASK_FAILED",
                        )
                    await self._skip_pending_after_failure(
                        run_id, await self.repository.list_tasks(run_id)
                    )
                    await self._finish_run_from_tasks(run_id)
                    break

                for offset in range(0, len(pending), self.limits.max_concurrent_tasks):
                    batch = pending[offset : offset + self.limits.max_concurrent_tasks]
                    batch_results = await asyncio.gather(
                        *(
                            self.task_runner.execute(
                                run_id,
                                task,
                                workflow,
                                outputs,
                                unavailable_outputs,
                                input_overrides.get(task.id),
                                deadline,
                                approval_requests,
                                allow_original_inputs,
                            )
                            for task in batch
                        ),
                        return_exceptions=True,
                    )
                    for task, result in zip(batch, batch_results):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        if isinstance(result, BaseException):
                            await self._fail_task(
                                run_id,
                                task.id,
                                self._safe_error(result),
                                expected_statuses=("PENDING", "RUNNING"),
                                event_type="TASK_FAILED",
                            )
                        else:
                            completed_row = await self.repository.get_task(
                                run_id, task.id
                            )
                            if (
                                completed_row is not None
                                and completed_row["status"] == "COMPLETED"
                            ):
                                outputs[task.id] = result
                    self._output_cache[run_id] = outputs
                    if any(
                        row["status"] in {"FAILED", "CANCELLED"}
                        for row in await self.repository.list_tasks(run_id)
                    ):
                        break

            result = await self._result(run_id, tuple(approval_requests))
            if result.state == RunState.COMPLETED:
                self._log_event("RUN_COMPLETED", run_id)
                await self._audit(run_id, "INFO", "RUN_COMPLETED", "Workflow run completed.")
            elif result.state == RunState.FAILED:
                self._log_event("RUN_FAILED", run_id)
                await self._audit(
                    run_id,
                    "ERROR",
                    "RUN_FAILED",
                    "Workflow run failed.",
                )
            if result.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                self._release_run_cache(run_id)
            return result
        except asyncio.CancelledError:
            await self._cancel_persisted_tasks(run_id)
            self._release_run_cache(run_id)
            raise
        finally:
            self._active_runs.pop(run_id, None)

    async def _ensure_task_rows(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
    ) -> None:
        existing = {
            str(row["task_id"])
            for row in await self.repository.list_tasks(run_id)
        }
        for task in workflow.tasks:
            if task.id in existing:
                continue
            await self.repository.create_task(
                run_id=run_id,
                task_id=task.id,
                name=task.name,
                tool_name=task.tool_name,
                requires_approval=task.requires_approval,
                depends_on=[dependency.task_id for dependency in task.dependencies],
                input_params=task.input_parameters,
                input_redacted=self._was_redacted(task.input_parameters),
            )

    async def _workflow_deadline(self, run_id: str) -> float:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RecoveryError("The workflow run does not exist.")
        started_at = run.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_at))
        except (TypeError, ValueError):
            raise RecoveryError("The workflow start time is invalid.") from None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        remaining = self.limits.max_run_time_seconds - max(0.0, elapsed)
        return asyncio.get_running_loop().time() + max(0.0, remaining)

    async def _skip_failed_dependencies(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
        task_rows: list[dict[str, Any]],
    ) -> None:
        states = {str(row["task_id"]): str(row["status"]) for row in task_rows}
        for task in workflow.tasks:
            if states.get(task.id) != "PENDING":
                continue
            if any(
                states.get(dependency.task_id) in {"FAILED", "CANCELLED", "SKIPPED"}
                for dependency in task.dependencies
            ):
                await self.repository.transition_task_status(
                    run_id,
                    task.id,
                    ("PENDING",),
                    "SKIPPED",
                    error_message="A required dependency did not complete successfully.",
                    completed=True,
                )

    async def _skip_pending_after_failure(
        self,
        run_id: str,
        task_rows: list[dict[str, Any]],
    ) -> None:
        for row in task_rows:
            if row["status"] == "PENDING":
                await self.repository.transition_task_status(
                    run_id,
                    str(row["task_id"]),
                    ("PENDING",),
                    "SKIPPED",
                    error_message="Scheduling stopped after a terminal task failure.",
                    completed=True,
                )

    async def _finish_run_from_tasks(self, run_id: str) -> None:
        rows = await self.repository.list_tasks(run_id)
        statuses = {str(row["status"]) for row in rows}
        if "RUNNING" in statuses:
            for row in rows:
                if row["status"] == "RUNNING":
                    await recover_running_task(self, run_id, row)
            rows = await self.repository.list_tasks(run_id)
            statuses = {str(row["status"]) for row in rows}
        if "CANCELLED" in statuses:
            status = "CANCELLED"
            message = "Workflow run was cancelled."
        elif "FAILED" in statuses:
            status = "FAILED"
            message = "One or more workflow tasks failed."
        elif "PAUSED_APPROVAL" in statuses:
            status = "PAUSED_APPROVAL"
            message = None
        else:
            status = "COMPLETED"
            message = None
        await self.repository.update_run_status(
            run_id,
            status,
            error_message=message,
            completed=status in {"CANCELLED", "FAILED", "COMPLETED"},
        )

    async def _cancel_persisted_tasks(self, run_id: str) -> None:
        for row in await self.repository.list_tasks(run_id):
            task_id = str(row["task_id"])
            key = (run_id, task_id)
            reconciliation_required = bool(
                row.get("reconciliation_required")
                or key in self._interrupted_tool_tasks
            )
            if (
                key in self._interrupted_tool_tasks
                and row["status"] == "COMPLETED"
                and row.get("output_data_json") is not None
                and not row.get("reconciliation_required")
            ):
                self._interrupted_tool_tasks.discard(key)
                continue
            if row["status"] in _ACTIVE_TASK_STATUSES:
                status = "CANCELLED"
            elif key in self._interrupted_tool_tasks and not row.get(
                "reconciliation_required"
            ):
                status = str(row["status"])
            else:
                continue
            changed, _ = await self._await_cleanup(
                self.repository.transition_task_status(
                    run_id,
                    task_id,
                    (str(row["status"]),),
                    status,
                    error_message=(
                        "Tool invocation was interrupted before its result was "
                        "durably recorded; external side effects may have occurred "
                        "and require manual reconciliation."
                        if reconciliation_required
                        else "Workflow run was cancelled."
                    ),
                    reconciliation_required=reconciliation_required,
                    completed=True,
                )
            )
            if changed and reconciliation_required:
                self._interrupted_tool_tasks.discard(key)
        await self._await_cleanup(
            self.repository.update_run_status(
                run_id,
                "CANCELLED",
                error_message="Workflow run was cancelled.",
                completed=True,
            )
        )
        self._log_event("RUN_CANCELLED", run_id)
        await self._await_cleanup(
            self._audit(
                run_id, "WARNING", "RUN_CANCELLED", "Workflow run was cancelled."
            )
        )

    async def _fail_task(
        self,
        run_id: str,
        task_id: str,
        message: str,
        *,
        expected_statuses: tuple[str, ...],
        event_type: str,
        error_type: str | None = None,
        reconciliation_required: bool = False,
    ) -> None:
        safe_message = redact_text(message)
        transition = self.repository.transition_task_status(
            run_id,
            task_id,
            expected_statuses,
            "FAILED",
            error_message=safe_message,
            reconciliation_required=reconciliation_required or None,
            completed=True,
        )
        if reconciliation_required:
            changed, cancellation_received = await self._await_cleanup(transition)
            if changed:
                self._interrupted_tool_tasks.discard((run_id, task_id))
        else:
            await transition
        self._log_event(event_type, run_id, task_id)
        metadata = {"error_type": error_type} if error_type else None
        if reconciliation_required:
            _, interrupted = await self._await_cleanup(
                self._audit(
                    run_id,
                    "ERROR",
                    event_type,
                    safe_message,
                    task_id,
                    metadata,
                )
            )
            cancellation_received = cancellation_received or interrupted
            if cancellation_received:
                raise asyncio.CancelledError
        else:
            await self._audit(
                run_id,
                "ERROR",
                event_type,
                safe_message,
                task_id,
                metadata,
            )

    async def _await_cleanup(
        self,
        awaitable: Awaitable[Any],
    ) -> tuple[Any, bool]:
        """Finish a persistence cleanup despite repeated caller cancellation."""
        async def run_cleanup() -> Any:
            return await awaitable

        cleanup = asyncio.create_task(run_cleanup())
        cancellation_received = False
        while True:
            try:
                return await asyncio.shield(cleanup), cancellation_received
            except asyncio.CancelledError:
                cancellation_received = True
                if cleanup.done():
                    return cleanup.result(), cancellation_received

    async def _fail_expired_approval(self, run_id: str, task_id: str) -> None:
        await self._fail_task(
            run_id,
            task_id,
            "Approval expired before execution; the tool was not invoked.",
            expected_statuses=("PAUSED_APPROVAL",),
            event_type="APPROVAL_EXPIRED",
        )
        await self._skip_pending_after_failure(
            run_id,
            await self.repository.list_tasks(run_id),
        )
        await self.repository.update_run_status(
            run_id,
            "FAILED",
            error_message="A required approval expired.",
            completed=True,
        )
        self._release_run_cache(run_id)

    async def _result(
        self,
        run_id: str,
        pending_approvals: tuple[ApprovalRequest, ...] = (),
    ) -> ExecutionResult:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RecoveryError("The workflow run does not exist.")
        state = _RUN_DB_TO_STATE.get(str(run["status"]))
        if state is None:
            raise RecoveryError("The persisted workflow run has an unknown state.")
        task_states: dict[str, TaskState] = {}
        outputs: dict[str, Any] = {}
        errors: dict[str, str] = {}
        reconciliation_required: dict[str, bool] = {}
        for row in await self.repository.list_tasks(run_id):
            task_id = str(row["task_id"])
            task_state = _TASK_DB_TO_STATE.get(str(row["status"]))
            if task_state is None:
                raise RecoveryError("A persisted workflow task has an unknown state.")
            task_states[task_id] = task_state
            reconciliation_required[task_id] = bool(
                row.get("reconciliation_required")
            )
            if task_state == TaskState.COMPLETED:
                outputs[task_id] = redact_object(row.get("output_data_json"))
            if row.get("error_message"):
                errors[task_id] = redact_text(str(row["error_message"]))
        return ExecutionResult(
            run_id=run_id,
            state=state,
            task_states=task_states,
            outputs=outputs,
            errors=errors,
            pending_approvals=pending_approvals,
            reconciliation_required=reconciliation_required,
        )

    def _release_run_cache(self, run_id: str) -> None:
        self._workflow_cache.pop(run_id, None)
        self._output_cache.pop(run_id, None)
        self._interrupted_tool_tasks = {
            key for key in self._interrupted_tool_tasks if key[0] != run_id
        }

    async def _audit(
        self,
        run_id: str,
        level: str,
        event_type: str,
        message: str,
        task_id: str | None = None,
        metadata: Any = None,
    ) -> None:
        await self.repository.create_audit_log(
            run_id=run_id,
            task_id=task_id,
            level=level,
            event_type=event_type,
            message=message,
            metadata=redact_object(metadata) if metadata is not None else None,
        )

    def _log_event(
        self,
        event_type: str,
        run_id: str,
        task_id: str | None = None,
    ) -> None:
        self.logger.info(
            event_type.replace("_", " ").lower(),
            extra={
                "run_id": redact_text(run_id),
                "task_id": redact_text(task_id) if task_id else None,
                "event_type": event_type,
            },
        )

    def _breaker_for(self, identifier: str) -> CircuitBreaker:
        breaker = self.circuit_breakers.get(identifier)
        if breaker is None:
            breaker = CircuitBreaker(name=f"tool:{identifier}")
            self.circuit_breakers[identifier] = breaker
        return breaker

    @staticmethod
    def _fingerprint(
        run_id: str,
        task: TaskDefinition,
        arguments: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "run_id": run_id,
                "task_id": task.id,
                "tool_name": task.tool_name,
                "arguments": arguments,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _was_redacted(value: Any) -> bool:
        if redact_object(value) != value:
            return True
        try:
            serialized = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError, UnicodeError):
            return True
        return redact_text(serialized) != serialized

    @staticmethod
    def _cache_matches(
        cached: dict[str, Any] | None,
        run_id: str,
        task_id: str,
        tool_name: str,
    ) -> bool:
        return bool(
            cached
            and cached.get("run_id") == run_id
            and cached.get("task_id") == task_id
            and cached.get("tool_name") == tool_name
        )

    @staticmethod
    def _approval_expired(expires_at: Any) -> bool:
        try:
            expiry = datetime.fromisoformat(str(expires_at))
        except (TypeError, ValueError):
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expiry.astimezone(timezone.utc)

    @staticmethod
    def _task_from_workflow(
        workflow: WorkflowDefinition,
        task_id: str,
    ) -> TaskDefinition:
        task = next((item for item in workflow.tasks if item.id == task_id), None)
        if task is None:
            raise ApprovalTamperingException(
                "The approval task is not present in the stored workflow."
            )
        return task

    def _validate_approval_payload(
        self,
        payload: dict[str, Any],
        *,
        run_id: str,
        task: TaskDefinition,
    ) -> dict[str, Any]:
        if not task.requires_approval or not isinstance(payload, dict):
            raise ApprovalTamperingException(
                "The approval payload does not match the requested workflow task."
            )
        if (
            set(payload) != {"run_id", "task_id", "tool_name", "arguments"}
            or payload.get("run_id") != run_id
            or payload.get("task_id") != task.id
            or payload.get("tool_name") != task.tool_name
            or not isinstance(payload.get("arguments"), dict)
        ):
            raise ApprovalTamperingException(
                "The approval payload does not match the requested workflow task."
            )
        return payload["arguments"]

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        return f"{type(exc).__name__} occurred."
