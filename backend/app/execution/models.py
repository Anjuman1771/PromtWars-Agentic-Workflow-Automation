"""Public execution states and result types."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ApprovalRequest:
    """One-time approval material returned only to the caller, never persisted raw."""

    approval_id: str
    run_id: str
    task_id: str
    tool_name: str
    expires_at: str
    raw_token: str = field(repr=False)
    action_payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ExecutionResult:
    """Sanitized snapshot of a persisted workflow run."""

    run_id: str
    state: RunState
    task_states: dict[str, TaskState]
    outputs: dict[str, Any]
    errors: dict[str, str]
    pending_approvals: tuple[ApprovalRequest, ...] = ()
    reconciliation_required: dict[str, bool] = field(default_factory=dict)
