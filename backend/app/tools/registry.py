"""Explicit tool registration, schema validation, and authorization boundary."""

import re
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings, settings
from ..core.models import WorkflowDefinition
from ..safety.exceptions import ToolNotFoundError, ToolValidationError
from .file_ops import (
    FileOperations,
    ListFilesInput,
    ReadFileInput,
    WriteFileInput,
)
from .http_request import HttpRequestInput, HttpRequestTool
from .json_transform import JsonTransformInput, JsonTransformer
from .search import SearchInput, SearchTool, LocalSearchProvider

InputT = TypeVar("InputT", bound=BaseModel)
ToolHandler = Callable[[InputT], Any]
_TOOL_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class ToolRegistration(Generic[InputT]):
    """One tool's explicit identifier, schema, and implementation."""

    input_model: type[InputT]
    handler: ToolHandler[InputT]


class ToolRegistry:
    """Expose and invoke only explicitly registered tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolRegistration[Any]] = {}

    def register(
        self,
        identifier: str,
        input_model: type[InputT],
        handler: ToolHandler[InputT],
    ) -> None:
        """Register a unique, explicitly named schema and handler."""
        if not _TOOL_IDENTIFIER.fullmatch(identifier):
            raise ValueError("Tool identifiers must use the documented identifier format.")
        if identifier in self._tools:
            raise ValueError(f"Tool identifier '{identifier}' is already registered.")
        self._tools[identifier] = ToolRegistration(
            input_model=input_model,
            handler=handler,
        )

    def is_registered(self, identifier: str) -> bool:
        """Return whether an identifier is authorized by this registry."""
        return identifier in self._tools

    def identifiers(self) -> tuple[str, ...]:
        """Return registered identifiers in deterministic order."""
        return tuple(sorted(self._tools))

    def schemas(self) -> dict[str, dict[str, Any]]:
        """Expose JSON schemas only for authorized tools."""
        return {
            identifier: registration.input_model.model_json_schema()
            for identifier, registration in sorted(self._tools.items())
        }

    def validate_workflow(self, workflow: WorkflowDefinition) -> None:
        """Check planned task identifiers without executing any task."""
        for task in workflow.tasks:
            if not self.is_registered(task.tool_name):
                raise ToolNotFoundError(
                    "Workflow references a tool that is not authorized.",
                    details={"tool_name": task.tool_name},
                )

    def execute(self, identifier: str, inputs: dict[str, Any]) -> Any:
        """Validate inputs against the registered schema before invoking a tool."""
        registration = self._tools.get(identifier)
        if registration is None:
            raise ToolNotFoundError(
                "The requested tool is not authorized.",
                details={"tool_name": identifier},
            )
        try:
            validated = registration.input_model.model_validate(inputs)
        except ValidationError as exc:
            errors_found = [
                {"location": list(error["loc"]), "type": error["type"]}
                for error in exc.errors(include_input=False)
            ]
            raise ToolValidationError(
                "Tool inputs do not match the registered schema.",
                details={"validation_errors": errors_found},
            ) from None
        return registration.handler(validated)


def create_default_tool_registry(
    config: Settings | None = None,
) -> ToolRegistry:
    """Create the fixed safe tool set; no shell or arbitrary-code tool is registered."""
    app_config = config or settings
    files = FileOperations(
        workspace_root=app_config.workspace_root,
        max_file_size_bytes=app_config.max_file_size_bytes,
        max_response_size_bytes=app_config.max_response_size_bytes,
    )
    transformer = JsonTransformer(
        max_input_size_bytes=app_config.max_payload_size_bytes,
        max_output_size_bytes=app_config.max_response_size_bytes,
    )
    http_tool = HttpRequestTool(
        max_response_size_bytes=app_config.max_response_size_bytes,
        max_payload_size_bytes=app_config.max_payload_size_bytes,
        connect_timeout_seconds=app_config.tool_http_connect_timeout_seconds,
        read_timeout_seconds=app_config.tool_http_read_timeout_seconds,
        max_redirects=app_config.tool_http_max_redirects,
    )
    search_tool = SearchTool(
        provider=LocalSearchProvider(files),
        max_response_size_bytes=app_config.max_response_size_bytes,
    )
    registry = ToolRegistry()
    registry.register("file.read", ReadFileInput, files.read)
    registry.register("file.write", WriteFileInput, files.write)
    registry.register("file.list", ListFilesInput, files.list_files)
    registry.register("http.request", HttpRequestInput, http_tool.request)
    registry.register("transform", JsonTransformInput, transformer.transform)
    registry.register("search.query", SearchInput, search_tool.search)
    return registry


default_tool_registry = create_default_tool_registry()
