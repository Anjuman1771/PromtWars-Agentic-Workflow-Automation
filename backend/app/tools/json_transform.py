"""Bounded JSON parsing and transformations with a restricted path grammar."""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from ..config import settings
from ..safety.exceptions import ExecutionLimitExceededException, ToolExecutionError

_PATH_PATTERN = re.compile(
    r"^\$(?:\.[A-Za-z_][A-Za-z0-9_-]*|\[(?:0|[1-9][0-9]*)\])*$"
)
_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")


class JsonTransformInput(BaseModel):
    """Input schema for parse, select, and filter JSON operations.

    Paths use `$`, `.object_key`, and `[array_index]`; quoted keys, wildcards,
    expressions, and recursive descent are intentionally unsupported.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "parse", "select", "filter", "summarize", "extract_key_points"
    ]
    data: Any
    path: StrictStr | None = Field(
        default=None,
        max_length=1024,
        pattern=_PATH_PATTERN.pattern,
    )
    field: StrictStr | None = Field(
        default=None,
        max_length=1024,
        pattern=_FIELD_PATTERN.pattern,
    )
    operator: Literal["equals", "not_equals", "contains"] | None = None
    value: Any = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "JsonTransformInput":
        if self.operation == "parse":
            if not isinstance(self.data, str):
                raise ValueError("parse requires a JSON string in data")
            if {"path", "field", "operator", "value"} & self.model_fields_set:
                raise ValueError("parse does not accept path or filter fields")
        elif self.operation == "select":
            if self.path is None:
                raise ValueError("select requires path")
            if {"field", "operator", "value"} & self.model_fields_set:
                raise ValueError("select does not accept filter fields")
        elif self.operation in {"summarize", "extract_key_points"}:
            if not isinstance(self.data, str) or not self.data.strip():
                raise ValueError(f"{self.operation} requires non-empty text in data")
            if {"path", "field", "operator", "value"} & self.model_fields_set:
                raise ValueError(f"{self.operation} does not accept path or filter fields")
        elif self.operation == "filter":
            if self.field is None or self.operator is None:
                raise ValueError("filter requires field and operator")
            if "value" not in self.model_fields_set:
                raise ValueError("filter requires value")
            if self.path is not None and not _PATH_PATTERN.fullmatch(self.path):
                raise ValueError("filter path is invalid")
        return self


class JsonTransformer:
    """Execute supported, non-executable JSON transformations."""

    def __init__(
        self,
        max_input_size_bytes: int | None = None,
        max_output_size_bytes: int | None = None,
    ) -> None:
        self.max_input_size_bytes = max_input_size_bytes or settings.max_payload_size_bytes
        self.max_output_size_bytes = (
            max_output_size_bytes or settings.max_response_size_bytes
        )

    def transform(self, request: JsonTransformInput) -> dict[str, Any]:
        """Run one explicitly supported JSON operation."""
        self._check_size(request.data, self.max_input_size_bytes, "input")
        data = request.data
        if request.operation == "parse":
            try:
                result = json.loads(
                    data,
                    parse_constant=self._reject_nonstandard_constant,
                )
            except (json.JSONDecodeError, RecursionError, ValueError):
                raise ToolExecutionError("The supplied text is not valid JSON.") from None
        elif request.operation == "select":
            result = self._select(data, request.path or "$")
        elif request.operation == "filter":
            target = self._select(data, request.path or "$")
            if not isinstance(target, list):
                raise ToolExecutionError("The filter target must be a JSON array.")
            filtered = []
            for item in target:
                try:
                    actual = self._select_field(item, request.field or "")
                except KeyError:
                    continue
                if self._matches(actual, request.operator or "equals", request.value):
                    filtered.append(item)
            result = filtered
        elif request.operation == "summarize":
            result = " ".join(self._sentences(data)[:3])
        else:
            result = self._sentences(data)[:5]

        response = {"result": result}
        if request.operation == "summarize":
            response["summary"] = result
        elif request.operation == "extract_key_points":
            response["key_points"] = result
        self._check_size(response, self.max_output_size_bytes, "output")
        return response

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|\r?\n+", text)
            if sentence.strip()
        ]

    @staticmethod
    def _select(data: Any, path: str) -> Any:
        if not _PATH_PATTERN.fullmatch(path):
            raise ToolExecutionError("The JSON path is invalid.")
        if path == "$":
            return data
        for token in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[([0-9]+)\]", path[1:]):
            key, index = token.groups()
            if key is not None:
                if not isinstance(data, dict) or key not in data:
                    raise ToolExecutionError("The JSON path does not exist.")
                data = data[key]
            else:
                if not isinstance(data, list) or int(index or "0") >= len(data):
                    raise ToolExecutionError("The JSON path does not exist.")
                data = data[int(index or "0")]
        return data

    @staticmethod
    def _select_field(item: Any, field: str) -> Any:
        value = item
        for key in field.split("."):
            if not isinstance(value, dict) or key not in value:
                raise KeyError(key)
            value = value[key]
        return value

    @staticmethod
    def _matches(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "equals":
            return actual == expected
        if operator == "not_equals":
            return actual != expected
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return expected in actual
        return False

    @staticmethod
    def _check_size(value: Any, limit: int, label: str) -> None:
        try:
            serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise ToolExecutionError(f"The JSON {label} is invalid.") from None
        if len(serialized.encode("utf-8")) > limit:
            raise ExecutionLimitExceededException(
                f"The JSON {label} exceeds the configured size limit."
            )

    @staticmethod
    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"Non-standard JSON constant: {value}")
