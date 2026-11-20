"""Typed workflow definitions and planning data models."""

from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

TaskId = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ToolName = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
OutputName = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")]
RetryCount = Annotated[StrictInt, Field(ge=0)]
TimeoutSeconds = Annotated[StrictInt, Field(ge=1)]


class TaskDependency(BaseModel):
    """Reference to another task that must complete first."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId


class TaskDefinition(BaseModel):
    """A single workflow graph node and its declarative tool inputs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: TaskId
    name: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    description: StrictStr | None = None
    tool_name: ToolName = Field(
        validation_alias=AliasChoices("tool_name", "tool"),
        serialization_alias="tool",
    )
    input_parameters: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("input_parameters", "input_params", "inputs"),
    )
    dependencies: list[TaskDependency] = Field(
        default_factory=list,
        validation_alias=AliasChoices("dependencies", "depends_on"),
    )
    outputs: list[OutputName] = Field(
        default_factory=list,
        description="Declared top-level output names available for interpolation.",
    )
    requires_approval: StrictBool = False
    retries: RetryCount = 0
    timeout_seconds: TimeoutSeconds | None = None

    @field_validator("dependencies", mode="before")
    @classmethod
    def normalize_dependencies(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [
                {"task_id": dependency} if isinstance(dependency, str) else dependency
                for dependency in value
            ]
        return value


class WorkflowDefinition(BaseModel):
    """A directed acyclic graph of declared workflow tasks."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: StrictStr | None = None
    name: Annotated[StrictStr, Field(min_length=1, max_length=200)] | None = None
    description: StrictStr | None = None
    tasks: list[TaskDefinition] = Field(default_factory=list)


class TaskExecutionContext(BaseModel):
    """Declared workflow metadata and completed outputs available to interpolation."""

    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowDefinition
    task_id: TaskId
    task_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_context_task(self) -> "TaskExecutionContext":
        if self.task_id not in {task.id for task in self.workflow.tasks}:
            raise ValueError(f"Task '{self.task_id}' is not part of the workflow.")
        return self


class WorkflowValidationResult(BaseModel):
    """Successful structural validation and deterministic graph ordering."""

    model_config = ConfigDict(extra="forbid")

    valid: StrictBool = True
    task_count: StrictInt
    topological_order: list[TaskId]


class ExecutionPlan(BaseModel):
    """Deterministic task order and parallel-ready batches; performs no execution."""

    model_config = ConfigDict(extra="forbid")

    topological_order: list[TaskId]
    initial_ready_tasks: list[TaskId]
    parallel_groups: list[list[TaskId]]
    max_concurrent_tasks: StrictInt
