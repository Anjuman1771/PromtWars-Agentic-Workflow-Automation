# FlowPilot AI: Intelligent Agentic Workflow Automation Platform

> **Challenge:** PS01 - Agentic Workflow Automation  
> **Status:** Phase 5 Complete (Gemini Workflow Planning & Deterministic Offline Fallback)

FlowPilot AI is an agentic workflow automation platform engineered for reliability, safety, and operational transparency. It translates natural language objectives into Directed Acyclic Graphs (DAGs) and executes tasks with strict validation, human-in-the-loop (HITL) approval gates, and zero-trust security guardrails.

---

## Key Capabilities

1. **Natural Language Workflow Planning:** Gemini structured output produces validated workflow DAGs, with a deterministic offline fallback for supported text intents.
2. **Deterministic DAG Execution:** Asynchronous topological scheduling with cycle detection and parallel execution.
3. **Safe-Only Tool Ecosystem:** Sandboxed file operations with workspace path containment, SSRF-protected HTTP client, and safe data transformations (strictly zero arbitrary shell or `eval` execution).
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
│   │   ├── agent/              # Structured Gemini planner & deterministic fallback
│   │   ├── tools/              # Safe tools (HTTP, file ops, search, data)
│   │   ├── storage/            # SQLite schema and repositories
│   │   └── api/                # FastAPI REST endpoints & SSE streaming
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

Other fallback requests fail with a clear planning error rather than receiving an invented workflow. The AI planner treats user prompts as untrusted input, validates structured responses against the Phase 4 workflow schema and DAG limits, permits only the planning tool identifier `transform`, and marks generated tasks as requiring approval. Planning never executes tasks; a future executor must independently validate tool authorization.

For production, set `FLOWPILOT_ENV=production` and configure `FLOWPILOT_SECRET_KEY` with a unique random secret of at least 32 characters. The development default and `.env.example` placeholder are rejected in production.

### Running Tests

```bash
pytest backend/tests
```
