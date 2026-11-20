"""Deterministic tests for persistent workflow execution and recovery."""

import asyncio
import sqlite3
import threading

import pytest
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from backend.app.core.models import WorkflowDefinition
from backend.app.execution import ExecutionEngine, RunState, TaskState
from backend.app.safety.approval import ApprovalTokenManager, hash_payload, hash_token
from backend.app.safety.circuit_breaker import CircuitBreaker
from backend.app.safety.exceptions import (
    ApprovalConsumedException,
    ApprovalExpiredException,
    ApprovalTamperingException,
    CircuitBreakerOpenException,
    DuplicateExecutionError,
    RecoveryError,
    ToolNotFoundError,
    TransientToolExecutionError,
)
from backend.app.safety.limits import ExecutionLimits
from backend.app.safety.redaction import REDACTED_SUBSTITUTE
from backend.app.storage.database import Database, DatabaseError, SCHEMA_FILE
from backend.app.storage.repository import WorkflowRepository
from backend.app.tools.registry import ToolRegistry


class ValueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: StrictInt


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: StrictStr


@pytest.fixture
async def repository(tmp_path):
    database = Database(str(tmp_path / "execution.db"))
    await database.init_db()
    return WorkflowRepository(database)


def build_engine(
    repository,
    registry,
    *,
    limits=None,
    breakers=None,
):
    return ExecutionEngine(
        repository=repository,
        tool_registry=registry,
        approval_manager=ApprovalTokenManager(repository, ttl_seconds=60),
        limits=limits
        or ExecutionLimits(
            max_workflow_steps=30,
            max_run_time_seconds=10,
            default_task_timeout_seconds=2,
            max_concurrent_tasks=5,
            max_retries_per_task=3,
        ),
        circuit_breakers=breakers,
        retry_base_delay_seconds=0.001,
        retry_max_delay_seconds=0.002,
        sleep=_no_sleep,
    )


async def _no_sleep(_):
    return None


def value_workflow(tasks):
    return WorkflowDefinition(name="Execution test", tasks=tasks)


@pytest.mark.asyncio
async def test_sequential_tasks_resolve_only_declared_dependency_outputs(repository):
    calls = []
    registry = ToolRegistry()

    def double(request):
        calls.append(("first", request.value))
        return {"value": request.value * 2}

    def collect(request):
        calls.append(("second", request.value))
        return {"value": request.value}

    registry.register("double", ValueInput, double)
    registry.register("collect", ValueInput, collect)
    workflow = value_workflow(
        [
            {
                "id": "first",
                "name": "First",
                "tool": "double",
                "inputs": {"value": 4},
                "outputs": ["value"],
            },
            {
                "id": "second",
                "name": "Second",
                "tool": "collect",
                "inputs": {"value": "{{tasks.first.output.value}}"},
                "dependencies": ["first"],
            },
        ]
    )

    result = await build_engine(repository, registry).execute(workflow)

    assert result.state == RunState.COMPLETED
    assert calls == [("first", 4), ("second", 8)]
    assert result.outputs == {
        "first": {"value": 8},
        "second": {"value": 8},
    }


