# FlowPilot AI: Intelligent Agentic Workflow Automation Platform

> **Challenge:** PS01 - Agentic Workflow Automation  
> **Status:** Phases 1–9 implemented (local MVP; see limitations below)

FlowPilot AI turns natural-language process requests into validated, inspectable workflows and provides a local dashboard for saving, running, and monitoring them. It is designed to make routine multi-step work easier to specify and orchestrate while retaining explicit tool authorization, human approval gates, bounded execution, and clear run state.

**Chosen vertical:** team operations and lightweight internal process automation. **Target users:** operations analysts, project coordinators, and technical teams who want to prototype repeatable workflows without granting an AI arbitrary code or shell access.

---

## Key Capabilities

1. **Natural Language Workflow Planning:** Gemini structured output produces validated workflow DAGs, with a deterministic offline fallback for supported text intents.
2. **Deterministic DAG Execution:** Asynchronous topological scheduling with cycle detection and parallel execution.
3. **Safe-Only Tool Ecosystem:** Explicitly registered workspace file, restricted HTTP, JSON transformation, and local search tools; no shell or arbitrary-code execution.
4. **Hardened Human-in-the-Loop (HITL):** Expiring, single-use, payload-bound approval tokens with atomic database consumption.
5. **Circuit Breakers & Resource Limits:** Distinct failure-rate circuit breakers and execution resource bounds (max steps, timeouts, payload sizes).
6. **Persistent Crash Recovery:** SQLite checkpointing enables state restoration and prevents duplicate task execution upon application restart.
7. **Observability & Secret Redaction:** Structured JSON logging that automatically strips Google API keys, Bearer tokens, JWTs, AWS credentials, and passwords.
8. **Zero-Key Offline Support:** Deterministic fallback supports explicitly documented text summarization and key-point extraction requests without network access.

---

## Architectural Layout

```text
PromtWars-Agentic-Workflow-Automation/
├── pyproject.toml              # Primary packaging & dependencies
├── requirements.txt            # Synchronized dependencies
├── .env.example                # Safe configuration template
│
├── backend/
│   ├── app/
│   │   ├── config.py           # Pydantic Settings configuration
│   │   ├── observability/      # Structured JSON logging & secret redaction
│   │   ├── safety/             # Security guards, limits, circuit breaker, HITL
│   │   ├── core/               # Typed DAG models, validation, planning & interpolation
│   │   ├── execution/          # Async workflow execution, approvals & recovery
│   │   ├── agent/              # Structured Gemini planner & deterministic fallback
│   │   ├── tools/              # Safe tools (HTTP, file ops, search, data)
│   │   ├── storage/            # SQLite schema and repositories
│   │   ├── api.py              # FastAPI factory, REST endpoints & SSE
│   │   └── main.py             # Uvicorn entry point
│   └── tests/                  # Deterministic offline test suite
│
└── frontend/                   # React 18 + Vite + Tailwind + React Flow
```

---

## Getting Started

### Prerequisites
* Python 3.12+
* Node.js 20+ (for frontend development)

### Backend Installation

