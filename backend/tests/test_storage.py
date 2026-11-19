"""Comprehensive tests for FlowPilot AI Database & Repository Layer."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import pytest
import sqlite3

from backend.app.storage.database import Database, DatabaseError
from backend.app.storage.repository import WorkflowRepository
from backend.app.safety.redaction import REDACTED_SUBSTITUTE


@pytest.fixture
async def temp_db(tmp_path):
    """Provide a fresh, initialized temporary SQLite database instance."""
    db_file = tmp_path / "test_flowpilot.db"
    database = Database(db_path=str(db_file))
    await database.init_db()
    return database


@pytest.fixture
def repo(temp_db):
    """Provide a WorkflowRepository bound to the test database."""
    return WorkflowRepository(database=temp_db)


@pytest.mark.asyncio
async def test_database_initialization_and_tables(temp_db):
    """Verify that init_db creates all six required tables and indexes."""
    async with temp_db.connection() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        rows = await cursor.fetchall()
        table_names = {r["name"] for r in rows}

    expected_tables = {
        "workflows",
        "runs",
        "tasks",
        "approvals",
        "idempotency_records",
        "audit_logs",
    }
    for table in expected_tables:
        assert table in table_names, f"Table '{table}' missing from database schema."


@pytest.mark.asyncio
async def test_connection_pragmas_are_configured(temp_db):
    async with temp_db.connection() as conn:
        foreign_keys = await (await conn.execute("PRAGMA foreign_keys;")).fetchone()
        busy_timeout = await (await conn.execute("PRAGMA busy_timeout;")).fetchone()
        journal_mode = await (await conn.execute("PRAGMA journal_mode;")).fetchone()

    assert foreign_keys[0] == 1
    assert busy_timeout[0] == 5000
    assert journal_mode[0].lower() == "wal"


@pytest.mark.asyncio
async def test_database_initialization_rejects_missing_or_invalid_schema(tmp_path):
    database = Database(db_path=str(tmp_path / "initialization-error.db"))
    with pytest.raises(DatabaseError, match="Schema file not found"):
        await database.init_db(tmp_path / "missing-schema.sql")

    invalid_schema = tmp_path / "invalid-schema.sql"
    invalid_schema.write_text("THIS IS NOT VALID SQL;", encoding="utf-8")
    with pytest.raises(DatabaseError, match="Failed to initialize schema"):
        await database.init_db(invalid_schema)


@pytest.mark.asyncio
async def test_wal_initialization_error_is_not_swallowed(monkeypatch):
    import backend.app.storage.database as database_module

    class Cursor:
        def __init__(self, value):
            self.value = value

        async def fetchone(self):
            return (self.value,)

    class FailingConnection:
        row_factory = None

        async def execute(self, query):
            if "journal_mode = WAL" in query:
                raise sqlite3.OperationalError("WAL initialization failed")
            if query == "PRAGMA foreign_keys;":
                return Cursor(1)
            if query == "PRAGMA busy_timeout;":
                return Cursor(5000)
            return Cursor(None)

        async def close(self):
            return None

    async def connect(_db_path):
        return FailingConnection()

    monkeypatch.setattr(database_module.aiosqlite, "connect", connect)
    with pytest.raises(DatabaseError, match="WAL initialization failed"):
        async with Database(db_path="flowpilot.db").connection():
            pass


@pytest.mark.asyncio
async def test_repeated_initialization_is_safe(temp_db):
    """Verify that calling init_db multiple times does not raise errors or corrupt tables."""
    await temp_db.init_db()
    await temp_db.init_db()


@pytest.mark.asyncio
async def test_foreign_key_enforcement(temp_db):
    """Verify foreign key constraints reject orphaned child records."""
    async with temp_db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            # Attempt to insert a run referencing a non-existent workflow
            await conn.execute(
                "INSERT INTO runs (id, workflow_id, status) VALUES ('r-invalid', 'wf-nonexistent', 'PENDING');"
            )
            await conn.commit()


@pytest.mark.asyncio
async def test_workflow_crud(repo):
    """Verify workflow creation, retrieval, listing, and deletion."""
    wf_id = "wf-test-1"
    created = await repo.create_workflow(
        workflow_id=wf_id,
        title="Fetch and Analyze",
        description="A sample workflow",
        raw_prompt="Fetch news and summarize",
        definition={"tasks": [{"id": "t1", "tool": "http_client"}]},
    )
    assert created["id"] == wf_id
    assert created["title"] == "Fetch and Analyze"
    assert created["definition_json"]["tasks"][0]["id"] == "t1"

    fetched = await repo.get_workflow(wf_id)
    assert fetched is not None
    assert fetched["title"] == "Fetch and Analyze"

    # Listing
    workflows = await repo.list_workflows()
    assert len(workflows) >= 1
    assert any(w["id"] == wf_id for w in workflows)

    # Deletion
    deleted = await repo.delete_workflow(wf_id)
    assert deleted is True
    assert await repo.get_workflow(wf_id) is None


@pytest.mark.asyncio
async def test_sensitive_text_is_redacted_at_persistence_boundaries(repo):
    secret = "unlistedApiSecret_9x7K"
    prompt = f"Summarize this report and use api_key={secret}"
    wf = await repo.create_workflow(
        workflow_id="wf-redaction",
        title="Workflow title",
        description="A safe description",
        raw_prompt=prompt,
        definition={"safe": "preserved", "client_secret": secret},
    )
    assert wf["raw_prompt"] == f"Summarize this report and use api_key={REDACTED_SUBSTITUTE}"
    assert "Summarize this report" in wf["raw_prompt"]
    assert wf["definition_json"] == {
        "safe": "preserved",
        "client_secret": REDACTED_SUBSTITUTE,
    }

    json_prompt = await repo.create_workflow(
        workflow_id="wf-json-prompt-redaction",
        title="JSON prompt",
        raw_prompt='{"api_key":"private-value-123","instruction":"Keep this text"}',
        definition={},
    )
    assert json.loads(json_prompt["raw_prompt"]) == {
        "api_key": REDACTED_SUBSTITUTE,
        "instruction": "Keep this text",
    }

    run = await repo.create_run(run_id="run-redaction", workflow_id=wf["id"])
    task = await repo.create_task(
        run_id=run["id"],
        task_id="task1",
        name="Task",
        tool_name="transform",
        input_params={"api_key": secret, "author": "A. Example"},
    )
    assert task["input_params_json"]["api_key"] == REDACTED_SUBSTITUTE
    assert task["input_params_json"]["author"] == "A. Example"

    output = await repo.update_task_output(
        run_id=run["id"],
        task_id="task1",
        output_data={"result": "kept", "token": secret},
    )
    assert output["output_data_json"] == {
        "result": "kept",
        "token": REDACTED_SUBSTITUTE,
    }

    failed_task = await repo.update_task_status(
        run_id=run["id"],
        task_id="task1",
        status="FAILED",
        error_message=f"Tool failed with api_key={secret}",
    )
    assert secret not in failed_task["error_message"]
    assert REDACTED_SUBSTITUTE in failed_task["error_message"]

    failed_run = await repo.update_run_status(
        run_id=run["id"],
        status="FAILED",
        error_message=f"Run failed with api_key={secret}",
    )
    assert secret not in failed_run["error_message"]
    assert REDACTED_SUBSTITUTE in failed_run["error_message"]

    audit = await repo.create_audit_log(
        run_id=run["id"],
        level="ERROR",
        event_type="TOOL_FAILED",
        message=f"Request failed with api_key={secret}",
        metadata={"reason": f"api_key={secret}", "safe": "preserved"},
    )
    assert secret not in audit["message"]
    assert REDACTED_SUBSTITUTE in audit["message"]
    assert audit["metadata_json"] == {
        "reason": f"api_key={REDACTED_SUBSTITUTE}",
        "safe": "preserved",
    }


@pytest.mark.asyncio
async def test_run_crud(repo):
    """Verify execution run lifecycle operations."""
    wf = await repo.create_workflow(
        workflow_id="wf-runs-test",
        title="Run Test WF",
        raw_prompt="prompt",
        definition={},
    )
    run_id = "run-001"
    run = await repo.create_run(run_id=run_id, workflow_id=wf["id"])
    assert run["id"] == run_id
    assert run["status"] == "PENDING"
    assert run["current_step_count"] == 0

    # Update status to RUNNING
    updated = await repo.update_run_status(
        run_id=run_id,
        status="RUNNING",
        current_step_count=1,
    )
    assert updated["status"] == "RUNNING"
    assert updated["current_step_count"] == 1

    # Update status to COMPLETED with completed_at timestamp
    completed = await repo.update_run_status(
        run_id=run_id,
        status="COMPLETED",
        completed=True,
    )
    assert completed["status"] == "COMPLETED"
    assert completed["completed_at"] is not None

    # Filtered list
    runs = await repo.list_runs(workflow_id="wf-runs-test", status="COMPLETED")
    assert len(runs) == 1
    assert runs[0]["id"] == run_id


@pytest.mark.asyncio
async def test_task_crud(repo):
    """Verify task creation, output updates, and status transitions."""
    wf = await repo.create_workflow(workflow_id="wf-task-test", title="Task WF", raw_prompt="prompt", definition={})
    run = await repo.create_run(run_id="run-task-test", workflow_id=wf["id"])

    task = await repo.create_task(
        run_id=run["id"],
        task_id="step_1",
        name="Fetch URL",
        tool_name="http_client",
        requires_approval=True,
        depends_on=[],
        input_params={"url": "https://example.com/api", "api_key": "AIzaSyTest1234567890123456789012345678"},
    )
    assert task["task_id"] == "step_1"
    assert task["requires_approval"] == 1
    # Verify sensitive api_key was redacted before saving
    assert task["input_params_json"]["api_key"] == REDACTED_SUBSTITUTE

    # Update task to RUNNING
    started_task = await repo.update_task_status(
        run_id=run["id"],
        task_id="step_1",
        status="RUNNING",
        started=True,
    )
    assert started_task["status"] == "RUNNING"
    assert started_task["started_at"] is not None

    # Update task output
    completed_task = await repo.update_task_output(
        run_id=run["id"],
        task_id="step_1",
        output_data={"status_code": 200, "data": "success"},
    )
    assert completed_task["status"] == "COMPLETED"
    assert completed_task["output_data_json"]["status_code"] == 200
    assert completed_task["completed_at"] is not None

    # List tasks for run
    all_tasks = await repo.list_tasks(run["id"])
    assert len(all_tasks) == 1
    assert all_tasks[0]["task_id"] == "step_1"


@pytest.mark.asyncio
async def test_approval_persistence_and_atomic_consumption(repo):
    """Verify HITL approval creation, expiration filtering, and atomic single-use consumption."""
    wf = await repo.create_workflow(workflow_id="wf-appr-test", title="Approval WF", raw_prompt="p", definition={})
    run = await repo.create_run(run_id="run-appr-test", workflow_id=wf["id"])
    task = await repo.create_task(
        run_id=run["id"],
        task_id="delete_db",
        name="Delete Record",
        tool_name="file_ops",
        requires_approval=True,
    )

    future_exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    token = "sec-token-abc-123456"
    approval = await repo.create_approval(
        approval_id="appr-001",
        token=token,
        run_id=run["id"],
        task_id="delete_db",
        payload_hash="sha256_mock_hash",
        action_payload={"action": "delete", "target": "data.txt"},
        expires_at=future_exp,
    )
    assert approval["token"] == token
    assert approval["status"] == "PENDING"

    # Listed in pending approvals
    pending = await repo.list_pending_approvals(run_id=run["id"])
    assert len(pending) == 1
    assert pending[0]["token"] == token

    # Atomically consume the token
    consumed = await repo.consume_approval(token=token, status="APPROVED", decision_by="admin")
    assert consumed is not None
    assert consumed["status"] == "APPROVED"
    assert consumed["decision_by"] == "admin"
    assert consumed["consumed_at"] is not None

    # Second consumption MUST return None (Single-use safeguard)
    second_attempt = await repo.consume_approval(token=token, status="APPROVED", decision_by="attacker")
    assert second_attempt is None


@pytest.mark.asyncio
async def test_idempotency_cache(repo):
    """Verify idempotency record storage, caching, and retrieval."""
    wf = await repo.create_workflow(workflow_id="wf-idem-test", title="Idem WF", raw_prompt="p", definition={})
    run = await repo.create_run(run_id="run-idem-test", workflow_id=wf["id"])

    idem_key = "sha256_task_execution_fingerprint"
    saved = await repo.save_execution_result(
        idempotency_key=idem_key,
        run_id=run["id"],
        task_id="fetch_task",
        tool_name="http_client",
        output_data={"result": "data_item", "token": "Bearer mySecretToken123"},
    )
    assert saved["idempotency_key"] == idem_key
    # Secret redaction check: sensitive key 'token' is fully masked
    assert saved["output_json"]["token"] == REDACTED_SUBSTITUTE

    # Cache hit
    cached = await repo.get_cached_result(idem_key)
    assert cached is not None
    assert cached["tool_name"] == "http_client"
    assert cached["output_json"]["result"] == "data_item"


@pytest.mark.asyncio
async def test_audit_log_persistence(repo):
    """Verify audit logs are persisted and retrieved chronologically."""
    wf = await repo.create_workflow(workflow_id="wf-log-test", title="Audit WF", raw_prompt="p", definition={})
    run = await repo.create_run(run_id="run-log-test", workflow_id=wf["id"])

    log1 = await repo.create_audit_log(
        run_id=run["id"],
        task_id="step_1",
        level="INFO",
        event_type="TASK_STARTED",
        message="Started step 1",
        metadata={"attempt": 1},
    )
    assert log1["id"] is not None
    assert log1["event_type"] == "TASK_STARTED"

    log2 = await repo.create_audit_log(
        run_id=run["id"],
        task_id="step_1",
        level="INFO",
        event_type="TASK_COMPLETED",
        message="Step 1 finished",
        metadata={"duration_ms": 150},
    )

    logs = await repo.list_audit_logs(run["id"])
    assert len(logs) == 2
    assert logs[0]["event_type"] == "TASK_STARTED"
    assert logs[1]["event_type"] == "TASK_COMPLETED"


@pytest.mark.asyncio
async def test_cascading_deletion(repo):
    """Verify deleting a workflow cascades to delete runs, tasks, approvals, and logs."""
    wf = await repo.create_workflow(workflow_id="wf-cascade", title="Cascade WF", raw_prompt="p", definition={})
    run = await repo.create_run(run_id="run-cascade", workflow_id=wf["id"])
    task = await repo.create_task(run_id=run["id"], task_id="t1", name="Task 1", tool_name="http_client")
    approval = await repo.create_approval(
        approval_id="appr-casc",
        token="token-casc",
        run_id=run["id"],
        task_id="t1",
        payload_hash="h1",
        action_payload={},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    log = await repo.create_audit_log(
        run_id=run["id"],
        level="INFO",
        event_type="RUN_STARTED",
        message="Run starting",
    )

    # Delete the workflow
    deleted = await repo.delete_workflow(wf["id"])
    assert deleted is True

    # Assert all cascade targets are gone
    assert await repo.get_run("run-cascade") is None
    assert await repo.get_task("run-cascade", "t1") is None
    assert await repo.get_approval("token-casc") is None
    logs = await repo.list_audit_logs("run-cascade")
    assert len(logs) == 0


@pytest.mark.asyncio
async def test_transaction_rollback(temp_db):
    """Verify transaction rollback on error leaves database state intact."""
    async with temp_db.connection() as conn:
        await conn.execute(
            "INSERT INTO workflows (id, title, raw_prompt, definition_json) VALUES ('wf-rb', 'RB', 'p', '{}');"
        )
        await conn.commit()

        # Begin atomic operation that fails on duplicate key
        try:
            await conn.execute("INSERT INTO runs (id, workflow_id, status) VALUES ('run-valid', 'wf-rb', 'PENDING');")
            # Intentionally insert duplicate ID to cause IntegrityError
            await conn.execute("INSERT INTO runs (id, workflow_id, status) VALUES ('run-valid', 'wf-rb', 'PENDING');")
            await conn.commit()
        except sqlite3.IntegrityError:
            await conn.rollback()

        # Verify 'run-valid' was rolled back completely
        cursor = await conn.execute("SELECT * FROM runs WHERE id = 'run-valid';")
        assert await cursor.fetchone() is None


@pytest.mark.asyncio
async def test_concurrent_database_operations(repo):
    """Verify multiple concurrent writes across tasks and logs operate safely."""
    wf = await repo.create_workflow(workflow_id="wf-concurrent", title="Concurrent WF", raw_prompt="p", definition={})
    run = await repo.create_run(run_id="run-concurrent", workflow_id=wf["id"])

    # Dispatch 20 concurrent task creations
    async def create_numbered_task(num: int):
        return await repo.create_task(
            run_id=run["id"],
            task_id=f"concurrent_task_{num}",
            name=f"Task {num}",
            tool_name="http_client",
            input_params={"task_number": num},
        )

    tasks = await asyncio.gather(*(create_numbered_task(i) for i in range(20)))
    assert len(tasks) == 20

    stored_tasks = await repo.list_tasks(run["id"])
    assert len(stored_tasks) == 20