@pytest.mark.asyncio
async def test_independent_tasks_run_concurrently_within_configured_limit(repository):
    active = 0
    peak = 0
    two_active = asyncio.Event()
    registry = ToolRegistry()

    async def work(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            two_active.set()
        try:
            await asyncio.wait_for(two_active.wait(), timeout=0.1)
            return {"value": 1}
        finally:
            active -= 1

    registry.register("work", ValueInput, work)
    limits = ExecutionLimits(
        max_workflow_steps=10,
        max_run_time_seconds=10,
        default_task_timeout_seconds=2,
        max_concurrent_tasks=2,
        max_retries_per_task=1,
    )
    workflow = value_workflow(
        [
            {"id": f"task{i}", "name": f"Task {i}", "tool": "work", "inputs": {"value": i}}
            for i in range(5)
        ]
    )

    result = await build_engine(repository, registry, limits=limits).execute(workflow)

    assert result.state == RunState.COMPLETED
    assert peak == 2
    assert all(state == TaskState.COMPLETED for state in result.task_states.values())


@pytest.mark.asyncio
async def test_failed_task_skips_dependents_and_stops_new_scheduling(repository):
    calls = []
    registry = ToolRegistry()

    def fail(_):
        calls.append("fail")
        raise RuntimeError("private implementation detail")

    registry.register("work", ValueInput, fail)
    limits = ExecutionLimits(
        max_workflow_steps=10,
        max_run_time_seconds=10,
        default_task_timeout_seconds=2,
        max_concurrent_tasks=1,
        max_retries_per_task=1,
    )
    workflow = value_workflow(
        [
            {"id": "bad", "name": "Bad", "tool": "work", "inputs": {"value": 1}},
            {"id": "independent", "name": "Independent", "tool": "work", "inputs": {"value": 4}},
            {
                "id": "dependent",
                "name": "Dependent",
                "tool": "work",
                "inputs": {"value": 2},
                "dependencies": ["bad"],
            },
            {
                "id": "later",
                "name": "Later",
                "tool": "work",
                "inputs": {"value": 3},
                "dependencies": ["bad"],
            },
        ]
    )

    result = await build_engine(repository, registry, limits=limits).execute(workflow)

    assert result.state == RunState.FAILED
    assert result.task_states == {
        "bad": TaskState.FAILED,
        "independent": TaskState.SKIPPED,
        "dependent": TaskState.SKIPPED,
        "later": TaskState.SKIPPED,
    }
    assert calls == ["fail"]
    assert "private implementation detail" not in result.errors["bad"]


@pytest.mark.asyncio
async def test_unknown_tools_and_invalid_arguments_never_invoke_handlers(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    engine = build_engine(repository, registry)

    with pytest.raises(ToolNotFoundError):
        await engine.execute(
            value_workflow(
                [{"id": "task", "name": "Task", "tool": "shell", "inputs": {}}]
            )
        )

    result = await engine.execute(
        value_workflow(
            [
                {
                    "id": "badargs",
                    "name": "Bad arguments",
                    "tool": "work",
                    "inputs": {"value": 1, "unexpected": True},
                }
            ]
        )
    )

    assert result.state == RunState.FAILED
    assert result.task_states["badargs"] == TaskState.FAILED
    assert calls == []


@pytest.mark.asyncio
async def test_approval_is_persisted_bound_and_resumed_only_after_approval(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value) or {"value": request.value})
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [
            {
                "id": "guarded",
                "name": "Guarded",
                "tool": "work",
                "inputs": {"value": 9},
                "requires_approval": True,
            }
        ]
    )

    waiting = await engine.execute(workflow, run_id="approval_run")
    request = waiting.pending_approvals[0]
    stored = await repository.get_approval(hash_token(request.raw_token))

    assert waiting.state == RunState.WAITING_APPROVAL
    assert waiting.task_states["guarded"] == TaskState.WAITING_APPROVAL
    assert calls == []
    assert stored["token"] == hash_token(request.raw_token)
    assert stored["action_payload_json"] == {}
    assert stored["payload_hash"] == hash_payload(request.action_payload)
    assert request.raw_token not in stored["token"]
    assert request.raw_token not in repr(request)

    completed = await engine.decide_approval(
        waiting.run_id,
        request.task_id,
        request.raw_token,
        request.action_payload,
    )

    assert completed.state == RunState.COMPLETED
    assert calls == [9]
    assert completed.outputs["guarded"] == {"value": 9}
    with pytest.raises(ApprovalConsumedException):
        await engine.approval_manager.verify_and_consume(
            request.raw_token,
            expected_payload=request.action_payload,
        )
    with pytest.raises(DuplicateExecutionError):
        await engine.execute(workflow, run_id="approval_run")


@pytest.mark.asyncio
async def test_approval_rejection_and_payload_tampering_never_invoke(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [
            {
                "id": "guarded",
                "name": "Guarded",
                "tool": "work",
                "inputs": {"value": 9},
                "requires_approval": True,
            }
        ]
    )
    waiting = await engine.execute(workflow, run_id="tamper_run")
    request = waiting.pending_approvals[0]
    modified = {
        **request.action_payload,
        "arguments": {"value": 10},
    }

    with pytest.raises(ApprovalTamperingException):
        await engine.decide_approval(
            waiting.run_id,
            request.task_id,
            request.raw_token,
            modified,
        )
    assert calls == []
    assert (await repository.get_task(waiting.run_id, "guarded"))["status"] == "PAUSED_APPROVAL"

    rejected = await engine.decide_approval(
        waiting.run_id,
        request.task_id,
        request.raw_token,
        request.action_payload,
        decision="REJECT",
    )
    assert rejected.state == RunState.FAILED
    assert rejected.task_states["guarded"] == TaskState.FAILED
    assert calls == []


@pytest.mark.asyncio
async def test_expired_approval_fails_closed(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    engine = build_engine(repository, registry)
    waiting = await engine.execute(
        value_workflow(
            [
                {
                    "id": "guarded",
                    "name": "Guarded",
                    "tool": "work",
                    "inputs": {"value": 1},
                    "requires_approval": True,
                }
            ]
        ),
        run_id="expired_run",
    )
    request = waiting.pending_approvals[0]
    token_hash = hash_token(request.raw_token)
    async with repository.db.connection() as conn:
        await conn.execute(
            "UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token = ?;",
            (token_hash,),
        )
        await conn.commit()

    with pytest.raises(ApprovalExpiredException):
        await engine.decide_approval(
            waiting.run_id,
            request.task_id,
            request.raw_token,
            request.action_payload,
        )

    assert calls == []
    assert (await repository.get_task(waiting.run_id, "guarded"))["status"] == "FAILED"
    assert (await repository.get_run(waiting.run_id))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_approval_survives_engine_restart_when_caller_retains_payload(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value) or {"value": request.value})
    workflow = value_workflow(
        [
            {
                "id": "guarded",
                "name": "Guarded",
                "tool": "work",
                "inputs": {"value": 4},
                "requires_approval": True,
            }
        ]
    )
    first_engine = build_engine(repository, registry)
    waiting = await first_engine.execute(workflow, run_id="restart_approval")
    second_engine = build_engine(repository, registry)

    recovered = await second_engine.recover_interrupted_runs()
    assert recovered[0].state == RunState.WAITING_APPROVAL

    completed = await second_engine.decide_approval(
        waiting.run_id,
        waiting.pending_approvals[0].task_id,
        waiting.pending_approvals[0].raw_token,
        waiting.pending_approvals[0].action_payload,
    )
    assert completed.state == RunState.COMPLETED
    assert calls == [4]


