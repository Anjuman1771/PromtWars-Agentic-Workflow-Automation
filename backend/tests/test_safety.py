"""Comprehensive tests for FlowPilot AI Phase 3 Safety Infrastructure.

Covers:
- Execution safety limits (step limits, run timeout, task timeout, retries, payload sizes)
- Asynchronous circuit breaker state transitions and concurrent protection
- Cryptographic approval token generation, hashing, expiration, tampering, and single-use
- Workspace path containment, traversal defense, symlink escapes, and .git protection
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import pytest

from backend.app.config import Settings
from backend.app.safety.approval import ApprovalTokenManager, hash_payload, hash_token
from backend.app.safety.circuit_breaker import CircuitBreaker, CircuitBreakerState
from backend.app.safety.containment import validate_workspace_path
from backend.app.safety.exceptions import (
    ApprovalConsumedException,
    ApprovalExpiredException,
    ApprovalNotFoundException,
    ApprovalTamperingException,
    CircuitBreakerOpenException,
    ExecutionLimitExceededException,
    PathTraversalViolation,
    TaskTimeoutException,
)
from backend.app.safety.limits import ExecutionLimits
from backend.app.storage.database import Database
from backend.app.storage.repository import WorkflowRepository


def test_safety_package_keeps_lazy_approval_exports():
    import backend.app.safety as safety_package

    assert safety_package.ApprovalTokenManager is ApprovalTokenManager
    assert safety_package.approval_manager is not None


# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest.fixture
async def safety_db(tmp_path):
    """Provide a fresh SQLite database for safety tests."""
    db_file = tmp_path / "safety_test.db"
    database = Database(db_path=str(db_file))
    await database.init_db()
    return database


@pytest.fixture
def safety_repo(safety_db):
    """Provide a WorkflowRepository bound to safety_db."""
    return WorkflowRepository(database=safety_db)


@pytest.fixture
def token_manager(safety_repo):
    """Provide an ApprovalTokenManager bound to safety_repo with a short TTL."""
    return ApprovalTokenManager(repository=safety_repo, ttl_seconds=600)


# ==============================================================================
# 1. EXECUTION SAFETY LIMITS TESTS
# ==============================================================================

def test_workflow_step_limit_validation():
    """Verify workflow definitions exceeding max_workflow_steps are rejected."""
    limits = ExecutionLimits(max_workflow_steps=5)

    # 5 tasks is acceptable
    valid_wf = {"tasks": [{"id": f"t{i}", "retries": 1} for i in range(5)]}
    limits.validate_workflow_definition(valid_wf)

    # 6 tasks exceeds limit of 5
    invalid_wf = {"tasks": [{"id": f"t{i}", "retries": 1} for i in range(6)]}
    with pytest.raises(ExecutionLimitExceededException) as exc_info:
        limits.validate_workflow_definition(invalid_wf)
    assert "exceeds configured maximum steps limit" in str(exc_info.value)


@pytest.mark.parametrize(
    "limit",
    [
        {"max_workflow_steps": 0},
        {"max_concurrent_tasks": 0},
        {"max_run_time_seconds": 0},
        {"default_task_timeout_seconds": 0},
        {"max_retries_per_task": -1},
    ],
)
def test_execution_limits_reject_invalid_values_instead_of_using_defaults(limit):
    with pytest.raises(ValueError, match="Invalid execution limit"):
        ExecutionLimits(**limit)


def test_zero_retry_limit_is_preserved():
    assert ExecutionLimits(max_retries_per_task=0).max_retries_per_task == 0


def test_safety_components_reject_zero_approval_and_breaker_limits():
    with pytest.raises(ValueError, match="lifetime"):
        ApprovalTokenManager(ttl_seconds=0)
    with pytest.raises(ValueError, match="Circuit breaker limits"):
        CircuitBreaker(failure_threshold=0)


def test_workflow_task_retry_limit_validation():
    """Verify task retry counts above max_retries_per_task are rejected."""
    limits = ExecutionLimits(max_retries_per_task=2)

    invalid_wf = {
        "tasks": [
            {"id": "task_1", "retries": 5}  # 5 > 2
        ]
    }
    with pytest.raises(ExecutionLimitExceededException) as exc_info:
        limits.validate_workflow_definition(invalid_wf)
    assert "exceeding maximum allowed" in str(exc_info.value)


def test_task_timeout_exceeds_workflow_duration_validation():
    """Verify individual task timeout cannot exceed total workflow max runtime."""
    limits = ExecutionLimits(max_run_time_seconds=60)

    invalid_wf = {
        "tasks": [
            {"id": "slow_task", "timeout_seconds": 120}  # 120 > 60
        ]
    }
    with pytest.raises(ExecutionLimitExceededException) as exc_info:
        limits.validate_workflow_definition(invalid_wf)
    assert "cannot exceed total workflow max runtime" in str(exc_info.value)


def test_payload_and_response_size_limits():
    """Verify payload and response byte caps raise exceptions when breached."""
    limits = ExecutionLimits(max_payload_size_bytes=100, max_response_size_bytes=200)

    # Valid payload
    assert limits.validate_payload_size({"key": "val"}) < 100

    # Oversized payload
    huge_data = {"key": "A" * 150}
    with pytest.raises(ExecutionLimitExceededException):
        limits.validate_payload_size(huge_data)

    # Response size validation
    limits.validate_response_size(150)  # ok
    with pytest.raises(ExecutionLimitExceededException):
        limits.validate_response_size(250)


def test_runtime_step_and_retry_checks():
    """Verify runtime counter checks."""
    limits = ExecutionLimits(max_workflow_steps=10, max_retries_per_task=3)

    limits.check_step_limit(current_step_count=9)  # ok
    with pytest.raises(ExecutionLimitExceededException):
        limits.check_step_limit(current_step_count=10)

    limits.check_retry_limit(current_retries=2)  # ok
    with pytest.raises(ExecutionLimitExceededException):
        limits.check_retry_limit(current_retries=3)


def test_runtime_timeout_checks():
    """Verify runtime timeout checks."""
    import time
    limits = ExecutionLimits(max_run_time_seconds=2, default_task_timeout_seconds=1)

    # Within bounds (just started)
    now = time.time()
    limits.check_execution_time(started_at_timestamp=now)
    limits.check_task_timeout(started_at_timestamp=now)

    # Past workflow timeout
    with pytest.raises(ExecutionLimitExceededException):
        limits.check_execution_time(started_at_timestamp=now - 5.0)

    # Past task timeout
    with pytest.raises(TaskTimeoutException):
        limits.check_task_timeout(started_at_timestamp=now - 2.0)


# ==============================================================================
# 2. CIRCUIT BREAKER TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_circuit_breaker_closed_state_success():
    """Verify breaker stays CLOSED on successful calls and resets failure count."""
    cb = CircuitBreaker(name="test_cb", failure_threshold=3, recovery_timeout=0.1)
    assert cb.state == CircuitBreakerState.CLOSED

    async def successful_call():
        return "success"

    result = await cb.execute(successful_call)
    assert result == "success"
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_tripping_to_open():
    """Verify breaker trips to OPEN after reaching failure threshold."""
    cb = CircuitBreaker(name="test_cb", failure_threshold=2, recovery_timeout=0.2)

    async def failing_call():
        raise RuntimeError("Service failure")

    # Failure 1
    with pytest.raises(RuntimeError):
        await cb.execute(failing_call)
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 1

    # Failure 2 -> Trips to OPEN
    with pytest.raises(RuntimeError):
        await cb.execute(failing_call)
    assert cb.state == CircuitBreakerState.OPEN
    assert cb.failure_count == 2

    # Subsequent call rejected immediately without executing failing_call
    with pytest.raises(CircuitBreakerOpenException) as exc_info:
        await cb.execute(failing_call)
    assert "is OPEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovery():
    """Verify recovery timeout moves breaker to HALF_OPEN and success resets to CLOSED."""
    cb = CircuitBreaker(name="test_cb", failure_threshold=1, recovery_timeout=0.1)

    async def fail():
        raise RuntimeError("Trip failure")

    # Trip breaker
    with pytest.raises(RuntimeError):
        await cb.execute(fail)
    assert cb.state == CircuitBreakerState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.12)
    assert cb.state == CircuitBreakerState.HALF_OPEN

    # Successful probe recovers breaker to CLOSED
    async def probe():
        return "recovered"

    result = await cb.execute(probe)
    assert result == "recovered"
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_failure_re_trips():
    """Verify failure during HALF_OPEN probe immediately returns breaker to OPEN."""
    cb = CircuitBreaker(name="test_cb", failure_threshold=1, recovery_timeout=0.1)

    # Trip breaker
    await cb.trip()
    assert cb.state == CircuitBreakerState.OPEN

    # Wait for cooldown
    await asyncio.sleep(0.12)
    assert cb.state == CircuitBreakerState.HALF_OPEN

    # Failing probe re-trips
    async def bad_probe():
        raise ValueError("Still broken")

    with pytest.raises(ValueError):
        await cb.execute(bad_probe)

    assert cb.state == CircuitBreakerState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_concurrent_access():
    """Verify concurrent requests respect breaker state under load."""
    cb = CircuitBreaker(name="concur_cb", failure_threshold=2, recovery_timeout=0.5)

    async def flaky_call():
        raise ConnectionError("down")

    # Trip breaker by exceeding failure threshold
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await cb.execute(flaky_call)

    assert cb.state == CircuitBreakerState.OPEN

    # Concurrently fire 5 requests; all must be blocked by the OPEN breaker immediately
    results = await asyncio.gather(
        *(cb.execute(flaky_call) for _ in range(5)),
        return_exceptions=True,
    )

    open_exceptions = [r for r in results if isinstance(r, CircuitBreakerOpenException)]
    assert len(open_exceptions) == 5


# ==============================================================================
# 3. SECURE APPROVAL TOKENS TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_token_generation_and_hashing(token_manager, safety_repo):
    """Verify issued token stores only SHA-256 hash in DB, returning raw token to client."""
    # Setup test run
    wf = await safety_repo.create_workflow(
        workflow_id="wf-tok", title="T", raw_prompt="p", definition={}
    )
    run = await safety_repo.create_run(run_id="run-tok", workflow_id=wf["id"])
    task = await safety_repo.create_task(
        run_id=run["id"], task_id="t1", name="Task 1", tool_name="http_client"
    )

    action_payload = {"url": "https://api.example.com/delete", "method": "DELETE"}
    issued = await token_manager.issue_token(
        run_id=run["id"],
        task_id="t1",
        action_payload=action_payload,
        ttl_seconds=300,
    )

    raw_token = issued["raw_token"]
    token_hash = issued["token_hash"]
    assert raw_token.startswith("fp_tok_")
    assert token_hash == hash_token(raw_token)

    # Verify database ONLY contains the hash
    stored = await safety_repo.get_approval(token_hash)
    assert stored is not None
    assert stored["token"] == token_hash
    assert stored["payload_hash"] == hash_payload(action_payload)

    # Searching DB for raw token must return nothing
    assert await safety_repo.get_approval(raw_token) is None


@pytest.mark.asyncio
async def test_invalid_token_is_rejected(token_manager):
    with pytest.raises(ApprovalNotFoundException):
        await token_manager.verify_and_consume(
            raw_token="not-a-valid-token",
            expected_payload={"action": "noop"},
        )


@pytest.mark.asyncio
async def test_token_expiration(token_manager, safety_repo):
    """Verify expired tokens cannot be approved."""
    wf = await safety_repo.create_workflow(workflow_id="wf-exp", title="E", raw_prompt="p", definition={})
    run = await safety_repo.create_run(run_id="run-exp", workflow_id=wf["id"])
    await safety_repo.create_task(run_id=run["id"], task_id="t1", name="Task 1", tool_name="tool")

    # Issue token with negative TTL (already expired)
    issued = await token_manager.issue_token(
        run_id=run["id"],
        task_id="t1",
        action_payload={"op": "send"},
        ttl_seconds=-10,
    )

    with pytest.raises(ApprovalExpiredException):
        await token_manager.verify_and_consume(
            raw_token=issued["raw_token"],
            action="APPROVE",
            decision_by="admin",
            expected_payload={"op": "send"},
        )


@pytest.mark.asyncio
async def test_payload_tampering_mismatch(token_manager, safety_repo):
    """Verify approval fails if the approved action payload differs from the issued payload."""
    wf = await safety_repo.create_workflow(workflow_id="wf-tamper", title="T", raw_prompt="p", definition={})
    run = await safety_repo.create_run(run_id="run-tamper", workflow_id=wf["id"])
    await safety_repo.create_task(run_id=run["id"], task_id="t1", name="Task 1", tool_name="tool")

    original_payload = {"account": "user123", "amount": 100}
    issued = await token_manager.issue_token(
        run_id=run["id"],
        task_id="t1",
        action_payload=original_payload,
    )

    # Attempt to approve with tampered payload
    tampered_payload = {"account": "user123", "amount": 99999}
    with pytest.raises(ApprovalTamperingException):
        await token_manager.verify_and_consume(
            raw_token=issued["raw_token"],
            action="APPROVE",
            decision_by="admin",
            expected_payload=tampered_payload,
        )

    # Approving with original payload succeeds
    approved = await token_manager.verify_and_consume(
        raw_token=issued["raw_token"],
        action="APPROVE",
        decision_by="admin",
        expected_payload=original_payload,
    )
    assert approved["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_token_reuse_prevention(token_manager, safety_repo):
    """Verify single-use token cannot be consumed a second time."""
    wf = await safety_repo.create_workflow(workflow_id="wf-reuse", title="R", raw_prompt="p", definition={})
    run = await safety_repo.create_run(run_id="run-reuse", workflow_id=wf["id"])
    await safety_repo.create_task(run_id=run["id"], task_id="t1", name="Task 1", tool_name="tool")

    issued = await token_manager.issue_token(
        run_id=run["id"],
        task_id="t1",
        action_payload={"safe": True},
    )

    # First consumption succeeds
    with pytest.raises(ApprovalTamperingException):
        await token_manager.verify_and_consume(
            raw_token=issued["raw_token"],
            action="APPROVE",
            expected_payload=None,
        )

    first = await token_manager.verify_and_consume(
        raw_token=issued["raw_token"],
        action="APPROVE",
        expected_payload={"safe": True},
    )
    assert first["status"] == "APPROVED"

    # Second consumption must be rejected
    with pytest.raises(ApprovalConsumedException):
        await token_manager.verify_and_consume(
            raw_token=issued["raw_token"],
            action="APPROVE",
            expected_payload={"safe": True},
        )


@pytest.mark.asyncio
async def test_concurrent_approval_consumption(token_manager, safety_repo):
    """Verify atomic race protection: only one concurrent consumption can succeed."""
    wf = await safety_repo.create_workflow(workflow_id="wf-race", title="Race", raw_prompt="p", definition={})
    run = await safety_repo.create_run(run_id="run-race", workflow_id=wf["id"])
    await safety_repo.create_task(run_id=run["id"], task_id="t1", name="Task 1", tool_name="tool")

    issued = await token_manager.issue_token(
        run_id=run["id"],
        task_id="t1",
        action_payload={"item": "data"},
    )

    # Fire 5 simultaneous approvals with the same token
    results = await asyncio.gather(
        *(
            token_manager.verify_and_consume(
                raw_token=issued["raw_token"],
                action="APPROVE",
                decision_by=f"operator_{i}",
                expected_payload={"item": "data"},
            )
            for i in range(5)
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, dict) and r.get("status") == "APPROVED"]
    consumed_errors = [r for r in results if isinstance(r, ApprovalConsumedException)]

    # Exactly one succeeded
    assert len(successes) == 1
    assert len(consumed_errors) == 4


# ==============================================================================
# 4. WORKSPACE PATH SECURITY TESTS
# ==============================================================================

def test_valid_paths_inside_workspace(tmp_path):
    """Verify valid relative and absolute paths within workspace resolve properly."""
    workspace = tmp_path / "my_workspace"
    workspace.mkdir()

    # Relative file
    p1 = validate_workspace_path("data.json", workspace_root=workspace)
    assert p1 == (workspace / "data.json").resolve()
    assert p1.is_relative_to(workspace)

    # Nested relative path
    p2 = validate_workspace_path("sub/folder/file.txt", workspace_root=workspace)
    assert p2 == (workspace / "sub/folder/file.txt").resolve()
    assert p2.is_relative_to(workspace)

    # Absolute path inside workspace
    inside_abs = workspace / "nested" / "doc.pdf"
    p3 = validate_workspace_path(inside_abs, workspace_root=workspace)
    assert p3 == inside_abs.resolve()


def test_empty_explicit_workspace_root_is_rejected():
    with pytest.raises(PathTraversalViolation, match="root cannot be empty"):
        validate_workspace_path("file.txt", workspace_root="")


def test_path_traversal_rejection(tmp_path):
    """Verify ../ traversal attempts are rejected."""
    workspace = tmp_path / "jail"
    workspace.mkdir()

    traversal_attempts = [
        "../outside.txt",
        "../../etc/passwd",
        "sub/../../escaped.txt",
        "..\\windows\\system32",
        "foo/bar/../../../root.txt",
    ]

    for attempt in traversal_attempts:
        with pytest.raises(PathTraversalViolation):
            validate_workspace_path(attempt, workspace_root=workspace)


def test_encoded_traversal_rejection(tmp_path):
    """Verify URL-encoded and double-encoded traversal attempts are rejected."""
    workspace = tmp_path / "jail"
    workspace.mkdir()

    encoded_attempts = [
        "%2e%2e/outside.txt",
        "%2e%2e%2foutside.txt",
        "%252e%252e/double_encoded.txt",
        "nested/%2e%2e/%2e%2e/outside.txt",
    ]

    for attempt in encoded_attempts:
        with pytest.raises(PathTraversalViolation):
            validate_workspace_path(attempt, workspace_root=workspace)


def test_absolute_path_escape_rejection(tmp_path):
    """Verify absolute paths outside the workspace root are rejected."""
    workspace = tmp_path / "sandbox"
    workspace.mkdir()

    outside_dir = tmp_path / "outside_dir" / "secret.txt"

    with pytest.raises(PathTraversalViolation):
        validate_workspace_path(outside_dir, workspace_root=workspace)


def test_git_directory_access_forbidden(tmp_path):
    """Verify attempts to target .git directory inside workspace are rejected."""
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(PathTraversalViolation) as exc_info:
        validate_workspace_path(".git/config", workspace_root=workspace)
    assert ".git" in str(exc_info.value)


def test_symlink_escape_rejection(tmp_path):
    """Verify symlinks pointing outside the workspace boundary are rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside_target = tmp_path / "secret_host_file.txt"
    outside_target.write_text("classified")

    symlink_path = workspace / "innocent_link.txt"
    try:
        os.symlink(outside_target, symlink_path)
    except (OSError, NotImplementedError):
        # On Windows, symlink creation without admin privileges might raise OSError
        pytest.skip("Symlink creation requires elevated permissions on this environment.")

    # Validating the symlink must resolve to outside_target and trigger PathTraversalViolation
    with pytest.raises(PathTraversalViolation):
        validate_workspace_path("innocent_link.txt", workspace_root=workspace)
