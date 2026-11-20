"""Fail-closed recovery for persisted workflow runs."""

from typing import TYPE_CHECKING, Any

from ..core.models import WorkflowDefinition
from ..safety.exceptions import RecoveryError
from .models import ExecutionResult

if TYPE_CHECKING:
    from .engine import ExecutionEngine


async def recover_interrupted_runs(
    engine: "ExecutionEngine",
) -> list[ExecutionResult]:
    """Reconcile persisted work once at startup without repeating ambiguous calls."""
    runs: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = await engine.repository.list_runs(
            status=None,
            limit=500,
            offset=offset,
        )
        runs.extend(batch)
        if len(batch) < 500:
            break
        offset += len(batch)

    recovered: list[ExecutionResult] = []
    for run in runs:
        run_id = str(run["id"])
        if run["status"] not in {"PENDING", "RUNNING", "PAUSED_APPROVAL"}:
            continue
        if run_id in engine._active_runs:
            continue
        workflow = await load_workflow(engine, run_id)
        engine.validator.validate(workflow)
        engine.tool_registry.validate_workflow(workflow)
        for task_row in await engine.repository.list_tasks(run_id):
            if task_row["status"] == "RUNNING":
                await recover_running_task(engine, run_id, task_row)
        await reconcile_pending_approvals(engine, run_id)

        task_rows = await engine.repository.list_tasks(run_id)
        statuses = {str(item["status"]) for item in task_rows}
        if "FAILED" in statuses or "CANCELLED" in statuses:
            await engine._skip_pending_after_failure(run_id, task_rows)
            task_rows = await engine.repository.list_tasks(run_id)
            statuses = {str(item["status"]) for item in task_rows}
            if "CANCELLED" in statuses:
                await engine.repository.update_run_status(
                    run_id,
                    "CANCELLED",
                    error_message="Interrupted run was cancelled.",
                    completed=True,
                )
                recovered.append(await engine._result(run_id))
                continue
            await engine.repository.update_run_status(
                run_id,
                "FAILED",
                error_message="Interrupted work requires reconciliation.",
                completed=True,
            )
            engine._log_event("RUN_FAILED", run_id)
            recovered.append(await engine._result(run_id))
            continue
        if "PAUSED_APPROVAL" in statuses:
            await engine.repository.update_run_status(run_id, "PAUSED_APPROVAL")
            await engine._audit(
                run_id,
                "INFO",
                "RECOVERY_WAITING_APPROVAL",
                "Recovered run remains paused for approval.",
            )
            recovered.append(await engine._result(run_id))
            continue
        if statuses and statuses.issubset({"COMPLETED", "SKIPPED"}):
            await engine.repository.update_run_status(
                run_id, "COMPLETED", completed=True
            )
            recovered.append(await engine._result(run_id))
            continue

        await engine.repository.update_run_status(run_id, "PENDING")
        await engine._audit(
            run_id,
            "INFO",
            "RECOVERY_RESUMED",
            "Recovered safe pending work; no ambiguous task will be invoked.",
        )
        recovered.append(await engine.resume(run_id))
    return recovered


async def recover_running_task(
    engine: "ExecutionEngine",
    run_id: str,
    task_row: dict[str, Any],
) -> None:
    """Use a durable result if available; otherwise require manual reconciliation."""
    task_id = str(task_row["task_id"])
    if task_row.get("reconciliation_required"):
        await engine._fail_task(
            run_id,
            task_id,
            "Task outcome is marked uncertain and requires manual reconciliation; "
            "recovery will not invoke it again.",
            expected_statuses=("RUNNING",),
            event_type="RECOVERY_RECONCILIATION_REQUIRED",
            reconciliation_required=True,
        )
        return
    key = task_row.get("idempotency_key")
    cached = await engine.repository.get_cached_result(key) if key else None
    if engine._cache_matches(cached, run_id, task_id, task_row["tool_name"]):
        await engine.repository.update_task_output(
            run_id,
            task_id,
            cached["output_json"],
            output_redacted=bool(cached.get("output_redacted")),
        )
        await engine._audit(
            run_id,
            "INFO",
            "RECOVERY_CACHED_RESULT",
            "Recovered a completed result from the idempotency store.",
            task_id,
        )
        return
    await engine._fail_task(
        run_id,
        task_id,
        "Execution was interrupted after invocation began; automatic retry "
        "is unsafe and requires reconciliation.",
        expected_statuses=("RUNNING",),
        event_type="RECOVERY_AMBIGUOUS_TASK",
        reconciliation_required=True,
    )