@pytest.mark.asyncio
async def test_dispatched_transient_failure_is_not_retried_even_if_retry_safe(
    repository,
):
    calls = 0
    registry = ToolRegistry()

    def flaky(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientToolExecutionError("temporary network failure")
        return {"value": 7}

    registry.register("flaky", ValueInput, flaky, retry_safe=True)
    workflow = value_workflow(
        [
            {
                "id": "retry",
                "name": "Retry",
                "tool": "flaky",
                "inputs": {"value": 7},
                "retries": 2,
            }
        ]
    )

    result = await build_engine(repository, registry).execute(workflow)

    assert result.state == RunState.FAILED
    assert result.reconciliation_required["retry"] is True
    assert calls == 1
    task = await repository.get_task(result.run_id, "retry")
    assert task["retry_count"] == 0
    assert task["reconciliation_required"] == 1
    audit = await repository.list_audit_logs(result.run_id)
    assert not any(row["event_type"] == "TASK_RETRY" for row in audit)


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_tool_invocation(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    breaker = CircuitBreaker(name="blocked", failure_threshold=1)
    await breaker.trip()
    engine = build_engine(repository, registry, breakers={"work": breaker})

    result = await engine.execute(
        value_workflow(
            [{"id": "blocked", "name": "Blocked", "tool": "work", "inputs": {"value": 1}}]
        )
    )

    assert result.state == RunState.FAILED
    assert calls == []
    assert (await repository.get_run(result.run_id))["circuit_breaker_tripped"] == 1


@pytest.mark.asyncio
async def test_task_timeout_and_workflow_deadline_fail_without_retry(repository):
    calls = []
    registry = ToolRegistry()

    async def slow(request):
        calls.append(request.value)
        await asyncio.sleep(1.2)
        return {"value": request.value}

    registry.register("slow", ValueInput, slow, retry_safe=True)
    engine = build_engine(
        repository,
        registry,
        limits=ExecutionLimits(
            max_workflow_steps=10,
            max_run_time_seconds=5,
            default_task_timeout_seconds=1,
            max_concurrent_tasks=2,
            max_retries_per_task=2,
        ),
    )
    task_result = await engine.execute(
        value_workflow(
            [
                {
                    "id": "timeout",
                    "name": "Timeout",
                    "tool": "slow",
                    "inputs": {"value": 1},
                    "retries": 2,
                    "timeout_seconds": 1,
                }
            ]
        )
    )
    assert task_result.state == RunState.FAILED
    assert calls == [1]

    deadline_engine = build_engine(repository, registry)

    async def short_deadline(_):
        return asyncio.get_running_loop().time() + 0.8

    deadline_engine._workflow_deadline = short_deadline
    deadline_result = await deadline_engine.execute(
        value_workflow(
            [
                {
                    "id": "deadline",
                    "name": "Deadline",
                    "tool": "slow",
                    "inputs": {"value": 2},
                    "timeout_seconds": 2,
                }
            ]
        )
    )
    assert deadline_result.state == RunState.FAILED
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_cancellation_persists_terminal_states(repository):
    started = asyncio.Event()
    registry = ToolRegistry()

    async def wait_for_cancel(_):
        started.set()
        await asyncio.Event().wait()

    registry.register("wait", ValueInput, wait_for_cancel)
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [{"id": "wait", "name": "Wait", "tool": "wait", "inputs": {"value": 1}}]
    )
    run_task = asyncio.create_task(engine.execute(workflow, run_id="cancel_run"))
    await started.wait()

    result = await engine.cancel("cancel_run")

    assert result.state == RunState.CANCELLED
    assert result.task_states["wait"] == TaskState.CANCELLED
    assert run_task.cancelled()


@pytest.mark.asyncio
async def test_timed_out_sync_worker_is_marked_ambiguous_without_retry(repository):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    effects = []
    registry = ToolRegistry()

    def blocking_work(request):
        started.set()
        release.wait(timeout=5)
        effects.append(request.value)
        finished.set()
        return {"value": request.value}

    registry.register("work", ValueInput, blocking_work, retry_safe=True)
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [
            {
                "id": "blocking",
                "name": "Blocking",
                "tool": "work",
                "inputs": {"value": 4},
                "timeout_seconds": 1,
                "retries": 2,
            }
        ]
    )
    run_task = asyncio.create_task(engine.execute(workflow, run_id="sync_timeout"))
    assert await asyncio.to_thread(started.wait, 2)

    try:
        result = await asyncio.wait_for(run_task, timeout=2)
        assert result.state == RunState.FAILED
        assert result.reconciliation_required["blocking"] is True
        assert "manual reconciliation" in result.errors["blocking"]
        assert effects == []
    finally:
        release.set()

    assert await asyncio.to_thread(finished.wait, 2)
    assert effects == [4]
    task_row = await repository.get_task("sync_timeout", "blocking")
    assert task_row["status"] == "FAILED"
    assert task_row["reconciliation_required"] == 1


