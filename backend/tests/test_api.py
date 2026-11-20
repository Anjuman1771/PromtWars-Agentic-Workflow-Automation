"""API integration tests using temporary SQLite databases and mocked services."""

import asyncio

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, StrictStr

from backend.app.agent.schemas import PlanningMetadata, PlanningResult
from backend.app.api import create_app
from backend.app.core.models import WorkflowDefinition
from backend.app.execution.engine import ExecutionEngine
from backend.app.safety.approval import ApprovalTokenManager
from backend.app.storage.database import Database
from backend.app.storage.repository import WorkflowRepository
from backend.app.tools.registry import ToolRegistry


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: StrictStr


class FakePlanner:
    async def plan(self, prompt, use_fallback=None):
        return PlanningResult(
            provider="fallback",
            workflow=WorkflowDefinition(
                name="Generated",
                tasks=[{"id": "echo", "name": "Echo", "tool": "echo", "inputs": {"value": prompt}}],
            ),
            metadata=PlanningMetadata(fallback_reason="forced"),
        )


def make_client(tmp_path):
    database = Database(str(tmp_path / "api.db"))
    repository = WorkflowRepository(database)
    registry = ToolRegistry()
    registry.register("echo", EchoInput, lambda request: {"value": request.value})
    engine = ExecutionEngine(
        repository=repository,
        tool_registry=registry,
        approval_manager=ApprovalTokenManager(repository, ttl_seconds=60),
    )
    app = create_app(database=database, repository=repository, engine=engine, planner=FakePlanner())
    return TestClient(app), engine


def test_health_generate_workflow_crud_and_run(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        generated = client.post(
            "/api/v1/workflows/generate",
            json={"prompt": "hello", "use_fallback": True},
        )
        assert generated.status_code == 200
        assert generated.json()["workflow"]["name"] == "Generated"
        assert client.get("/api/v1/workflows").json()["items"] == []

        payload = {
            "name": "Saved",
            "tasks": [{"id": "echo", "name": "Echo", "tool": "echo", "inputs": {"value": "ok"}}],
        }
        saved = client.post("/api/v1/workflows", json=payload)
        assert saved.status_code == 201
        workflow_id = saved.json()["id"]
        assert client.get(f"/api/v1/workflows/{workflow_id}").status_code == 200
        assert client.get("/api/v1/workflows").json()["items"]

        result = client.post(f"/api/v1/workflows/{workflow_id}/run")
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        assert result.json()["state"] == "completed"
        assert client.get(f"/api/v1/runs/{run_id}").json()["tasks"][0]["status"] == "completed"
        assert client.get("/api/v1/runs").json()["items"]
        assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200


def test_cancel_pending_run_endpoint(tmp_path):
    client, engine = make_client(tmp_path)
    with client:
        workflow = WorkflowDefinition(
            name="Pending",
            tasks=[{"id": "echo", "name": "Echo", "tool": "echo", "inputs": {"value": "ok"}}],
        )
        # Create a durable pending run through the repository without starting it.
        async def create_pending():
            saved = await engine.repository.create_workflow(
                "wf_pending",
                "Pending",
                "test",
                workflow.model_dump(mode="json", by_alias=True),
            )
            await engine.repository.create_run("run_pending", saved["id"])
            await engine.repository.create_task(
                "run_pending", "echo", "Echo", "echo", input_params={"value": "ok"}
            )

        asyncio.run(create_pending())
        response = client.post("/api/v1/runs/run_pending/cancel")
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"


def test_invalid_workflow_missing_resources_and_validation_errors(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        invalid = client.post("/api/v1/workflows", json={"name": "Bad", "tasks": []})
        assert invalid.status_code == 422
        assert "input" not in invalid.text
        assert client.get("/api/v1/workflows/not-found").status_code == 404
        assert client.get("/api/v1/runs/not-found").status_code == 404
        assert client.get("/api/v1/runs/not-found/approvals").status_code == 404


def test_approval_api_never_returns_token_and_rejects_invalid_token(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        saved = client.post(
            "/api/v1/workflows",
            json={
                "name": "Approval",
                "tasks": [
                    {
                        "id": "echo",
                        "name": "Echo",
                        "tool": "echo",
                        "inputs": {"value": "ok"},
                        "requires_approval": True,
                    }
                ],
            },
        ).json()
        result = client.post(f"/api/v1/workflows/{saved['id']}/run").json()
        approval_id = result["pending_approvals"][0]["approval_id"]
        run_id = result["run_id"]
        assert "token" not in result["pending_approvals"][0]
        listed = client.get(f"/api/v1/runs/{run_id}/approvals").json()["items"]
        assert listed[0]["approval_id"] == approval_id
        assert "token" not in listed[0]
        bad = client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json={"token": "invalid", "action_payload": {}},
        )
        assert bad.status_code == 409
        assert client.post(
            f"/api/v1/approvals/{approval_id}/reject",
            json={"token": "invalid", "action_payload": {}},
        ).status_code == 409
        asyncio.run(
            client.app.state.repository.update_run_status(
                run_id, "COMPLETED", completed=True
            )
        )
        with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
            lines = list(response.iter_lines())
            assert response.status_code == 200
            assert any(line == "event: approval_required" for line in lines)


def test_valid_approval_uses_engine_token_and_payload_validation(tmp_path):
    client, engine = make_client(tmp_path)
    with client:
        workflow = WorkflowDefinition(
            name="Approved",
            tasks=[
                {
                    "id": "echo",
                    "name": "Echo",
                    "tool": "echo",
                    "inputs": {"value": "ok"},
                    "requires_approval": True,
                }
            ],
        )
        result = asyncio.run(engine.execute(workflow))
        approval = result.pending_approvals[0]
        response = client.post(
            f"/api/v1/approvals/{approval.approval_id}/approve",
            json={
                "token": approval.raw_token,
                "action_payload": approval.action_payload,
            },
        )
        assert response.status_code == 200
        assert response.json()["state"] == "completed"


def test_sse_emits_status_event_for_persisted_run(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        workflow_id = client.post(
            "/api/v1/workflows",
            json={
                "name": "Events",
                "tasks": [{"id": "echo", "name": "Echo", "tool": "echo", "inputs": {"value": "ok"}}],
            },
        ).json()["id"]
        run_id = client.post(f"/api/v1/workflows/{workflow_id}/run").json()["run_id"]
        with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
            assert response.status_code == 200
            lines = list(response.iter_lines())
            assert any(line.startswith("event: status") for line in lines)
            assert any(line.startswith("data: ") for line in lines)