```bash
# Optional: create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Set `GEMINI_API_KEY` to a Google Gemini API key and optionally set `GEMINI_MODEL` (defaults to `gemini-2.5-flash`). A Gemini web/app subscription does **not** automatically provide Gemini API access or API credits; API usage is separate and may require its own billing setup.

Without an API key, or when Gemini is unavailable, the planner uses a deterministic offline fallback. It supports only these explicit request formats:

```text
Summarize: <text>
Extract key points: <text>
```

Other fallback requests fail with a clear planning error rather than receiving an invented workflow. The AI planner treats user prompts as untrusted input, validates structured responses against the workflow schema and DAG limits, permits only the planning tool identifier `transform`, and marks generated tasks as requiring approval. Planning never executes tasks; the Phase 7 executor independently validates tool authorization.

### Phase 6 safe tool boundary

The central tool registry exposes only registered identifiers and validates input against each tool's Pydantic schema before invocation. Phase 5 still produces plans only: planned identifiers must pass its existing planning allowlist and a registry authorization check. This phase does not implement workflow scheduling, approvals, or task execution; Phase 7 must independently authorize and validate every task at the execution boundary.

File operations are confined to `FLOWPILOT_WORKSPACE_ROOT`, reject resolved paths outside it, refuse symlink traversal, and apply per-file and output limits. As with ordinary path-based file APIs, they are not hardened against a separate untrusted process concurrently replacing workspace entries; keep the workspace protected from concurrent untrusted writes. HTTP requests support only GET, HEAD, POST, PUT, PATCH, and DELETE over HTTP(S). DNS answers are validated as globally routable and connections are pinned to validated addresses; HTTPS certificate verification uses the original hostname. Every redirect is re-resolved and validated, redirects cannot downgrade HTTPS, sensitive request headers are stripped on redirects, and response bodies and request durations are bounded. Deployments should still apply network egress controls for their environment. The local search provider performs deterministic, read-only literal matching over bounded workspace files; no external provider or search network access is included.

JSON transformations support `parse`, `select`, and `filter`; the offline planner also uses bounded deterministic `summarize` and `extract_key_points` text operations (first three/five sentences respectively, not semantic AI summarization). Select paths use only `$`, `.object_key`, and `[array_index]`; quoted keys, wildcards, expressions, and recursive descent are unsupported. Filter operators are `equals`, `not_equals`, and `contains`. These operations never evaluate user-provided code.

For production, set `FLOWPILOT_ENV=production` and configure `FLOWPILOT_SECRET_KEY` with a unique random secret of at least 32 characters. The development default and `.env.example` placeholder are rejected in production.

### Phase 7 execution engine

The asynchronous `ExecutionEngine` accepts a validated `WorkflowDefinition`, revalidates the DAG and every registered tool, and schedules dependency-ready tasks up to the configured concurrency limit. Tools are invoked only through the central registry; the engine does not evaluate arbitrary code or shell commands. Run and task states, sanitized outputs/errors, approval records, and idempotency results are persisted in SQLite.

The engine can be used directly from async application code:

```python
engine = ExecutionEngine(repository=workflow_repository, tool_registry=tool_registry)
result = await engine.execute(workflow)

for approval in result.pending_approvals:
    # Retain the token and payload securely; only their hashes are persisted.
    result = await engine.decide_approval(
        approval.run_id,
        approval.task_id,
        approval.raw_token,
        approval.action_payload,
        decision="APPROVE",  # or "REJECT"
    )
