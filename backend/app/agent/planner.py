"""Asynchronous Gemini workflow planning with validated offline fallback."""

import asyncio
import ast
import json
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from ..config import Settings, settings
from ..core.models import TaskDefinition, WorkflowDefinition
from ..core.validator import WorkflowValidator
from ..observability.logger import get_logger
from ..safety.exceptions import (
    ExecutionLimitExceededException,
    FlowPilotException,
    InvalidWorkflowSchemaError,
)
from ..safety.limits import ExecutionLimits
from .fallback import generate_fallback_workflow
from .prompts import SYSTEM_INSTRUCTIONS, build_user_prompt, sanitize_user_prompt
from .schemas import ALLOWED_PLANNING_TOOLS, PlanningMetadata, PlanningResult

_REQUEST_TIMEOUT_MS = 30_000
_FORBIDDEN_INPUT_KEYS = {
    "bash",
    "code",
    "command",
    "commands",
    "executable",
    "powershell",
    "python_code",
    "script",
    "shell",
}
_EXECUTABLE_TEXT = re.compile(
    r"\b(?:eval|exec|subprocess|os\.system)\s*\(|"
    r"\b(?:python|bash|sh|powershell|cmd(?:\.exe)?)\s+-c\b|"
    r"```(?:python|bash|sh|shell|powershell)\b|^\s*#!",
    re.IGNORECASE | re.MULTILINE,
)


class WorkflowPlanningError(FlowPilotException):
    """Raised when Gemini planning fails and fallback is not selected."""


