"""Tests for asynchronous, validated Gemini workflow planning."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from backend.app.agent.planner import WorkflowPlanner, WorkflowPlanningError
from backend.app.config import Settings
from backend.app.core.models import WorkflowDefinition
from backend.app.safety.limits import ExecutionLimits


def valid_workflow(**task_overrides):
    task = {
        "id": "transform",
        "name": "Transform provided input",
        "tool": "transform",
        "input_parameters": {"operation": "summarize"},
        "outputs": ["result"],
        "requires_approval": False,
    }
    task.update(task_overrides)
    return {
        "name": "Generated workflow",
        "tasks": [task],
    }


def response_factory(result):
    calls = []

    async def generate_content(**kwargs):
        calls.append(kwargs)
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(text=result)

    def create_client(**kwargs):
        calls.append({"client": kwargs})
        return SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)
            )
        )

    return create_client, calls


def settings_with_key():
    return Settings(
        _env_file=None,
        GEMINI_API_KEY="test-key-not-for-model",
        GEMINI_MODEL="gemini-test",
    )


@pytest.mark.asyncio
async def test_valid_gemini_structured_response_uses_workflow_schema():
    create_client, calls = response_factory(json.dumps(valid_workflow()))
    planner = WorkflowPlanner(config=settings_with_key(), client_factory=create_client)

    result = await planner.plan("Summarize this text.")

    assert result.provider == "gemini"
    assert result.workflow.tasks[0].requires_approval is True
    assert result.metadata.model == "gemini-test"
    request = calls[1]
    assert request["model"] == "gemini-test"
    assert request["config"].response_mime_type == "application/json"
    assert request["config"].response_schema is WorkflowDefinition
    assert request["config"].http_options is None
    assert request["config"].system_instruction
    assert json.loads(request["contents"]) == {"user_request": "Summarize this text."}


@pytest.mark.asyncio
async def test_api_timeout_is_reported_without_retrying():
    create_client, calls = response_factory(asyncio.TimeoutError())
    planner = WorkflowPlanner(config=settings_with_key(), client_factory=create_client)

    with pytest.raises(WorkflowPlanningError, match="timed out"):
        await planner.plan("Summarize: words", use_fallback=False)

    assert len(calls) == 2
    assert calls[0]["client"]["http_options"].timeout == 30_000


@pytest.mark.asyncio
async def test_rate_limit_failure_is_reported_safely():
    class RateLimitError(errors.APIError):
        def __init__(self):
            Exception.__init__(self, "rate limited")
            self.code = 429

    create_client, _ = response_factory(RateLimitError())
    planner = WorkflowPlanner(config=settings_with_key(), client_factory=create_client)

    with pytest.raises(WorkflowPlanningError, match="rate limit"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
async def test_invalid_json_and_invalid_workflow_responses_fail_validation():
    for response in ("{not-json", json.dumps({"tasks": "not-a-list"})):
        create_client, _ = response_factory(response)
        planner = WorkflowPlanner(
            config=settings_with_key(),
            client_factory=create_client,
        )
        with pytest.raises(WorkflowPlanningError, match="failed validation"):
            await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow",
    [
        {
            "tasks": [
                {"id": "same", "name": "A", "tool": "transform"},
                {"id": "same", "name": "B", "tool": "transform"},
            ]
        },
        {
            "tasks": [
                {
                    "id": "task",
                    "name": "A",
                    "tool": "transform",
                    "dependencies": ["absent"],
                }
            ]
        },
    ],
)
async def test_invalid_dag_responses_are_rejected(workflow):
    create_client, _ = response_factory(json.dumps(workflow))
    planner = WorkflowPlanner(config=settings_with_key(), client_factory=create_client)

    with pytest.raises(WorkflowPlanningError, match="failed validation"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
async def test_configured_workflow_step_limit_applies_to_gemini_response():
    payload = {"tasks": [valid_workflow()["tasks"][0], {
        "id": "second",
        "name": "Second transform",
        "tool": "transform",
        "dependencies": ["transform"],
    }]}
    create_client, _ = response_factory(json.dumps(payload))
    planner = WorkflowPlanner(
        config=settings_with_key(),
        limits=ExecutionLimits(max_workflow_steps=1),
        client_factory=create_client,
    )

    with pytest.raises(WorkflowPlanningError, match="failed validation"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
async def test_oversized_gemini_response_is_rejected():
    payload = json.dumps(
        {
            "name": "Generated workflow",
            "description": "x" * 2048,
            "tasks": [valid_workflow()["tasks"][0]],
        }
    )
    create_client, _ = response_factory(payload)
    planner = WorkflowPlanner(
        config=settings_with_key(),
        limits=ExecutionLimits(max_payload_size_bytes=1024),
        client_factory=create_client,
    )

    with pytest.raises(WorkflowPlanningError, match="failed validation"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_overrides",
    [
        {"retries": 4},
        {"timeout_seconds": 31},
    ],
)
async def test_retry_and_timeout_limits_apply_to_gemini_response(task_overrides):
    create_client, _ = response_factory(
        json.dumps(valid_workflow(**task_overrides))
    )
    planner = WorkflowPlanner(
        config=settings_with_key(),
        client_factory=create_client,
    )

    with pytest.raises(WorkflowPlanningError, match="failed validation"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
async def test_zero_retry_limit_from_settings_is_enforced():
    create_client, _ = response_factory(
        json.dumps(valid_workflow(retries=1))
    )
    planner = WorkflowPlanner(
        config=Settings(
            _env_file=None,
            GEMINI_API_KEY="test-key-not-for-model",
            max_retries_per_task=0,
        ),
        client_factory=create_client,
    )

    with pytest.raises(WorkflowPlanningError, match="failed validation"):
        await planner.plan("Summarize: words", use_fallback=False)


@pytest.mark.asyncio
async def test_missing_api_key_uses_fallback_or_reports_forced_gemini_error():
    def no_client(**kwargs):
        raise AssertionError("Gemini must not be constructed without an API key.")

    planner = WorkflowPlanner(
        config=Settings(_env_file=None, gemini_api_key=None),
        client_factory=no_client,
    )

    result = await planner.plan("Summarize: a short note")
    assert result.provider == "fallback"
    assert result.metadata.fallback_reason == "missing_api_key"

    with pytest.raises(WorkflowPlanningError, match="GEMINI_API_KEY"):
        await planner.plan("Summarize: a short note", use_fallback=False)


@pytest.mark.asyncio
async def test_gemini_unavailable_uses_offline_fallback_by_default():
    import httpx

    create_client, _ = response_factory(
        httpx.ConnectError("network unavailable")
    )
    planner = WorkflowPlanner(config=settings_with_key(), client_factory=create_client)

    result = await planner.plan("Summarize: a short note")

    assert result.provider == "fallback"
    assert result.metadata.fallback_reason == "unavailable"
