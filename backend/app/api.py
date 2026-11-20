"""FastAPI application for the trusted-local FlowPilot MVP."""

import asyncio
from contextlib import asynccontextmanager
import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent.planner import WorkflowPlanner
from .config import settings
from .core.models import WorkflowDefinition
from .execution.engine import ExecutionEngine
from .execution.models import ExecutionResult
from .observability.logger import get_logger
from .safety.exceptions import (
    ApprovalException,
    DuplicateExecutionError,
    FlowPilotException,
    InvalidWorkflowSchemaError,
)
from .safety.redaction import redact_object, redact_text
from .storage.database import Database, db as default_db
from .storage.repository import WorkflowRepository

api_logger = get_logger("flowpilot.api")


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=20_000)
    use_fallback: bool | None = None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=256)
    action_payload: dict[str, Any]


def _result_json(result: ExecutionResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "state": result.state.value,
        "task_states": {key: value.value for key, value in result.task_states.items()},
        "outputs": redact_object(result.outputs),
        "errors": {key: redact_text(value) for key, value in result.errors.items()},
        "reconciliation_required": result.reconciliation_required,
        "pending_approvals": [
            {
                "approval_id": item.approval_id,
                "run_id": item.run_id,
                "task_id": item.task_id,
                "tool_name": item.tool_name,
                "expires_at": item.expires_at,
            }
            for item in result.pending_approvals
        ],
    }