@pytest.mark.asyncio
async def test_cancelled_sync_worker_is_reported_for_manual_reconciliation(repository):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    effects = []
    registry = ToolRegistry()

    def blocking_work(request):
        started.set()
        release.wait(timeout=5)
        effects.append(request.value)
        finished.set()
        return {"value": request.value}

    registry.register("work", ValueInput, blocking_work)
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [
            {
                "id": "blocking",
                "name": "Blocking",
                "tool": "work",
                "inputs": {"value": 5},
            }
        ]
    )
    run_task = asyncio.create_task(engine.execute(workflow, run_id="sync_cancel"))
    assert await asyncio.to_thread(started.wait, 2)

    try:
        result = await engine.cancel("sync_cancel")
        assert result.state == RunState.CANCELLED
        assert result.reconciliation_required["blocking"] is True
        assert "manual reconciliation" in result.errors["blocking"]
        assert effects == []
    finally:
        release.set()

    assert run_task.cancelled()
    assert await asyncio.to_thread(finished.wait, 2)
    assert effects == [5]
    task_row = await repository.get_task("sync_cancel", "blocking")
    assert task_row["status"] == "CANCELLED"
    assert task_row["reconciliation_required"] == 1
    assert task_row["output_data_json"] is None
    assert (
        await repository.update_task_output(
            "sync_cancel", "blocking", {"value": 5}
        )
        is None
    )
    task_row = await repository.get_task("sync_cancel", "blocking")
    assert task_row["status"] == "CANCELLED"
    assert task_row["reconciliation_required"] == 1
    assert task_row["output_data_json"] is None


@pytest.mark.asyncio
async def test_worker_completion_before_timeout_handling_is_still_uncertain(
    repository, monkeypatch
):
    calls = []
    finished = threading.Event()
    registry = ToolRegistry()

    def complete(request):
        calls.append(request.value)
        finished.set()
        return {"value": request.value}

    registry.register("work", ValueInput, complete, retry_safe=True)
    engine = build_engine(repository, registry)

    class TimeoutAfterWorker:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            assert _exc_type is None
            assert finished.is_set()
            raise TimeoutError

    monkeypatch.setattr(
        "backend.app.execution.task_runner.asyncio.timeout",
        lambda _: TimeoutAfterWorker(),
    )
    result = await engine.execute(
        value_workflow(
            [
                {
                    "id": "timeout_race",
                    "name": "Timeout race",
                    "tool": "work",
                    "inputs": {"value": 8},
                    "retries": 2,
                }
            ]
        ),
        run_id="timeout_race",
    )

    assert result.state == RunState.FAILED
    assert result.reconciliation_required["timeout_race"] is True
    assert calls == [8]
    task_row = await repository.get_task("timeout_race", "timeout_race")
    assert task_row["status"] == "FAILED"
    assert task_row["reconciliation_required"] == 1


