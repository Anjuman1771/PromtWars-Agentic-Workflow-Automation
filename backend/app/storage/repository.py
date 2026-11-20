"""FlowPilot AI Storage Repositories.

Provides parameterized, transaction-safe CRUD operations for Workflows,
Runs, Tasks, Approvals, Idempotency, and Audit Logs.
"""

from datetime import datetime, timezone
import json
from collections.abc import Iterable
from typing import Any, Dict, List, Optional
import sqlite3

from .database import Database, DatabaseError, db as default_db
from ..safety.redaction import redact_object, redact_text


def _ensure_json_str(val: Any) -> str:
    """Ensure value is a validated, redacted JSON string."""
    if val is None:
        return "{}"
    if isinstance(val, str):
        # Validate that it parses as JSON, then redact and re-serialize
        try:
            parsed = json.loads(val)
            return json.dumps(redact_object(parsed))
        except json.JSONDecodeError:
            # If plain string, wrap in string representation safely
            return json.dumps(redact_object(val))
    return json.dumps(redact_object(val))


def _parse_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Convert an SQLite Row to a dictionary with parsed JSON fields where appropriate."""
    if row is None:
        return None
    d = dict(row)
    # Parse standard JSON columns if present
    for json_col in ("definition_json", "depends_on_json", "input_params_json",
                     "output_data_json", "action_payload_json", "output_json", "metadata_json"):
        if json_col in d and d[json_col] is not None:
            try:
                d[json_col] = json.loads(d[json_col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def _parse_rows(
    rows: Iterable[Optional[sqlite3.Row]],
) -> List[Dict[str, Any]]:
    parsed_rows: List[Dict[str, Any]] = []
    for row in rows:
        parsed = _parse_row(row)
        if parsed is not None:
            parsed_rows.append(parsed)
    return parsed_rows


class WorkflowRepository:
    """Repository handling persistence for all FlowPilot AI domain entities."""

    def __init__(self, database: Optional[Database] = None):
        self.db = database or default_db

    # --------------------------------------------------------------------------
    # WORKFLOWS
    # --------------------------------------------------------------------------

    async def create_workflow(
        self,
        workflow_id: str,
        title: str,
        raw_prompt: str,
        definition: Any,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create and persist a new workflow definition."""
        definition_json = _ensure_json_str(definition)
        query = """
        INSERT INTO workflows (id, title, description, raw_prompt, definition_json)
        VALUES (?, ?, ?, ?, ?);
        """
        async with self.db.connection() as conn:
            await conn.execute(
                query,
                (
                    workflow_id,
                    redact_text(title),
                    redact_text(description) if description is not None else None,
                    redact_text(raw_prompt),
                    definition_json,
                ),
            )
            await conn.commit()
        return (await self.get_workflow(workflow_id)) or {}

    async def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a workflow by its unique ID."""
        query = "SELECT * FROM workflows WHERE id = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (workflow_id,))
            row = await cursor.fetchone()
            return _parse_row(row)

    async def list_workflows(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """List workflows ordered by creation date."""
        query = "SELECT * FROM workflows ORDER BY created_at DESC LIMIT ? OFFSET ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (limit, offset))
            rows = await cursor.fetchall()
            return _parse_rows(rows)

    async def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow definition (cascades to all associated runs, tasks, and logs)."""
        query = "DELETE FROM workflows WHERE id = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (workflow_id,))
            await conn.commit()
            return cursor.rowcount > 0

    # --------------------------------------------------------------------------
    # RUNS
    # --------------------------------------------------------------------------

    async def create_run(
        self,
        run_id: str,
        workflow_id: str,
        status: str = "PENDING",
        triggered_by: str = "user",
    ) -> Dict[str, Any]:
        """Create an execution run for a workflow."""
        query = """
        INSERT INTO runs (id, workflow_id, status, triggered_by, current_step_count)
        VALUES (?, ?, ?, ?, 0);
        """
        async with self.db.connection() as conn:
            await conn.execute(query, (run_id, workflow_id, status, triggered_by))
            await conn.commit()
        return (await self.get_run(run_id)) or {}

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a run record by ID."""
        query = "SELECT * FROM runs WHERE id = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (run_id,))
            row = await cursor.fetchone()
            return _parse_row(row)

    async def list_runs(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs with optional filtering by workflow and status."""
        filters = []
        params: List[Any] = []
        if workflow_id:
            filters.append("workflow_id = ?")
            params.append(workflow_id)
        if status:
            filters.append("status = ?")
            params.append(status)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"SELECT * FROM runs {where_clause} ORDER BY started_at DESC LIMIT ? OFFSET ?;"
        params.extend([limit, offset])

        async with self.db.connection() as conn:
            cursor = await conn.execute(query, tuple(params))
            rows = await cursor.fetchall()
            return _parse_rows(rows)

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        error_message: Optional[str] = None,
        current_step_count: Optional[int] = None,
        circuit_breaker_tripped: Optional[bool] = None,
        completed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Update run state, optional error message, and completion timestamp."""
        updates = ["status = ?"]
        params: List[Any] = [status]

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(redact_text(error_message))

        if current_step_count is not None:
            updates.append("current_step_count = ?")
            params.append(current_step_count)

        if circuit_breaker_tripped is not None:
            updates.append("circuit_breaker_tripped = ?")
            params.append(1 if circuit_breaker_tripped else 0)

        if completed:
            updates.append("completed_at = CURRENT_TIMESTAMP")

        params.append(run_id)
        query = f"UPDATE runs SET {', '.join(updates)} WHERE id = ?;"

        async with self.db.connection() as conn:
            cursor = await conn.execute(query, tuple(params))
            await conn.commit()
            if cursor.rowcount == 0:
                return None
        return await self.get_run(run_id)

    async def claim_run(self, run_id: str) -> bool:
        """Atomically acquire a run that is pending or waiting for approval."""
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE runs SET status = 'RUNNING'
                WHERE id = ? AND status IN ('PENDING', 'PAUSED_APPROVAL');
                """,
                (run_id,),
            )
            await conn.commit()
            return cursor.rowcount == 1

    # --------------------------------------------------------------------------
    # TASKS
    # --------------------------------------------------------------------------

    async def create_task(
        self,
        run_id: str,
        task_id: str,
        name: str,
        tool_name: str,
        status: str = "PENDING",
        requires_approval: bool = False,
        depends_on: Optional[List[str]] = None,
        input_params: Any = None,
        idempotency_key: Optional[str] = None,
        input_redacted: bool = False,
        reconciliation_required: bool = False,
    ) -> Dict[str, Any]:
        """Create a task instance within a run."""
        full_id = f"{run_id}:{task_id}"
        depends_on_json = _ensure_json_str(depends_on or [])
        input_params_json = _ensure_json_str(input_params or {})

        query = """
        INSERT INTO tasks (
            id, run_id, task_id, name, tool_name, status,
            requires_approval, depends_on_json, input_params_json,
            idempotency_key, input_redacted, reconciliation_required, retry_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
        """
        async with self.db.connection() as conn:
            await conn.execute(
                query,
                (
                    full_id,
                    run_id,
                    task_id,
                    name,
                    tool_name,
                    status,
                    1 if requires_approval else 0,
                    depends_on_json,
                    input_params_json,
                    idempotency_key,
                    1 if input_redacted else 0,
                    1 if reconciliation_required else 0,
                ),
            )
            await conn.commit()
        return (await self.get_task(run_id, task_id)) or {}

    async def set_task_idempotency_key(
        self,
        run_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> bool:
        """Persist the input fingerprint before a tool can be invoked."""
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE tasks SET idempotency_key = ?
                WHERE run_id = ? AND task_id = ?
                  AND status IN ('PENDING', 'PAUSED_APPROVAL')
                  AND reconciliation_required = 0
                  AND (idempotency_key IS NULL OR idempotency_key = ?);
                """,
                (idempotency_key, run_id, task_id, idempotency_key),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def claim_task(
        self,
        run_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> bool:
        """Atomically claim a pending task and persist its execution fingerprint."""
        async with self.db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE;")
            cursor = await conn.execute(
                """
                UPDATE tasks
                SET status = 'RUNNING',
                    idempotency_key = ?,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
                WHERE run_id = ? AND task_id = ? AND status = 'PENDING'
                  AND reconciliation_required = 0
                  AND (idempotency_key IS NULL OR idempotency_key = ?);
                """,
                (idempotency_key, run_id, task_id, idempotency_key),
            )
            if cursor.rowcount == 1:
                await conn.execute(
                    """
                    UPDATE runs SET current_step_count = current_step_count + 1
                    WHERE id = ?;
                    """,
                    (run_id,),
                )
            await conn.commit()
            return cursor.rowcount == 1

    async def transition_task_status(
        self,
        run_id: str,
        task_id: str,
        expected_statuses: tuple[str, ...],
        status: str,
        *,
        error_message: Optional[str] = None,
        retry_count: Optional[int] = None,
        completed: bool = False,
        reconciliation_required: Optional[bool] = None,
    ) -> bool:
        """Change task state only if its current state matches an expected state."""
        if not expected_statuses:
            return False
        updates = ["status = ?"]
        params: list[Any] = [status]
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(redact_text(error_message))
        if retry_count is not None:
            updates.append("retry_count = ?")
            params.append(retry_count)
        if reconciliation_required is not None:
            updates.append("reconciliation_required = ?")
            params.append(1 if reconciliation_required else 0)
        if completed:
            updates.append("completed_at = CURRENT_TIMESTAMP")
        placeholders = ", ".join("?" for _ in expected_statuses)
        params.extend([run_id, task_id, *expected_statuses])
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE tasks SET {", ".join(updates)}
                WHERE run_id = ? AND task_id = ? AND status IN ({placeholders});
                """,
                tuple(params),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def get_task(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific task in a run."""
        query = "SELECT * FROM tasks WHERE run_id = ? AND task_id = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (run_id, task_id))
            row = await cursor.fetchone()
            return _parse_row(row)

    async def list_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        """List all tasks associated with a given run."""
        query = "SELECT * FROM tasks WHERE run_id = ? ORDER BY started_at ASC, id ASC;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (run_id,))
            rows = await cursor.fetchall()
            return _parse_rows(rows)

    async def update_task_status(
        self,
        run_id: str,
        task_id: str,
        status: str,
        error_message: Optional[str] = None,
        retry_count: Optional[int] = None,
        started: bool = False,
        completed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Update task lifecycle state."""
        updates = ["status = ?"]
        params: List[Any] = [status]

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(redact_text(error_message))

        if retry_count is not None:
            updates.append("retry_count = ?")
            params.append(retry_count)

        if started:
            updates.append("started_at = CURRENT_TIMESTAMP")

        if completed:
            updates.append("completed_at = CURRENT_TIMESTAMP")

        params.extend([run_id, task_id])
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE run_id = ? AND task_id = ?;"

        async with self.db.connection() as conn:
            cursor = await conn.execute(query, tuple(params))
            await conn.commit()
            if cursor.rowcount == 0:
                return None
        return await self.get_task(run_id, task_id)

    async def update_task_output(
        self,
        run_id: str,
        task_id: str,
        output_data: Any,
        status: str = "COMPLETED",
        output_redacted: Optional[bool] = None,
        reconciliation_required: bool = False,
        expected_statuses: tuple[str, ...] = ("PENDING", "RUNNING"),
    ) -> Optional[Dict[str, Any]]:
        """Set task output payload and complete the task."""
        if not expected_statuses:
            return None
        output_data_json = _ensure_json_str(output_data)
        redacted_update = (
            "" if output_redacted is None else ", output_redacted = ?"
        )
        reconciliation_update = ", reconciliation_required = ?"
        params: list[Any] = [output_data_json, status]
        if output_redacted is not None:
            params.append(1 if output_redacted else 0)
        params.append(1 if reconciliation_required else 0)
        placeholders = ", ".join("?" for _ in expected_statuses)
        params.extend([run_id, task_id])
        query = """
        UPDATE tasks
        SET output_data_json = ?, status = ?, completed_at = CURRENT_TIMESTAMP
            {redacted_update}{reconciliation_update}
        WHERE run_id = ? AND task_id = ? AND status IN ({placeholders});
        """.format(
            redacted_update=redacted_update,
            reconciliation_update=reconciliation_update,
            placeholders=placeholders,
        )
        params.extend(expected_statuses)
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, tuple(params))
            await conn.commit()
            if cursor.rowcount == 0:
                return None
        return await self.get_task(run_id, task_id)

    # --------------------------------------------------------------------------
    # APPROVALS (HITL)
    # --------------------------------------------------------------------------

    async def create_approval(
        self,
        approval_id: str,
        token: str,
        run_id: str,
        task_id: str,
        payload_hash: str,
        action_payload: Any,
        expires_at: datetime | str,
    ) -> Dict[str, Any]:
        """Create a cryptographic approval token bound to run, task, and action payload."""
        action_payload_json = _ensure_json_str(action_payload)
        exp_str = expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at)

        query = """
        INSERT INTO approvals (
            id, token, run_id, task_id, payload_hash,
            action_payload_json, status, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?);
        """
        async with self.db.connection() as conn:
            await conn.execute(
                query,
                (approval_id, token, run_id, task_id, payload_hash, action_payload_json, exp_str),
            )
            await conn.commit()
        return (await self.get_approval(token)) or {}

    async def get_approval(self, token: str) -> Optional[Dict[str, Any]]:
        """Fetch an approval record by token."""
        query = "SELECT * FROM approvals WHERE token = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (token,))
            row = await cursor.fetchone()
            return _parse_row(row)

    async def get_approval_by_id(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Fetch an approval record by its public identifier."""
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE id = ?;", (approval_id,)
            )
            return _parse_row(await cursor.fetchone())

    async def list_pending_approvals(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List active pending approvals, optionally scoped to a specific run."""
        if run_id:
            query = """
            SELECT * FROM approvals
            WHERE run_id = ? AND status = 'PENDING' AND datetime(expires_at) > CURRENT_TIMESTAMP
            ORDER BY created_at ASC;
            """
            params = (run_id,)
        else:
            query = """
            SELECT * FROM approvals
            WHERE status = 'PENDING' AND datetime(expires_at) > CURRENT_TIMESTAMP
            ORDER BY created_at ASC;
            """
            params = ()

        async with self.db.connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return _parse_rows(rows)

    async def consume_approval(
        self,
        token: str,
        status: str,
        decision_by: str = "operator",
    ) -> Optional[Dict[str, Any]]:
        """Atomically transition token from PENDING to APPROVED/REJECTED.

        Guarantees single-use semantics: returns None if token was already consumed or expired.
        """
        query = """
        UPDATE approvals
        SET status = ?, consumed_at = CURRENT_TIMESTAMP, decision_by = ?
        WHERE token = ? AND status = 'PENDING' AND datetime(expires_at) > CURRENT_TIMESTAMP;
        """
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (status, decision_by, token))
            await conn.commit()
            if cursor.rowcount == 0:
                return None
        return await self.get_approval(token)

    async def get_latest_approval_for_task(
        self,
        run_id: str,
        task_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch the newest approval record, including expired and consumed records."""
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ? AND task_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1;
                """,
                (run_id, task_id),
            )
            row = await cursor.fetchone()
            return _parse_row(row)

    # --------------------------------------------------------------------------
    # IDEMPOTENCY
    # --------------------------------------------------------------------------

    async def get_cached_result(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Lookup previously executed tool result by deterministic fingerprint."""
        query = "SELECT * FROM idempotency_records WHERE idempotency_key = ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (idempotency_key,))
            row = await cursor.fetchone()
            return _parse_row(row)

    async def save_execution_result(
        self,
        idempotency_key: str,
        run_id: str,
        task_id: str,
        tool_name: str,
        output_data: Any,
        output_redacted: bool = False,
    ) -> Dict[str, Any]:
        """Store output of completed task for idempotency caching."""
        output_json = _ensure_json_str(output_data)
        query = """
        INSERT OR IGNORE INTO idempotency_records (
            idempotency_key, run_id, task_id, tool_name, output_json, output_redacted
        ) VALUES (?, ?, ?, ?, ?, ?);
        """
        async with self.db.connection() as conn:
            await conn.execute(
                query,
                (
                    idempotency_key,
                    run_id,
                    task_id,
                    tool_name,
                    output_json,
                    1 if output_redacted else 0,
                ),
            )
            await conn.commit()
        return (await self.get_cached_result(idempotency_key)) or {}

    # --------------------------------------------------------------------------
    # AUDIT LOGS
    # --------------------------------------------------------------------------

    async def create_audit_log(
        self,
        run_id: str,
        level: str,
        event_type: str,
        message: str,
        task_id: Optional[str] = None,
        metadata: Any = None,
    ) -> Dict[str, Any]:
        """Persist a structured, redacted audit log entry."""
        metadata_json = _ensure_json_str(metadata)
        query = """
        INSERT INTO audit_logs (run_id, task_id, level, event_type, message, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?);
        """
        async with self.db.connection() as conn:
            cursor = await conn.execute(
                query,
                (run_id, task_id, level, event_type, redact_text(message), metadata_json),
            )
            log_id = cursor.lastrowid
            await conn.commit()

            fetch_cursor = await conn.execute("SELECT * FROM audit_logs WHERE id = ?;", (log_id,))
            row = await fetch_cursor.fetchone()
            return _parse_row(row) or {}

    async def list_audit_logs(self, run_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch audit logs chronologically for a run."""
        query = "SELECT * FROM audit_logs WHERE run_id = ? ORDER BY id ASC LIMIT ?;"
        async with self.db.connection() as conn:
            cursor = await conn.execute(query, (run_id, limit))
            rows = await cursor.fetchall()
            return _parse_rows(rows)


# Global repository instance
repository = WorkflowRepository()