```

Approval tokens are single-use, expire, and are bound to the exact run, task, tool, and arguments. New Phase 7 approval records store only the token hash and payload hash, not the raw token or arguments. Approval rows created by older versions are not rewritten by the Phase 7 migration and may retain their prior payload data. The caller must retain each returned token and its matching action payload securely to approve after a process restart. Losing either makes that approval unusable; the task will fail closed rather than invoke without approval. Approval-wait time counts against the configured workflow deadline.

Call `await engine.recover_interrupted_runs()` once during startup, before accepting new execution requests. Safe pending work is resumed, valid pending approvals remain paused, and completed idempotency results are restored. A task whose final outcome is ambiguous is marked with a persisted `reconciliation_required` flag and failed closed; once marked, it is never retried or automatically re-invoked. Transient retries are limited to tools explicitly registered as retry-safe and failures known before tool dispatch. Once a tool is dispatched, an exception leaves the outcome uncertain and is not automatically retried; timeout or cancellation before its result and task output are durably recorded is handled the same way. A synchronous worker may finish after timeout or cancellation, but late completion cannot overwrite the task's terminal reconciliation state. Operators can distinguish these uncertain outcomes through `ExecutionResult.reconciliation_required`; the flag and task transition are written atomically and survive restart. This is intentionally at-most-once recovery behavior, not an exactly-once guarantee for external side effects. Inputs or outputs redacted before persistence cannot be reconstructed after restart; dependent work requiring those values is blocked for safety. Raw inputs/outputs remain in process memory only while a run is active or waiting for approval and are released when the run reaches a terminal state.

The legacy SQLite task-table rebuild preserves the repository's supported schema. Custom task-table columns, indexes, or triggers outside that schema are not guaranteed to survive the rebuild.

### Phase 8 FastAPI MVP

Launch the API from the repository root with `.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`. The app initializes SQLite and performs fail-closed interrupted-run recovery during startup. The API reuses the workflow planner, validator, repositories, tool registry, and execution engine; generated workflows are returned for review and are never run automatically.

The MVP is intended for trusted local use and does not provide authentication or authorization. Keep it bound to loopback unless deployed behind an authenticated gateway. CORS origins come from `FLOWPILOT_CORS_ORIGINS`. Approval endpoints require the original approval token and exact action payload supplied by a trusted caller; this API deliberately never returns raw tokens or approval arguments. The initiating client must obtain those securely out-of-band to complete approval.

Run events are streamed by polling persisted run/task/approval state and include status changes and heartbeats. Event IDs are connection-local; durable event replay and `Last-Event-ID` resumption are not supported. The workflow run endpoint waits for execution to reach a pause or terminal state before responding.

### Phase 9 React frontend

From the repository root, start the API with `.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`. In another terminal run `cd frontend`, `npm install`, and `npm run dev`. The Vite app is available at `http://127.0.0.1:5173`; override the API URL by setting `VITE_API_BASE_URL` in `frontend/.env.local` before starting or building.

The dashboard uses live API data. Workflow generation only creates a draft; save it before running. Run execution currently waits for the Phase 8 API response before the browser can attach to SSE, so in-progress updates during a long synchronous run are limited. Browser EventSource reconnects to the latest persisted snapshot, but backend event replay is not available. Approval tokens and exact action payloads are not returned by the API; the Approvals page requires a trusted initiator to provide both out-of-band.

### Running Tests

Use the repository virtual environment from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Final verification on 2026-11-20: **209 passed, 2 skipped**. The build and backend tests were run locally; no live Gemini or external tool service is required by the test suite. A local browser smoke test verified health, API connectivity, fallback generation, graph/task inspection, save, run submission, approval-paused status, run history, and live dashboard counts. A separately submitted no-external-side-effect transform workflow completed and its persisted output appeared in run detail. The generated workflow remained paused because its approval token/payload are intentionally not exposed by the API.

## Windows MVP setup

Prerequisites: Python 3.12, Node.js/npm, and PowerShell. From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` only on your machine. Leave `GEMINI_API_KEY` blank for offline fallback or provide a Google Gemini API key to enable Gemini planning. Start the backend from the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```powershell
Set-Location frontend
npm install
Copy-Item .env.example .env.local
npm run dev
```

Open `http://127.0.0.1:5173`. To verify a production frontend bundle, run `npm run build` in `frontend`. Configure a different backend URL with `VITE_API_BASE_URL` in `frontend/.env.local` before starting/building. The backend binds to loopback by default and allows the local Vite origins in `FLOWPILOT_CORS_ORIGINS`.

## Technology stack

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLite/aiosqlite, Uvicorn.
- Planning: Google Gemini structured output when configured; deterministic, supported-intent fallback otherwise.
- Frontend: React 18, TypeScript, Vite, Tailwind CSS, `@xyflow/react`, Lucide React.
- Persistence and safety: SQLite repositories, explicit tool registry, Pydantic tool schemas, redaction, approval tokens, execution limits, circuit breakers, and recovery.

## Planning, validation, and execution