def create_app(
    *,
    database: Database | None = None,
    repository: WorkflowRepository | None = None,
    engine: ExecutionEngine | None = None,
    planner: WorkflowPlanner | None = None,
) -> FastAPI:
    """Create the API with injectable persistence, execution, and planning services."""
    resolved_database = database or (repository.db if repository else default_db)
    resolved_repository = repository or WorkflowRepository(resolved_database)
    resolved_engine = engine or ExecutionEngine(repository=resolved_repository)
    resolved_planner = planner or WorkflowPlanner(
        tool_registry=resolved_engine.tool_registry
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await resolved_database.init_db()
        await resolved_engine.recover_interrupted_runs()
        yield

    app = FastAPI(title="FlowPilot AI API", version="1.0.0", lifespan=lifespan)
    app.state.database = resolved_database
    app.state.repository = resolved_repository
    app.state.engine = resolved_engine
    app.state.planner = resolved_planner
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Request validation failed."}},
        )

    @app.exception_handler(FlowPilotException)
    async def domain_error(_: Request, exc: FlowPilotException):
        if isinstance(exc, (ApprovalException, DuplicateExecutionError)):
            status = 409
        elif isinstance(exc, InvalidWorkflowSchemaError):
            status = 422
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content={"error": {"code": type(exc).__name__, "message": redact_text(exc.message)}},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail)}},
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception):
        api_logger.error(
            "Unhandled API exception.",
            extra={"data": {"error_type": type(exc).__name__}},
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "An internal error occurred."}},
        )

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/workflows/generate")
    async def generate_workflow(body: GenerateRequest):
        planned = await resolved_planner.plan(body.prompt, use_fallback=body.use_fallback)
        return {
            "provider": planned.provider,
            "metadata": planned.metadata.model_dump(),
            "workflow": planned.workflow.model_dump(mode="json", by_alias=True),
        }

    @app.post("/api/v1/workflows", status_code=201)
    async def save_workflow(workflow: WorkflowDefinition):
        if not workflow.tasks:
            raise InvalidWorkflowSchemaError("A workflow must contain at least one task.")
        resolved_engine.validator.validate(workflow)
        resolved_engine.tool_registry.validate_workflow(workflow)
        workflow_id = f"wf_{uuid.uuid4().hex}"
        record = await resolved_repository.create_workflow(
            workflow_id=workflow_id,
            title=workflow.name or "Untitled workflow",
            description=workflow.description,
            raw_prompt="Submitted through the FlowPilot API.",
            definition=workflow.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return record

    @app.get("/api/v1/workflows")
    async def list_workflows(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return {"items": await resolved_repository.list_workflows(limit, offset)}

    @app.get("/api/v1/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        record = await resolved_repository.get_workflow(workflow_id)
        if record is None:
            raise HTTPException(404, "Workflow not found.")
        return record

    @app.post("/api/v1/workflows/{workflow_id}/run", status_code=202)
    async def start_run(workflow_id: str):
        record = await resolved_repository.get_workflow(workflow_id)
        if record is None:
            raise HTTPException(404, "Workflow not found.")
        workflow = WorkflowDefinition.model_validate(record["definition_json"])
        return _result_json(await resolved_engine.execute(workflow))

    @app.get("/api/v1/runs")
    async def list_runs(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        runs = await resolved_repository.list_runs(limit=limit, offset=offset)
        return {"items": runs}

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        run = await resolved_repository.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found.")
        tasks = await resolved_repository.list_tasks(run_id)
        return {
            "run": run,
            "tasks": [
                {
                    "task_id": row["task_id"],
                    "name": row["name"],
                    "tool_name": row["tool_name"],
                    "status": str(row["status"]).lower(),
                    "error": redact_text(row["error_message"]) if row.get("error_message") else None,
                    "output": redact_object(row.get("output_data_json")),
                    "reconciliation_required": bool(row.get("reconciliation_required")),
                }
                for row in tasks
            ],
        }

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        if await resolved_repository.get_run(run_id) is None:
            raise HTTPException(404, "Run not found.")
        return _result_json(await resolved_engine.cancel(run_id))

    @app.get("/api/v1/runs/{run_id}/approvals")
    async def list_approvals(run_id: str):
        run = await resolved_repository.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found.")
        workflow = await resolved_repository.get_workflow(str(run["workflow_id"]))
        rows = await resolved_repository.list_pending_approvals(run_id)
        items = []
        for row in rows:
            task = await resolved_repository.get_task(run_id, str(row["task_id"]))
            items.append(
                {
                    "approval_id": row["id"],
                    "run_id": row["run_id"],
                    "workflow_name": workflow.get("title") if workflow else None,
                    "task_id": row["task_id"],
                    "task_name": task.get("name") if task else None,
                    "tool_name": task.get("tool_name") if task else None,
                    "status": row["status"],
                    "expires_at": row["expires_at"],
                }
            )
        return {"items": items}

    async def decide_approval(approval_id: str, body: ApprovalDecisionRequest, decision: str):
        approval = await resolved_repository.get_approval_by_id(approval_id)
        if approval is None:
            raise HTTPException(404, "Approval not found.")
        result = await resolved_engine.decide_approval(
            str(approval["run_id"]),
            str(approval["task_id"]),
            body.token,
            body.action_payload,
            decision=decision,
        )
        return _result_json(result)

    @app.post("/api/v1/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: ApprovalDecisionRequest):
        return await decide_approval(approval_id, body, "APPROVE")

    @app.post("/api/v1/approvals/{approval_id}/reject")
    async def reject(approval_id: str, body: ApprovalDecisionRequest):
        return await decide_approval(approval_id, body, "REJECT")

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str):
        if await resolved_repository.get_run(run_id) is None:
            raise HTTPException(404, "Run not found.")

        async def stream():
            previous: str | None = None
            sequence = 0
            while not await request.is_disconnected():
                run = await resolved_repository.get_run(run_id)
                tasks = await resolved_repository.list_tasks(run_id)
                snapshot = {
                    "run": str(run["status"]).lower() if run else "unknown",
                    "tasks": {str(row["task_id"]): str(row["status"]).lower() for row in tasks},
                    "approvals": [
                        {"approval_id": row["id"], "task_id": row["task_id"]}
                        for row in await resolved_repository.list_pending_approvals(run_id)
                    ],
                }
                encoded = json.dumps(snapshot, separators=(",", ":"))
                if encoded != previous:
                    sequence += 1
                    event_type = (
                        "approval_required"
                        if snapshot["approvals"]
                        and (previous is None or snapshot["approvals"] != json.loads(previous)["approvals"])
                        else "status"
                    )
                    yield f"id: {sequence}\nevent: {event_type}\ndata: {encoded}\n\n"
                    previous = encoded
                else:
                    yield ": heartbeat\n\n"
                if snapshot["run"] in {"completed", "failed", "cancelled", "timed_out"}:
                    break
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
