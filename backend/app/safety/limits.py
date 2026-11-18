"""FlowPilot AI Execution Safety Limits Engine.

Enforces centralized resource constraints:
- Maximum workflow DAG steps
- Maximum workflow runtime duration
- Maximum task timeout
- Maximum concurrent task execution
- Maximum retry attempts
- Maximum payload and tool response sizes

All limits are loaded from configuration without scattered hardcoded values.
"""

import json
import time
from typing import Any, Dict, List, Optional, Union

from ..config import settings
from .exceptions import ExecutionLimitExceededException, TaskTimeoutException


class ExecutionLimits:
    """Validator and tracker for workflow execution limits."""

    def __init__(
        self,
        max_workflow_steps: Optional[int] = None,
        max_run_time_seconds: Optional[int] = None,
        default_task_timeout_seconds: Optional[int] = None,
        max_concurrent_tasks: Optional[int] = None,
        max_retries_per_task: Optional[int] = None,
        max_response_size_bytes: Optional[int] = None,
        max_payload_size_bytes: Optional[int] = None,
    ):
        self.max_workflow_steps = max_workflow_steps or settings.max_workflow_steps
        self.max_run_time_seconds = max_run_time_seconds or settings.max_run_time_seconds
        self.default_task_timeout_seconds = (
            default_task_timeout_seconds or settings.default_task_timeout_seconds
        )
        self.max_concurrent_tasks = max_concurrent_tasks or settings.max_concurrent_tasks
        self.max_retries_per_task = max_retries_per_task or settings.max_retries_per_task
        self.max_response_size_bytes = (
            max_response_size_bytes or settings.max_response_size_bytes
        )
        self.max_payload_size_bytes = (
            max_payload_size_bytes or settings.max_payload_size_bytes
        )

    def validate_payload_size(self, data: Union[str, bytes, Dict[str, Any], List[Any]]) -> int:
        """Validate payload size against max_payload_size_bytes."""
        if isinstance(data, bytes):
            size = len(data)
        elif isinstance(data, str):
            size = len(data.encode("utf-8"))
        else:
            try:
                serialized = json.dumps(data)
                size = len(serialized.encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise ExecutionLimitExceededException(
                    f"Payload could not be serialized to JSON: {exc}"
                ) from exc

        if size > self.max_payload_size_bytes:
            raise ExecutionLimitExceededException(
                f"Payload size ({size} bytes) exceeds maximum allowed limit ({self.max_payload_size_bytes} bytes).",
                details={"size": size, "limit": self.max_payload_size_bytes},
            )
        return size

    def validate_response_size(self, size_bytes: int) -> None:
        """Validate received response byte count against max_response_size_bytes."""
        if size_bytes > self.max_response_size_bytes:
            raise ExecutionLimitExceededException(
                f"Response size ({size_bytes} bytes) exceeds configured maximum limit ({self.max_response_size_bytes} bytes).",
                details={"size": size_bytes, "limit": self.max_response_size_bytes},
            )

    def validate_workflow_definition(self, definition: Dict[str, Any]) -> None:
        """Reject workflows that exceed configured safety bounds before execution starts."""
        # 1. Payload size check
        self.validate_payload_size(definition)

        # 2. Task step count check
        tasks = definition.get("tasks", [])
        if not isinstance(tasks, list):
            raise ExecutionLimitExceededException("Workflow definition must contain a 'tasks' list.")

        step_count = len(tasks)
        if step_count > self.max_workflow_steps:
            raise ExecutionLimitExceededException(
                f"Workflow task count ({step_count}) exceeds configured maximum steps limit ({self.max_workflow_steps}).",
                details={"step_count": step_count, "max_workflow_steps": self.max_workflow_steps},
            )

        # 3. Individual task checks (retries, timeouts)
        for task in tasks:
            task_id = task.get("id", "unknown")
            retries = task.get("retries", 0)
            if retries > self.max_retries_per_task:
                raise ExecutionLimitExceededException(
                    f"Task '{task_id}' specifies {retries} retries, exceeding maximum allowed ({self.max_retries_per_task}).",
                    details={"task_id": task_id, "retries": retries, "limit": self.max_retries_per_task},
                )

            timeout = task.get("timeout_seconds", self.default_task_timeout_seconds)
            if timeout > self.max_run_time_seconds:
                raise ExecutionLimitExceededException(
                    f"Task '{task_id}' timeout ({timeout}s) cannot exceed total workflow max runtime ({self.max_run_time_seconds}s).",
                    details={"task_id": task_id, "timeout": timeout, "limit": self.max_run_time_seconds},
                )

    def check_execution_time(self, started_at_timestamp: float) -> float:
        """Ensure total workflow runtime has not exceeded max_run_time_seconds."""
        elapsed = time.time() - started_at_timestamp
        if elapsed > self.max_run_time_seconds:
            raise ExecutionLimitExceededException(
                f"Workflow execution exceeded maximum duration ({elapsed:.2f}s > {self.max_run_time_seconds}s).",
                details={"elapsed_seconds": elapsed, "max_duration": self.max_run_time_seconds},
            )
        return elapsed

    def check_task_timeout(self, started_at_timestamp: float, task_timeout_seconds: Optional[int] = None) -> float:
        """Ensure individual task execution has not exceeded its timeout limit."""
        allowed_timeout = task_timeout_seconds or self.default_task_timeout_seconds
        elapsed = time.time() - started_at_timestamp
        if elapsed > allowed_timeout:
            raise TaskTimeoutException(
                f"Task execution timed out after {elapsed:.2f}s (limit: {allowed_timeout}s).",
                details={"elapsed_seconds": elapsed, "limit": allowed_timeout},
            )
        return elapsed

    def check_step_limit(self, current_step_count: int) -> None:
        """Ensure current executed step count does not breach max_workflow_steps."""
        if current_step_count >= self.max_workflow_steps:
            raise ExecutionLimitExceededException(
                f"Execution reached maximum allowed steps limit ({self.max_workflow_steps}). Halting workflow.",
                details={"current_step_count": current_step_count, "limit": self.max_workflow_steps},
            )

    def check_retry_limit(self, current_retries: int) -> None:
        """Ensure task retry count does not exceed max_retries_per_task."""
        if current_retries >= self.max_retries_per_task:
            raise ExecutionLimitExceededException(
                f"Task retry limit reached ({current_retries} >= {self.max_retries_per_task}).",
                details={"current_retries": current_retries, "limit": self.max_retries_per_task},
            )


# Default global instance
execution_limits = ExecutionLimits()
