"""Small deterministic, offline workflow templates for supported text intents."""

import re

from ..core.models import TaskDefinition, WorkflowDefinition
from ..safety.exceptions import FlowPilotException
from .prompts import sanitize_user_prompt
from .schemas import ALLOWED_PLANNING_TOOLS

SUPPORTED_FALLBACK_INTENTS = (
    "Summarize: <text>",
    "Extract key points: <text>",
)

_INTENTS = (
    (
        re.compile(r"^\s*summarize\s*:\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL),
        "summarize",
        "Summarize supplied text",
        "summary",
        "Offline text summarization",
    ),
    (
        re.compile(r"^\s*extract\s+key\s+points\s*:\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL),
        "extract_key_points",
        "Extract key points from supplied text",
        "key_points",
        "Offline key-point extraction",
    ),
)


class UnsupportedFallbackRequestError(FlowPilotException):
    """Raised when a request does not match a documented offline intent."""


def generate_fallback_workflow(prompt: str) -> WorkflowDefinition:
    """Generate a deterministic one-task plan for a documented text intent."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise UnsupportedFallbackRequestError(
            "Offline planning requires a non-empty request."
        )
    prompt = sanitize_user_prompt(prompt)

    for pattern, operation, task_name, output_name, workflow_name in _INTENTS:
        match = pattern.fullmatch(prompt)
        if match is None:
            continue
        workflow = WorkflowDefinition(
            name=workflow_name,
            description="A planning-only text transformation; no task is executed.",
            tasks=[
                TaskDefinition(
                    id="transform_text",
                    name=task_name,
                    tool_name=next(iter(ALLOWED_PLANNING_TOOLS)),
                    input_parameters={
                        "operation": operation,
                        "text": match.group(1),
                    },
                    outputs=[output_name],
                    requires_approval=True,
                )
            ],
        )
        return workflow

    supported = "; ".join(SUPPORTED_FALLBACK_INTENTS)
    raise UnsupportedFallbackRequestError(
        f"Offline planning does not support this request. Supported formats: {supported}."
    )