@pytest.mark.asyncio
async def test_worker_completion_before_cancellation_handling_is_still_uncertain(
    repository, monkeypatch
):
    calls = []
    finished = threading.Event()
    registry = ToolRegistry()

    def complete(request):
        calls.append(request.value)
        finished.set()
        return {"value": request.value}

    registry.register("work", ValueInput, complete)
    engine = build_engine(repository, registry)

    class CancelAfterWorker:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            assert _exc_type is None
            assert finished.is_set()
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "backend.app.execution.task_runner.asyncio.timeout",
        lambda _: CancelAfterWorker(),
    )
    run_task = asyncio.create_task(
        engine.execute(
            value_workflow(
                [
                    {
                        "id": "cancel_race",
                        "name": "Cancel race",
                        "tool": "work",
                        "inputs": {"value": 9},
                    }
                ]
            ),
            run_id="cancel_race",
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await run_task

    result = await engine._result("cancel_race")
    task_row = await repository.get_task("cancel_race", "cancel_race")
    assert result.task_states["cancel_race"] == TaskState.CANCELLED
    assert result.reconciliation_required["cancel_race"] is True
    assert task_row["reconciliation_required"] == 1
    assert calls == [9]


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_reconciliation_persistence(repository):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    registry = ToolRegistry()

    def blocking_work(_):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return {"value": 1}

    registry.register("work", ValueInput, blocking_work)
    engine = build_engine(repository, registry)
    run_task = asyncio.create_task(
        engine.execute(
            value_workflow(
                [{"id": "repeated", "name": "Repeated", "tool": "work", "inputs": {"value": 1}}]
            ),
            run_id="repeated_cancel",
        )
    )
    assert await asyncio.to_thread(started.wait, 2)

    transition_entered = asyncio.Event()
    allow_transition = asyncio.Event()
    original_transition = repository.transition_task_status

    async def delayed_transition(*args, **kwargs):
        if kwargs.get("reconciliation_required"):
            transition_entered.set()
            await allow_transition.wait()
        return await original_transition(*args, **kwargs)

    repository.transition_task_status = delayed_transition
    cancel_task = asyncio.create_task(engine.cancel("repeated_cancel"))
    await transition_entered.wait()
    run_task.cancel()
    await asyncio.sleep(0)
    allow_transition.set()
    result = await cancel_task
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    if engine._tool_workers:
        await asyncio.gather(*tuple(engine._tool_workers), return_exceptions=True)

    task_row = await repository.get_task("repeated_cancel", "repeated")
    assert result.reconciliation_required["repeated"] is True
    assert task_row["status"] == "CANCELLED"
    assert task_row["reconciliation_required"] == 1


@pytest.mark.asyncio
async def test_reconciliation_flag_persists_across_database_connections(tmp_path):
    path = str(tmp_path / "reconciliation.db")
    database = Database(path)
    await database.init_db()
    repository = WorkflowRepository(database)
    workflow = await repository.create_workflow(
        "reconciliation_workflow", "Reconciliation", "prompt", {}
    )
    await repository.create_run("reconciliation_run", workflow["id"])
    await repository.create_task(
        "reconciliation_run", "uncertain", "Uncertain", "work"
    )
    changed = await repository.transition_task_status(
        "reconciliation_run",
        "uncertain",
        ("PENDING",),
        "FAILED",
        error_message="External outcome requires reconciliation.",
        reconciliation_required=True,
        completed=True,
    )
    assert changed

    reopened = WorkflowRepository(Database(path))
    task_row = await reopened.get_task("reconciliation_run", "uncertain")
    assert task_row["status"] == "FAILED"
    assert task_row["reconciliation_required"] == 1


@pytest.mark.asyncio
async def test_recovery_and_retry_do_not_invoke_reconciliation_required_task(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    workflow = value_workflow(
        [{"id": "uncertain", "name": "Uncertain", "tool": "work", "inputs": {"value": 2}}]
    )
    stored = await repository.create_workflow(
        "uncertain_workflow",
        "Uncertain",
        "prompt",
        workflow.model_dump(mode="json", exclude_none=True),
    )
    await repository.create_run(
        "uncertain_run", stored["id"], status="RUNNING"
    )
    await repository.create_task(
        "uncertain_run",
        "uncertain",
        "Uncertain",
        "work",
        status="FAILED",
        input_params={"value": 2},
        reconciliation_required=True,
    )
    engine = build_engine(repository, registry)

    recovered = await engine.recover_interrupted_runs()
    assert recovered[0].reconciliation_required["uncertain"] is True
    assert calls == []

    with pytest.raises(RecoveryError, match="manual reconciliation"):
        await engine.task_runner.execute(
            "uncertain_run",
            workflow.tasks[0],
            workflow,
            outputs={},
            unavailable_outputs=set(),
            input_override=None,
            workflow_deadline=asyncio.get_running_loop().time() + 5,
            approval_requests=[],
            allow_original_inputs=True,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_tool_exception_without_side_effect_free_contract_is_uncertain(repository):
    registry = ToolRegistry()

    def fail(_):
        raise ValueError("tool operation failed")

    registry.register("work", ValueInput, fail)
    result = await build_engine(repository, registry).execute(
        value_workflow(
            [{"id": "confirmed", "name": "Confirmed", "tool": "work", "inputs": {"value": 1}}]
        )
    )

    assert result.state == RunState.FAILED
    assert result.reconciliation_required["confirmed"] is True
    assert "manual reconciliation" in result.errors["confirmed"]
    task_row = await repository.get_task(result.run_id, "confirmed")
    assert task_row["status"] == "FAILED"
    assert task_row["reconciliation_required"] == 1


@pytest.mark.asyncio
async def test_sync_side_effect_then_transient_exception_is_not_retried_or_recovered(
    repository,
):
    calls = []
    effects = []
    registry = ToolRegistry()

    def perform_side_effect(request):
        calls.append(request.value)
        effects.append(f"effect:{request.value}")
        raise TransientToolExecutionError("The response failed after the effect.")

    registry.register("work", ValueInput, perform_side_effect)
    engine = build_engine(repository, registry)
    workflow = value_workflow(
        [
            {
                "id": "uncertain_exception",
                "name": "Uncertain exception",
                "tool": "work",
                "inputs": {"value": 12},
                "retries": 3,
            }
        ]
    )

    result = await engine.execute(workflow, run_id="uncertain_exception_run")
    task_row = await repository.get_task(
        "uncertain_exception_run", "uncertain_exception"
    )

    assert result.state == RunState.FAILED
    assert result.reconciliation_required["uncertain_exception"] is True
    assert task_row["status"] == "FAILED"
    assert task_row["reconciliation_required"] == 1
    assert task_row["retry_count"] == 0
    assert calls == [12]
    assert effects == ["effect:12"]
    assert (
        await repository.update_task_output(
            "uncertain_exception_run",
            "uncertain_exception",
            {"value": 12},
        )
        is None
    )

    await repository.update_run_status("uncertain_exception_run", "RUNNING")
    recovered = await engine.recover_interrupted_runs()
    assert len(recovered) == 1
    assert recovered[0].reconciliation_required["uncertain_exception"] is True
    assert calls == [12]
    assert effects == ["effect:12"]


@pytest.mark.asyncio
async def test_handler_circuit_breaker_exception_after_dispatch_is_uncertain(repository):
    effects = []
    registry = ToolRegistry()

    def perform_side_effect(_):
        effects.append("performed")
        raise CircuitBreakerOpenException("Handler reported a downstream failure.")

    registry.register("work", ValueInput, perform_side_effect)
    result = await build_engine(repository, registry).execute(
        value_workflow(
            [{"id": "breaker_error", "name": "Breaker error", "tool": "work", "inputs": {"value": 1}}]
        )
    )
    task_row = await repository.get_task(result.run_id, "breaker_error")

    assert result.reconciliation_required["breaker_error"] is True
    assert task_row["reconciliation_required"] == 1
    assert effects == ["performed"]


@pytest.mark.asyncio
async def test_recovery_fails_ambiguous_running_tasks_without_reinvocation(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    workflow = value_workflow(
        [{"id": "inflight", "name": "In flight", "tool": "work", "inputs": {"value": 3}}]
    )
    stored = await repository.create_workflow(
        "recovery_workflow",
        "Recovery",
        "prompt",
        workflow.model_dump(mode="json", exclude_none=True),
    )
    await repository.create_run("recovery_run", stored["id"], status="RUNNING")
    await repository.create_task(
        "recovery_run",
        "inflight",
        "In flight",
        "work",
        status="RUNNING",
        input_params={"value": 3},
        idempotency_key="no-cached-result",
    )

    recovered = await build_engine(repository, registry).recover_interrupted_runs()

    assert len(recovered) == 1
    assert recovered[0].state == RunState.FAILED
    assert recovered[0].task_states["inflight"] == TaskState.FAILED
    assert "reconciliation" in recovered[0].errors["inflight"]
    assert calls == []


@pytest.mark.asyncio
async def test_recovery_resumes_safe_pending_tasks(repository):
    calls = []
    registry = ToolRegistry()

    def work(request):
        calls.append(request.value)
        return {"value": request.value}

    registry.register("work", ValueInput, work)
    workflow = value_workflow(
        [{"id": "pending", "name": "Pending", "tool": "work", "inputs": {"value": 7}}]
    )
    stored = await repository.create_workflow(
        "pending_workflow",
        "Recovery",
        "prompt",
        workflow.model_dump(mode="json", exclude_none=True),
    )
    await repository.create_run("pending_run", stored["id"], status="PENDING")
    await repository.create_task(
        "pending_run",
        "pending",
        "Pending",
        "work",
        input_params={"value": 7},
    )

    recovered = await build_engine(repository, registry).recover_interrupted_runs()

    assert len(recovered) == 1
    assert recovered[0].state == RunState.COMPLETED
    assert recovered[0].outputs["pending"] == {"value": 7}
    assert calls == [7]


@pytest.mark.asyncio
async def test_recovery_uses_cached_result_instead_of_repeating_tool(repository):
    calls = []
    registry = ToolRegistry()
    registry.register("work", ValueInput, lambda request: calls.append(request.value))
    workflow = value_workflow(
        [{"id": "cached", "name": "Cached", "tool": "work", "inputs": {"value": 5}}]
    )
    stored = await repository.create_workflow(
        "cached_workflow",
        "Recovery",
        "prompt",
        workflow.model_dump(mode="json", exclude_none=True),
    )
    await repository.create_run("cached_run", stored["id"], status="RUNNING")
    key = "safe-cached-key"
    await repository.create_task(
        "cached_run",
        "cached",
        "Cached",
        "work",
        status="RUNNING",
        input_params={"value": 5},
        idempotency_key=key,
    )
    await repository.save_execution_result(
        key,
        "cached_run",
        "cached",
        "work",
        {"value": 5},
    )

    recovered = await build_engine(repository, registry).recover_interrupted_runs()

    assert recovered[0].state == RunState.COMPLETED
    assert recovered[0].outputs["cached"] == {"value": 5}
    assert calls == []


@pytest.mark.asyncio
async def test_task_inputs_outputs_and_audit_logs_are_redacted(repository, capsys):
    secret = "unlisted-credential-value"
    registry = ToolRegistry()
    registry.register(
        "secret",
        SecretInput,
        lambda _: {"api_key": secret, "message": f"api_key={secret}"},
    )
    engine = build_engine(repository, registry)

    result = await engine.execute(
        WorkflowDefinition(
            tasks=[
                {
                    "id": "secret_task",
                    "name": "Secret task",
                    "tool": "secret",
                    "inputs": {"api_key": secret},
                }
            ]
        )
    )

    task = await repository.get_task(result.run_id, "secret_task")
    audit = await repository.list_audit_logs(result.run_id)
    logs = capsys.readouterr().out
    assert task["input_params_json"]["api_key"] == REDACTED_SUBSTITUTE
    assert result.outputs["secret_task"]["api_key"] == REDACTED_SUBSTITUTE
    assert all(secret not in str(row) for row in audit)
    assert secret not in logs


@pytest.mark.asyncio
async def test_sqlite_migration_preserves_tasks_and_adds_cancellation_state(tmp_path):
    legacy_schema = SCHEMA_FILE.read_text(encoding="utf-8")
    legacy_schema = legacy_schema.replace(
        "'FAILED', 'CANCELLED', 'SKIPPED'",
        "'FAILED', 'SKIPPED'",
    )
    legacy_schema = legacy_schema.replace(
        "    idempotency_key TEXT,\n"
        "    input_redacted BOOLEAN DEFAULT 0,\n"
        "    output_redacted BOOLEAN DEFAULT 0,\n",
        "",
    )
    legacy_schema = legacy_schema.replace(
        "    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,\n",
        "",
    )
    legacy_schema = legacy_schema.replace(
        "    output_redacted BOOLEAN DEFAULT 0,\n    created_at",
        "    created_at",
    )
    schema_path = tmp_path / "legacy-schema.sql"
    schema_path.write_text(legacy_schema, encoding="utf-8")
    database = Database(str(tmp_path / "legacy.db"))

    async with database.connection() as conn:
        await conn.executescript(legacy_schema)
        await conn.execute(
            "INSERT INTO workflows (id, title, raw_prompt, definition_json) "
            "VALUES ('wf', 'Workflow', 'prompt', '{}');"
        )
        await conn.execute(
            "INSERT INTO runs (id, workflow_id, status) VALUES ('run', 'wf', 'PENDING');"
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, run_id, task_id, name, tool_name, status,
                requires_approval, depends_on_json, input_params_json
            ) VALUES (
                'run:task', 'run', 'task', 'Task', 'work', 'PENDING',
                1, '["parent"]', '{"value":4}'
            );
            """
        )
        await conn.commit()

    await database.init_db(schema_path)
    await database.init_db(schema_path)
    repository = WorkflowRepository(database)
    task = await repository.get_task("run", "task")
    assert task is not None
    assert task["status"] == "PENDING"
    assert task["name"] == "Task"
    assert task["requires_approval"] == 1
    assert task["depends_on_json"] == ["parent"]
    assert task["input_params_json"] == {"value": 4}
    assert task["idempotency_key"] is None
    assert task["reconciliation_required"] == 0
    async with database.connection() as conn:
        index_rows = await (
            await conn.execute("PRAGMA index_list(tasks);")
        ).fetchall()
        foreign_key_rows = await (
            await conn.execute("PRAGMA foreign_key_list(tasks);")
        ).fetchall()
        violations = await (await conn.execute("PRAGMA foreign_key_check;")).fetchall()
    assert any(row["name"] == "idx_tasks_run_status" for row in index_rows)
    assert any(row["table"] == "runs" for row in foreign_key_rows)
    assert violations == []
    assert await repository.transition_task_status(
        "run", "task", ("PENDING",), "CANCELLED"
    )
    assert (await repository.get_task("run", "task"))["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_sqlite_migration_adds_reconciliation_column_to_existing_tasks(tmp_path):
    previous_schema = SCHEMA_FILE.read_text(encoding="utf-8").replace(
        "    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,\n",
        "",
    )
    schema_path = tmp_path / "previous-schema.sql"
    schema_path.write_text(previous_schema, encoding="utf-8")
    database = Database(str(tmp_path / "previous.db"))
    async with database.connection() as conn:
        await conn.executescript(previous_schema)
        await conn.execute(
            "INSERT INTO workflows (id, title, raw_prompt, definition_json) "
            "VALUES ('wf', 'Workflow', 'prompt', '{}');"
        )
        await conn.execute(
            "INSERT INTO runs (id, workflow_id, status) VALUES ('run', 'wf', 'PENDING');"
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, run_id, task_id, name, tool_name, status,
                requires_approval, depends_on_json, input_params_json
            ) VALUES ('run:task', 'run', 'task', 'Task', 'work', 'PENDING', 0, '[]', '{}');
            """
        )
        await conn.commit()

    await database.init_db()
    task = await WorkflowRepository(database).get_task("run", "task")
    assert task is not None
    assert task["status"] == "PENDING"
    assert task["reconciliation_required"] == 0


@pytest.mark.asyncio
async def test_concurrent_sqlite_migrations_serialize_and_observe_final_schema(
    tmp_path, monkeypatch
):
    previous_schema = SCHEMA_FILE.read_text(encoding="utf-8").replace(
        "    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,\n",
        "",
    )
    schema_path = tmp_path / "concurrent-previous-schema.sql"
    schema_path.write_text(previous_schema, encoding="utf-8")
    db_path = str(tmp_path / "concurrent-migration.db")
    database = Database(db_path)

    async with database.connection() as conn:
        await conn.executescript(previous_schema)
        await conn.execute(
            "INSERT INTO workflows (id, title, raw_prompt, definition_json) "
            "VALUES ('wf', 'Workflow', 'prompt', '{}');"
        )
        await conn.execute(
            "INSERT INTO runs (id, workflow_id, status) VALUES ('run', 'wf', 'PENDING');"
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, run_id, task_id, name, tool_name, status,
                requires_approval, depends_on_json, input_params_json
            ) VALUES ('run:task', 'run', 'task', 'Task', 'work', 'PENDING', 0, '[]', '{}');
            """
        )
        await conn.commit()

    original_migration = Database._migrate_execution_schema
    arrivals = 0
    both_at_migration = asyncio.Event()

    async def synchronize_migrations(self, conn):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_at_migration.set()
        await asyncio.wait_for(both_at_migration.wait(), timeout=10)
        await original_migration(self, conn)

    monkeypatch.setattr(Database, "_migrate_execution_schema", synchronize_migrations)
    await asyncio.wait_for(
        asyncio.gather(
            Database(db_path).init_db(schema_path),
            Database(db_path).init_db(schema_path),
        ),
        timeout=30,
    )

    for reopened in (Database(db_path), Database(db_path)):
        async with reopened.connection() as conn:
            columns = {
                row["name"]
                for row in await (await conn.execute("PRAGMA table_info(tasks);")).fetchall()
            }
            task = await (
                await conn.execute(
                    "SELECT name, status, reconciliation_required "
                    "FROM tasks WHERE run_id = 'run' AND task_id = 'task';"
                )
            ).fetchone()
            violations = await (
                await conn.execute("PRAGMA foreign_key_check;")
            ).fetchall()
        assert "reconciliation_required" in columns
        assert task["name"] == "Task"
        assert task["status"] == "PENDING"
        assert task["reconciliation_required"] == 0
        assert violations == []


@pytest.mark.asyncio
async def test_sqlite_additive_migration_rolls_back_partial_column_additions(
    tmp_path, monkeypatch
):
    previous_schema = SCHEMA_FILE.read_text(encoding="utf-8").replace(
        "    idempotency_key TEXT,\n"
        "    input_redacted BOOLEAN DEFAULT 0,\n"
        "    output_redacted BOOLEAN DEFAULT 0,\n"
        "    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,\n",
        "",
    )
    schema_path = tmp_path / "rollback-previous-schema.sql"
    schema_path.write_text(previous_schema, encoding="utf-8")
    database = Database(str(tmp_path / "rollback-migration.db"))

    async with database.connection() as conn:
        await conn.executescript(previous_schema)
        await conn.execute(
            "INSERT INTO workflows (id, title, raw_prompt, definition_json) "
            "VALUES ('wf', 'Workflow', 'prompt', '{}');"
        )
        await conn.execute(
            "INSERT INTO runs (id, workflow_id, status) VALUES ('run', 'wf', 'PENDING');"
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, run_id, task_id, name, tool_name, status,
                requires_approval, depends_on_json, input_params_json
            ) VALUES ('run:task', 'run', 'task', 'Task', 'work', 'PENDING', 0, '[]', '{}');
            """
        )
        await conn.commit()

    original_add_column = Database._add_execution_column

    async def fail_during_migration(self, conn, name, declaration):
        if name == "output_redacted":
            raise sqlite3.OperationalError("injected migration failure")
        await original_add_column(conn, name, declaration)

    monkeypatch.setattr(Database, "_add_execution_column", fail_during_migration)
    with pytest.raises(DatabaseError, match="injected migration failure"):
        await database.init_db(schema_path)

    async with database.connection() as conn:
        columns = {
            row["name"]
            for row in await (await conn.execute("PRAGMA table_info(tasks);")).fetchall()
        }
        task = await (
            await conn.execute(
                "SELECT name, status FROM tasks WHERE run_id = 'run' AND task_id = 'task';"
            )
        ).fetchone()
        index_rows = await (await conn.execute("PRAGMA index_list(tasks);")).fetchall()
        violations = await (await conn.execute("PRAGMA foreign_key_check;")).fetchall()
    assert not {
        "idempotency_key",
        "input_redacted",
        "output_redacted",
        "reconciliation_required",
    }.intersection(columns)
    assert task["name"] == "Task"
    assert task["status"] == "PENDING"
    assert any(row["name"] == "idx_tasks_run_status" for row in index_rows)
    assert violations == []