async def reconcile_pending_approvals(
    engine: "ExecutionEngine",
    run_id: str,
    approved_task_ids: set[str] | None = None,
) -> None:
    """Preserve valid pending approvals and fail closed on missing/consumed tokens."""
    approved_task_ids = approved_task_ids or set()
    for task_row in await engine.repository.list_tasks(run_id):
        if task_row["status"] not in {"PENDING", "PAUSED_APPROVAL"}:
            continue
        if not task_row.get("requires_approval"):
            continue
        task_id = str(task_row["task_id"])
        if task_id in approved_task_ids:
            continue
        approval = await engine.repository.get_latest_approval_for_task(
            run_id, task_id
        )
        if approval is None:
            if task_row["status"] == "PAUSED_APPROVAL":
                await engine._fail_task(
                    run_id,
                    task_id,
                    "Approval state is missing; the tool was not invoked.",
                    expected_statuses=("PAUSED_APPROVAL",),
                    event_type="RECOVERY_MISSING_APPROVAL",
                )
            continue
        approval_status = str(approval["status"])
        if approval_status == "PENDING" and not engine._approval_expired(
            approval.get("expires_at")
        ):
            if task_row["status"] == "PENDING":
                await engine.repository.transition_task_status(
                    run_id,
                    task_id,
                    ("PENDING",),
                    "PAUSED_APPROVAL",
                )
            continue

        if approval_status == "REJECTED":
            message = "Approval was rejected; the tool was not invoked."
            event_type = "APPROVAL_REJECTED"
        elif approval_status == "APPROVED":
            message = (
                "Approval was consumed before execution resumed; automatic "
                "execution is unsafe and requires reconciliation."
            )
            event_type = "RECOVERY_APPROVED_WITHOUT_INPUT"
        else:
            message = "Approval expired before execution; the tool was not invoked."
            event_type = "APPROVAL_EXPIRED"
        await engine._fail_task(
            run_id,
            task_id,
            message,
            expected_statuses=("PENDING", "PAUSED_APPROVAL"),
            event_type=event_type,
        )


async def load_workflow(
    engine: "ExecutionEngine",
    run_id: str,
) -> WorkflowDefinition:
    run = await engine.repository.get_run(run_id)
    if run is None:
        raise RecoveryError("The workflow run does not exist.")
    workflow_record = await engine.repository.get_workflow(str(run["workflow_id"]))
    if workflow_record is None:
        raise RecoveryError("The persisted workflow definition does not exist.")
    definition = workflow_record.get("definition_json")
    if not isinstance(definition, dict):
        raise RecoveryError("The persisted workflow definition is invalid.")
    try:
        return WorkflowDefinition.model_validate(definition)
    except Exception as exc:
        raise RecoveryError(
            "The persisted workflow definition cannot be restored safely."
        ) from exc


async def load_outputs(
    engine: "ExecutionEngine",
    run_id: str,
) -> tuple[dict[str, Any], set[str]]:
    cached = engine._output_cache.get(run_id)
    if cached is not None:
        return dict(cached), set()
    outputs: dict[str, Any] = {}
    unavailable: set[str] = set()
    for row in await engine.repository.list_tasks(run_id):
        if row["status"] != "COMPLETED":
            continue
        task_id = str(row["task_id"])
        outputs[task_id] = row.get("output_data_json") or {}
        if row.get("output_redacted"):
            unavailable.add(task_id)
    return outputs, unavailable
