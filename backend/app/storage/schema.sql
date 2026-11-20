-- FlowPilot AI SQLite Database Schema
-- Version: 0.1.0

PRAGMA foreign_keys = ON;

-- 1. Workflows (Reusable Definitions)
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    raw_prompt TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Execution Runs
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'PAUSED_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')),
    triggered_by TEXT DEFAULT 'user',
    current_step_count INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    circuit_breaker_tripped BOOLEAN DEFAULT 0,
    FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

-- 3. Tasks in a Run
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,          -- Format: "{run_id}:{task_id}"
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,        -- Graph node identifier (e.g. "fetch_data")
    name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'PAUSED_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')),
    requires_approval BOOLEAN DEFAULT 0,
    depends_on_json TEXT NOT NULL, -- JSON Array of prerequisite task_ids
    input_params_json TEXT NOT NULL,
    idempotency_key TEXT,
    input_redacted BOOLEAN DEFAULT 0,
    output_redacted BOOLEAN DEFAULT 0,
    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,
    output_data_json TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- 4. Cryptographic Single-Use Approval Tokens (HITL)
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,   -- SHA-256 of canonical action JSON
    action_payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP,
    decision_by TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- 5. Idempotency Records
CREATE TABLE IF NOT EXISTS idempotency_records (
    idempotency_key TEXT PRIMARY KEY, -- SHA-256(run_id, task_id, tool_name, normalized_input)
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    output_json TEXT NOT NULL,
    output_redacted BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- 6. Audit & Telemetry Log
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- Indexes for Fast Lookups and Crash Recovery
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_token ON approvals(token);
CREATE INDEX IF NOT EXISTS idx_approvals_run_task ON approvals(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_idempotency_lookup ON idempotency_records(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_logs(run_id);