The planner asks Gemini for a structured `WorkflowDefinition` and validates its schema, supported tool vocabulary, task limits, and DAG. If no Gemini key is configured, the offline planner supports `Summarize: <text>` and `Extract key points: <text>`; unsupported prompts fail instead of receiving an invented workflow. Generated tasks require approval. Planning only returns a draft.

The DAG validator checks task identifiers, dependencies, cycles, payload limits, and execution bounds. Before invocation, the execution engine validates the DAG and the central registry authorization/schema. Dependency-ready tasks can run concurrently within configured limits. No generated Python, shell, or arbitrary code is executed. Run/task transitions, sanitized outputs, approval hashes, and idempotency results are persisted in SQLite.

Approval tokens are single-use, expire, and bind to the run, task, tool, and exact action payload. The API does not return raw tokens or arguments; the trusted initiator must provide them out-of-band to the Approvals page. Uncertain synchronous side effects are marked for manual reconciliation and are not automatically retried. Recovery favors avoiding duplicate side effects over automatic completion; exactly-once external execution is not guaranteed.

## API overview

All routes use `/api/v1`:

- `GET /health`
- `POST /workflows/generate`, `POST /workflows`, `GET /workflows`, `GET /workflows/{workflow_id}`, `POST /workflows/{workflow_id}/run`
- `GET /runs`, `GET /runs/{run_id}`, `POST /runs/{run_id}/cancel`
- `GET /runs/{run_id}/approvals`, `POST /approvals/{approval_id}/approve`, `POST /approvals/{approval_id}/reject`
- `GET /runs/{run_id}/events` (SSE status polling and heartbeats)

The frontend dashboard and builder use these routes directly through one API client. Generated workflows are never run automatically.

## Environment variables

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Optional Gemini API credential; leave blank to use offline planning. Never place a real key in tracked files. |
| `GEMINI_MODEL` | Gemini model name; defaults to `gemini-2.5-flash`. |
| `FLOWPILOT_ENV`, `FLOWPILOT_DEBUG` | Backend environment and debug behavior. |
| `FLOWPILOT_HOST`, `FLOWPILOT_PORT` | Backend bind settings; use loopback for local MVP use. |
| `FLOWPILOT_CORS_ORIGINS` | JSON array of allowed browser origins. |
| `FLOWPILOT_SECRET_KEY` | Required strong unique value in production mode. |
| `FLOWPILOT_DB_PATH` | SQLite database path; defaults to `flowpilot.db`. |
| `FLOWPILOT_WORKSPACE_ROOT` | File-tool containment root. |
| `FLOWPILOT_MAX_WORKFLOW_STEPS`, `FLOWPILOT_MAX_RUN_TIME_SECONDS`, `FLOWPILOT_DEFAULT_TASK_TIMEOUT_SECONDS` | Execution resource limits. |
| `VITE_API_BASE_URL` | Frontend API base URL; defaults to `http://127.0.0.1:8000/api/v1`. |

`.env.example` contains development placeholders only. Do not copy real secrets into `.env.example` or frontend build variables.

## Security assumptions and known limitations

- The API does not implement authentication or authorization. It is for trusted local use only; deployments outside loopback need an authenticated gateway and network controls.
- The workflow run endpoint waits for the execution engine to pause or finish before responding. Browser SSE attaches afterward, so long-running tasks do not show live intermediate progress in the UI.
- SSE snapshots are polled from current database state. Event IDs are connection-local and `Last-Event-ID` replay is not implemented.
- The API intentionally does not expose approval token material. API-only users cannot complete an approval unless a trusted initiator supplies the original token and exact payload out-of-band.
- External side effects may be ambiguous after timeout, cancellation, or process failure. Such tasks require manual reconciliation; exactly-once execution is not claimed.
- Older approval rows may retain payload data from earlier versions. Redacted inputs/outputs cannot be reconstructed for dependent work.
- The browser UI was exercised locally, but no screenshot image is committed in this repository.

## Screenshots

No screenshot asset is currently committed. The dashboard and workflow builder were verified in the integrated browser during final smoke testing.
