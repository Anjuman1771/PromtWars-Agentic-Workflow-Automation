"""Registered tool invocation, retries, timeouts, and per-task approval gates."""

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..core.interpolation import interpolate_variables
from ..core.models import TaskDefinition, TaskExecutionContext, WorkflowDefinition
from ..safety.exceptions import (
    CircuitBreakerOpenException,
    DuplicateExecutionError,
    ExecutionLimitExceededException,
    RecoveryError,
    TaskTimeoutException,
    ToolValidationError,
    TransientToolExecutionError,
)
from .models import ApprovalRequest

if TYPE_CHECKING:
    from .engine import ExecutionEngine


class TaskRunner:
    """Run one ready task through the registry after validating its full input."""

    def __init__(self, engine: "ExecutionEngine") -> None:
        self.engine = engine

    async def execute(
        self,
        run_id: str,
        task: TaskDefinition,
        workflow: WorkflowDefinition,
        outputs: dict[str, Any],
        unavailable_outputs: set[str],
        input_override: tuple[dict[str, Any], BaseModel] | None,
        workflow_deadline: float,
        approval_requests: list[ApprovalRequest],
        allow_original_inputs: bool,
    ) -> Any:
        engine = self.engine
        task_row = await engine.repository.get_task(run_id, task.id)
        if task_row is None:
            raise RecoveryError("The persisted task record is missing.")
        if task_row.get("reconciliation_required"):
            raise RecoveryError(
                "This task has an uncertain external outcome and requires manual "
                "reconciliation; automatic retry is disabled."
            )
        if input_override is not None:
            arguments, validated_inputs = input_override
        else:
            if task_row.get("input_redacted") and not allow_original_inputs:
                raise RecoveryError(
                    "Task arguments were redacted in storage; the original input "
                    "is required to resume safely."
                )
            if self._uses_unavailable_output(task, unavailable_outputs):
                raise RecoveryError(
                    "A dependency output was redacted in storage; the original "
                    "input is required to resume safely."
                )
            context = TaskExecutionContext(
                workflow=workflow,
                task_id=task.id,
                task_outputs=outputs,
            )
            arguments = interpolate_variables(
                task.input_parameters,
                context,
                limits=engine.limits,
            )
            if not isinstance(arguments, dict):
                raise ToolValidationError("Tool inputs must resolve to an object.")
            validated_inputs = engine.tool_registry.validate_inputs(
                task.tool_name,
                arguments,
            )

        fingerprint = engine._fingerprint(run_id, task, arguments)
        stored_fingerprint = task_row.get("idempotency_key")
        if stored_fingerprint is not None and stored_fingerprint != fingerprint:
            raise RecoveryError(
                "Task inputs do not match the persisted idempotency fingerprint."
            )

        if task.requires_approval and input_override is None:
            return await self._request_approval(
                run_id,
                task,
                arguments,
                fingerprint,
                approval_requests,
            )

        cached = await engine.repository.get_cached_result(fingerprint)
        if cached is not None:
            if not engine._cache_matches(cached, run_id, task.id, task.tool_name):
                raise RecoveryError("The idempotency record does not match this task.")
            updated = await engine.repository.update_task_output(
                run_id,
                task.id,
                cached["output_json"],
                output_redacted=bool(cached.get("output_redacted")),
            )
            if updated is None:
                raise DuplicateExecutionError(
                    "The cached result could not be applied to the current task state."
                )
            return cached["output_json"]

        if not await engine.repository.claim_task(run_id, task.id, fingerprint):
            current = await engine.repository.get_task(run_id, task.id)
            if current is not None and current["status"] == "COMPLETED":
                return current.get("output_data_json")
            raise DuplicateExecutionError(
                "The task is no longer pending and cannot be invoked again."
            )

        engine._log_event("TASK_STARTED", run_id, task.id)
        await engine._audit(
            run_id,
            "INFO",
            "TASK_STARTED",
            "Task execution started.",
            task.id,
            {"tool_name": task.tool_name},
        )
        timeout_seconds = task.timeout_seconds or engine.limits.default_task_timeout_seconds
        task_deadline = asyncio.get_running_loop().time() + timeout_seconds
        retry_safe = engine.tool_registry.is_retry_safe(task.tool_name, validated_inputs)
        retry_count = 0

        try:
            while True:
                now = asyncio.get_running_loop().time()
                remaining = min(task_deadline, workflow_deadline) - now
                if remaining <= 0:
                    if workflow_deadline <= task_deadline:
                        raise ExecutionLimitExceededException(
                            "Workflow execution deadline exceeded."
                        )
                    raise TaskTimeoutException("Task execution timed out.")

                breaker = engine._breaker_for(task.tool_name)
                previous_state = breaker.state
                try:
                    result = await breaker.execute(
                        self._call_registered_tool,
                        run_id,
                        task.id,
                        task.tool_name,
                        validated_inputs,
                        remaining,
                    )
                except TimeoutError:
                    if workflow_deadline <= task_deadline:
                        raise ExecutionLimitExceededException(
                            "Workflow execution deadline exceeded."
                        ) from None
                    raise TaskTimeoutException("Task execution timed out.") from None
                except Exception as exc:
                    if (
                        retry_safe
                        and isinstance(
                            exc,
                            (
                                TransientToolExecutionError,
                                TimeoutError,
                                ConnectionError,
                            ),
                        )
                        and retry_count < task.retries
                        and (run_id, task.id)
                        not in engine._interrupted_tool_tasks
                    ):
                        retry_count += 1
                        retry_state_updated = await engine.repository.transition_task_status(
                            run_id,
                            task.id,
                            ("RUNNING",),
                            "RUNNING",
                            retry_count=retry_count,
                        )
                        if not retry_state_updated:
                            raise DuplicateExecutionError(
                                "The task changed state before its retry could start."
                            )
                        delay = min(
                            engine.retry_base_delay_seconds
                            * (2 ** (retry_count - 1)),
                            engine.retry_max_delay_seconds,
                        )
                        remaining = min(
                            task_deadline,
                            workflow_deadline,
                        ) - asyncio.get_running_loop().time()
                        if remaining <= delay:
                            raise TaskTimeoutException(
                                "The retry backoff would exceed the execution deadline."
                            ) from None
                        await engine._audit(
                            run_id,
                            "WARNING",
                            "TASK_RETRY",
                            "Retrying a transient failure for a retry-safe tool.",
                            task.id,
                            {"retry": retry_count, "delay_seconds": delay},
                        )
                        await engine.sleep(delay)
                        continue
                    raise
                finally:
                    if breaker.state != previous_state:
                        await engine._audit(
                            run_id,
                            "WARNING",
                            "CIRCUIT_BREAKER_STATE",
                            "Tool circuit breaker state changed.",
                            task.id,
                            {
                                "tool_name": task.tool_name,
                                "from_state": previous_state.value,
                                "to_state": breaker.state.value,
                            },
                        )

                output_redacted = engine._was_redacted(result)
                await engine.repository.save_execution_result(
                    idempotency_key=fingerprint,
                    run_id=run_id,
                    task_id=task.id,
                    tool_name=task.tool_name,
                    output_data=result,
                    output_redacted=output_redacted,
                )
                updated = await engine.repository.update_task_output(
                    run_id,
                    task.id,
                    result,
                    output_redacted=output_redacted,
                )
                if updated is None:
                    raise RecoveryError(
                        "The tool result could not be persisted to the current task state."
                    )
                engine._interrupted_tool_tasks.discard((run_id, task.id))
                engine._log_event("TASK_COMPLETED", run_id, task.id)
                await engine._audit(
                    run_id,
                    "INFO",
                    "TASK_COMPLETED",
                    "Task execution completed.",
                    task.id,
                    {"attempts": retry_count + 1},
                )
                return result
        except asyncio.CancelledError:
            key = (run_id, task.id)
            reconciliation_required = key in engine._interrupted_tool_tasks
            message = (
                "Tool invocation was interrupted before its result was durably "
                "recorded; external side effects may have occurred and require "
                "manual reconciliation."
                if reconciliation_required
                else "Task was cancelled while execution was in progress."
            )
            changed, _ = await engine._await_cleanup(
                engine.repository.transition_task_status(
                    run_id,
                    task.id,
                    ("RUNNING",),
                    "CANCELLED",
                    error_message=message,
                    reconciliation_required=reconciliation_required,
                    completed=True,
                )
            )
            if changed and reconciliation_required:
                engine._interrupted_tool_tasks.discard(key)
            elif reconciliation_required:
                task_state, _ = await engine._await_cleanup(
                    engine.repository.get_task(run_id, task.id)
                )
                if (
                    task_state is not None
                    and task_state["status"] == "COMPLETED"
                    and task_state.get("output_data_json") is not None
                    and not task_state.get("reconciliation_required")
                ):
                    engine._interrupted_tool_tasks.discard(key)
                    message = "Task output was durably persisted before cancellation."
            await engine._await_cleanup(
                engine._audit(
                    run_id,
                    "WARNING",
                    "TASK_CANCELLED",
                    message,
                    task.id,
                )
            )
            raise
        except Exception as exc:
            key = (run_id, task.id)
            reconciliation_required = key in engine._interrupted_tool_tasks
            if (
                isinstance(exc, CircuitBreakerOpenException)
                and not reconciliation_required
            ):
                await engine.repository.update_run_status(
                    run_id,
                    "RUNNING",
                    circuit_breaker_tripped=True,
                )
                await engine._audit(
                    run_id,
                    "ERROR",
                    "CIRCUIT_BREAKER_OPEN",
                    "Tool execution was blocked by an open circuit breaker.",
                    task.id,
                    {"tool_name": task.tool_name},
                )

            message = (
                "Tool invocation was interrupted before its result was durably "
                "recorded; external side effects may have occurred and require "
                "manual reconciliation."
                if reconciliation_required
                else engine._safe_error(exc)
            )
            await engine._fail_task(
                run_id,
                task.id,
                message,
                expected_statuses=("RUNNING",),
                event_type="TASK_FAILED",
                error_type=type(exc).__name__,
                reconciliation_required=reconciliation_required,
            )
            return None

    async def _request_approval(
        self,
        run_id: str,
        task: TaskDefinition,
        arguments: dict[str, Any],
        fingerprint: str,
        requests: list[ApprovalRequest],
    ) -> None:
        engine = self.engine
        latest = await engine.repository.get_latest_approval_for_task(run_id, task.id)
        if latest is not None:
            status = str(latest["status"])
            if status == "PENDING":
                if engine._approval_expired(latest.get("expires_at")):
                    await engine._fail_task(
                        run_id,
                        task.id,
                        "Approval expired before execution; the tool was not invoked.",
                        expected_statuses=("PENDING",),
                        event_type="TASK_FAILED",
                    )
                else:
                    await engine.repository.transition_task_status(
                        run_id,
                        task.id,
                        ("PENDING",),
                        "PAUSED_APPROVAL",
                    )
                return None
            if status == "APPROVED":
                await engine._fail_task(
                    run_id,
                    task.id,
                    "Approval was consumed before execution resumed; manual "
                    "reconciliation is required.",
                    expected_statuses=("PENDING",),
                    event_type="TASK_FAILED",
                )
                return None
            if status == "REJECTED":
                await engine._fail_task(
                    run_id,
                    task.id,
                    "Approval was rejected; the tool was not invoked.",
                    expected_statuses=("PENDING",),
                    event_type="TASK_FAILED",
                )
                return None

        if not await engine.repository.set_task_idempotency_key(
            run_id,
            task.id,
            fingerprint,
        ):
            raise RecoveryError("Could not persist the task approval fingerprint.")
        action_payload = {
            "run_id": run_id,
            "task_id": task.id,
            "tool_name": task.tool_name,
            "arguments": arguments,
        }
        issued = await engine.approval_manager.issue_token(
            run_id,
            task.id,
            action_payload,
        )
        changed = await engine.repository.transition_task_status(
            run_id,
            task.id,
            ("PENDING",),
            "PAUSED_APPROVAL",
        )
        if not changed:
            raise RecoveryError("The approval request could not be persisted on its task.")
        request = ApprovalRequest(
            approval_id=str(issued["approval_id"]),
            run_id=run_id,
            task_id=task.id,
            tool_name=task.tool_name,
            expires_at=str(issued["expires_at"]),
            raw_token=str(issued["raw_token"]),
            action_payload=action_payload,
        )
        requests.append(request)
        engine._log_event("APPROVAL_REQUESTED", run_id, task.id)
        await engine._audit(
            run_id,
            "INFO",
            "APPROVAL_REQUESTED",
            "Task is waiting for human approval.",
            task.id,
            {"approval_id": request.approval_id, "expires_at": request.expires_at},
        )
        return None

    async def _call_registered_tool(
        self,
        run_id: str,
        task_id: str,
        identifier: str,
        validated_inputs: BaseModel,
        timeout_seconds: float,
    ) -> Any:
        key = (run_id, task_id)
        self.engine._interrupted_tool_tasks.add(key)
        worker = asyncio.create_task(
            asyncio.to_thread(
                self.engine.tool_registry.execute_validated,
                identifier,
                validated_inputs,
            )
        )
        self.engine._tool_workers.add(worker)
        worker.add_done_callback(self._consume_tool_worker)
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await asyncio.shield(worker)
                if inspect.isawaitable(result):
                    return await result
                return result
        except (asyncio.CancelledError, TimeoutError):
            self.engine._interrupted_tool_tasks.add(key)
            raise

    def _consume_tool_worker(self, worker: asyncio.Task[Any]) -> None:
        self.engine._tool_workers.discard(worker)
        if not worker.cancelled():
            worker.exception()

    @staticmethod
    def _uses_unavailable_output(
        task: TaskDefinition,
        unavailable_outputs: set[str],
    ) -> bool:
        def contains_reference(value: Any, task_id: str) -> bool:
            if isinstance(value, str):
                return f"{{{{tasks.{task_id}.output." in value
            if isinstance(value, dict):
                return any(contains_reference(item, task_id) for item in value.values())
            if isinstance(value, list):
                return any(contains_reference(item, task_id) for item in value)
            return False

        dependency_ids = {dependency.task_id for dependency in task.dependencies}
        return any(
            dependency_id in dependency_ids
            and contains_reference(task.input_parameters, dependency_id)
            for dependency_id in unavailable_outputs
        )
