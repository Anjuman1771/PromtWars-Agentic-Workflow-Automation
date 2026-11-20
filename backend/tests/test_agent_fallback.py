"""Tests for deterministic offline workflow generation."""

import pytest

from backend.app.agent.fallback import (
    SUPPORTED_FALLBACK_INTENTS,
    UnsupportedFallbackRequestError,
    generate_fallback_workflow,
)
from backend.app.agent.planner import WorkflowPlanner
from backend.app.config import Settings
from backend.app.tools.registry import default_tool_registry


@pytest.mark.parametrize(
    ("prompt", "expected_operation", "expected_output"),
    [
        ("Summarize: a short note", "summarize", "summary"),
        ("Extract key points: three facts", "extract_key_points", "key_points"),
    ],
)
def test_offline_fallback_supports_documented_text_intents(
    prompt,
    expected_operation,
    expected_output,
):
    workflow = generate_fallback_workflow(prompt)

    assert workflow.tasks[0].tool_name == "transform"
    assert workflow.tasks[0].input_parameters["operation"] == expected_operation
    assert workflow.tasks[0].input_parameters["data"]
    assert "text" not in workflow.tasks[0].input_parameters
    assert workflow.tasks[0].outputs == [expected_output]
    assert workflow.tasks[0].requires_approval is True


def test_fallback_is_deterministic_for_identical_input():
    prompt = "Summarize: a short note"

    first = generate_fallback_workflow(prompt).model_dump(mode="json")
    second = generate_fallback_workflow(prompt).model_dump(mode="json")

    assert first == second


@pytest.mark.parametrize(
    ("prompt", "output_key", "expected"),
    [
        ("Summarize: First. Second. Third. Fourth.", "summary", "First. Second. Third."),
        ("Extract key points: Alpha. Beta.", "key_points", ["Alpha.", "Beta."]),
    ],
)
def test_fallback_arguments_execute_through_registered_transform(
    prompt,
    output_key,
    expected,
):
    task = generate_fallback_workflow(prompt).tasks[0]
    validated = default_tool_registry.validate_inputs(
        task.tool_name, task.input_parameters
    )
    result = default_tool_registry.execute_validated(task.tool_name, validated)

    assert result[output_key] == expected


def test_unsupported_offline_request_fails_clearly():
    with pytest.raises(UnsupportedFallbackRequestError, match="does not support"):
        generate_fallback_workflow("Build a complex research workflow.")

    assert SUPPORTED_FALLBACK_INTENTS == (
        "Summarize: <text>",
        "Extract key points: <text>",
    )


@pytest.mark.asyncio
async def test_forced_fallback_does_not_construct_gemini_client():
    def no_client(**kwargs):
        raise AssertionError("Offline fallback must not create a Gemini client.")

    planner = WorkflowPlanner(
        config=Settings(_env_file=None, gemini_api_key="unused"),
        client_factory=no_client,
    )

    result = await planner.plan("Summarize: a short note", use_fallback=True)

    assert result.provider == "fallback"
    assert result.metadata.fallback_reason == "forced"