class WorkflowPlanner:
    """Plans workflows without executing them or granting tool authorization."""

    def __init__(
        self,
        config: Settings | None = None,
        limits: ExecutionLimits | None = None,
        client_factory: Callable[..., Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or settings
        self.limits = limits or ExecutionLimits(
            max_workflow_steps=self.config.max_workflow_steps,
            max_run_time_seconds=self.config.max_run_time_seconds,
            default_task_timeout_seconds=self.config.default_task_timeout_seconds,
            max_retries_per_task=self.config.max_retries_per_task,
            max_payload_size_bytes=self.config.max_payload_size_bytes,
        )
        self.validator = WorkflowValidator(limits=self.limits)
        self.client_factory = client_factory or genai.Client
        self.logger = logger or get_logger("flowpilot.agent.planner")

    async def plan(
        self,
        prompt: str,
        use_fallback: bool | None = None,
    ) -> PlanningResult:
        """Return a validated Gemini or offline workflow plan."""
        self._validate_prompt(prompt)
        safe_prompt = sanitize_user_prompt(prompt)
        api_key = self.config.gemini_api_key

        if use_fallback is True:
            return self._plan_offline(safe_prompt, reason="forced")
        if not api_key or not api_key.strip():
            if use_fallback is False:
                raise WorkflowPlanningError(
                    "Gemini planning was requested, but GEMINI_API_KEY is not configured."
                )
            return self._plan_offline(safe_prompt, reason="missing_api_key")

        try:
            workflow = await self._plan_with_gemini(safe_prompt, api_key.strip())
        except asyncio.TimeoutError:
            return self._handle_gemini_failure(
                safe_prompt, use_fallback, "timeout", "Gemini workflow planning timed out."
            )
        except httpx.TimeoutException:
            return self._handle_gemini_failure(
                safe_prompt, use_fallback, "timeout", "Gemini workflow planning timed out."
            )
        except httpx.RequestError:
            return self._handle_gemini_failure(
                safe_prompt, use_fallback, "unavailable", "Gemini is currently unavailable."
            )
        except errors.APIError as exc:
            if exc.code == 429:
                return self._handle_gemini_failure(
                    safe_prompt, use_fallback, "rate_limit", "Gemini rate limit was reached."
                )
            return self._handle_gemini_failure(
                safe_prompt, use_fallback, "api_error", "Gemini workflow planning failed."
            )
        except (InvalidWorkflowSchemaError, ExecutionLimitExceededException):
            return self._handle_gemini_failure(
                safe_prompt,
                use_fallback,
                "invalid_response",
                "Gemini returned a workflow that failed validation.",
            )

        return PlanningResult(
            provider="gemini",
            workflow=workflow,
            metadata=PlanningMetadata(model=self.config.gemini_model),
        )

    async def generate_workflow(
        self,
        prompt: str,
        use_fallback: bool | None = None,
    ) -> WorkflowDefinition:
        """Return only the validated workflow for endpoint-friendly callers."""
        return (await self.plan(prompt, use_fallback=use_fallback)).workflow

    def _validate_prompt(self, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowPlanningError("A non-empty workflow request is required.")
        self.limits.validate_payload_size(prompt)

    async def _plan_with_gemini(self, prompt: str, api_key: str) -> WorkflowDefinition:
        client = self.client_factory(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
        )
        response = await client.aio.models.generate_content(
            model=self.config.gemini_model,
            contents=build_user_prompt(prompt),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTIONS,
                response_mime_type="application/json",
                response_schema=WorkflowDefinition,
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )
        response_text = response.text
        if not isinstance(response_text, str) or not response_text.strip():
            raise InvalidWorkflowSchemaError(
                "Gemini returned an empty or non-text workflow response."
            )
        self.limits.validate_payload_size(response_text)
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            raise InvalidWorkflowSchemaError(
                "Gemini returned invalid JSON for the workflow."
            ) from None
        try:
            workflow = WorkflowDefinition.model_validate(payload)
        except ValidationError as exc:
            errors_found = [
                {"location": list(error["loc"]), "type": error["type"]}
                for error in exc.errors(include_input=False)
            ]
            raise InvalidWorkflowSchemaError(
                "Gemini returned a workflow that does not match the required schema.",
                details={"validation_errors": errors_found},
            ) from None
        return self._validate_generated_workflow(workflow)

    def _validate_generated_workflow(
        self,
        workflow: WorkflowDefinition,
    ) -> WorkflowDefinition:
        if not workflow.tasks:
            raise InvalidWorkflowSchemaError(
                "A generated workflow must contain at least one task."
            )

        self.validator.validate(workflow)
        for task in workflow.tasks:
            if task.tool_name not in ALLOWED_PLANNING_TOOLS:
                raise InvalidWorkflowSchemaError(
                    "Generated workflow references an unsupported planning tool.",
                    details={"tool_name": task.tool_name},
                )
            retry_limit = min(
                self.limits.max_retries_per_task,
                self.config.max_retries_per_task,
            )
            if task.retries > retry_limit:
                raise ExecutionLimitExceededException(
                    f"Task '{task.id}' retries exceed the configured retry limit.",
                    details={
                        "task_id": task.id,
                        "retries": task.retries,
                        "limit": retry_limit,
                    },
                )
            if task.timeout_seconds is not None and task.timeout_seconds > min(
                self.limits.default_task_timeout_seconds,
                self.limits.max_run_time_seconds,
            ):
                raise ExecutionLimitExceededException(
                    f"Task '{task.id}' timeout exceeds the configured task timeout limit.",
                    details={
                        "task_id": task.id,
                        "timeout": task.timeout_seconds,
                        "limit": min(
                            self.limits.default_task_timeout_seconds,
                            self.limits.max_run_time_seconds,
                        ),
                    },
                )
            self._reject_executable_content(task)

        return workflow.model_copy(
            update={
                "tasks": [
                    task.model_copy(update={"requires_approval": True})
                    for task in workflow.tasks
                ]
            }
        )

    @staticmethod
    def _reject_executable_content(task: TaskDefinition) -> None:
        executable_nodes = (
            ast.Call,
            ast.Assign,
            ast.AnnAssign,
            ast.AugAssign,
            ast.Import,
            ast.ImportFrom,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.With,
            ast.For,
            ast.While,
            ast.Try,
            ast.Lambda,
        )

        def contains_code(value: str) -> bool:
            if _EXECUTABLE_TEXT.search(value):
                return True
            try:
                parsed = ast.parse(value)
            except SyntaxError:
                return False
            return any(isinstance(node, executable_nodes) for node in ast.walk(parsed))

        def inspect_value(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = str(key).casefold().replace("-", "_")
                    if normalized_key in _FORBIDDEN_INPUT_KEYS or any(
                        normalized_key.endswith(f"_{field}")
                        for field in _FORBIDDEN_INPUT_KEYS
                    ):
                        raise InvalidWorkflowSchemaError(
                            "Generated workflow inputs must not contain executable code or commands."
                        )
                    inspect_value(child)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    inspect_value(item)
            elif isinstance(value, str) and contains_code(value):
                raise InvalidWorkflowSchemaError(
                    "Generated workflow inputs must not contain executable code or commands."
                )

        if contains_code(task.name) or (
            task.description and contains_code(task.description)
        ):
            raise InvalidWorkflowSchemaError(
                "Generated workflow descriptions must not contain executable code or command instructions."
            )
        inspect_value(task.input_parameters)

    def _handle_gemini_failure(
        self,
        prompt: str,
        use_fallback: bool | None,
        reason: str,
        message: str,
    ) -> PlanningResult:
        if use_fallback is False:
            raise WorkflowPlanningError(message) from None

        self.logger.warning(
            "Gemini workflow planning failed; using offline fallback.",
            extra={"data": {"reason": reason}},
        )
        return self._plan_offline(prompt, reason=reason)

    def _plan_offline(self, prompt: str, reason: str) -> PlanningResult:
        workflow = generate_fallback_workflow(prompt)
        validated = self._validate_generated_workflow(workflow)
        return PlanningResult(
            provider="fallback",
            workflow=validated,
            metadata=PlanningMetadata(fallback_reason=reason),
        )


_default_planner = WorkflowPlanner()


async def plan_workflow(
    prompt: str,
    use_fallback: bool | None = None,
) -> PlanningResult:
    """Return a structured planning result using the configured provider."""
    return await _default_planner.plan(prompt, use_fallback=use_fallback)


async def generate_workflow(
    prompt: str,
    use_fallback: bool | None = None,
) -> WorkflowDefinition:
    """Return a validated workflow using Gemini or the offline fallback."""
    return await _default_planner.generate_workflow(
        prompt,
        use_fallback=use_fallback,
    )
