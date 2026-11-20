"""Read-only, deterministic local workspace search."""

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ..config import settings
from ..safety.exceptions import ExecutionLimitExceededException, ToolExecutionError
from .file_ops import FileOperations, ReadFileInput

_MAX_QUERY_CHARS = 256
_MAX_SEARCH_FILES = 500
_MAX_MATCH_CHARS = 500


class SearchInput(BaseModel):
    """Input schema for literal local text search."""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=_MAX_QUERY_CHARS)
    max_results: int = Field(default=20, ge=1, le=100)


class SearchProvider(Protocol):
    """Read-only provider interface; this phase ships only local search."""

    def search(self, query: str, max_results: int) -> list[dict[str, Any]]: ...


class LocalSearchProvider:
    """Search bounded UTF-8 files in the configured workspace without network access."""

    def __init__(self, files: FileOperations | None = None) -> None:
        self.files = files or FileOperations()

    def search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        needle = query.casefold()
        for path in self.files.iter_file_paths(_MAX_SEARCH_FILES):
            try:
                data = self.files.read(ReadFileInput(path=str(path)))["content"]
            except ExecutionLimitExceededException:
                continue
            except ToolExecutionError:
                raise ToolExecutionError(
                    "A workspace file could not be searched safely."
                ) from None
            for line_number, line in enumerate(str(data).splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                matches.append(
                    {
                        "path": path.relative_to(self.files.workspace_root).as_posix(),
                        "line": line_number,
                        "text": line[:_MAX_MATCH_CHARS],
                    }
                )
                if len(matches) >= max_results:
                    return matches
        return matches


class SearchTool:
    """Enforce query, provider-result, and serialized output bounds."""

    def __init__(
        self,
        provider: SearchProvider | None = None,
        max_results: int | None = None,
        max_response_size_bytes: int | None = None,
    ) -> None:
        self.provider = provider or LocalSearchProvider()
        self.max_results = 50 if max_results is None else max_results
        self.max_response_size_bytes = (
            settings.max_response_size_bytes
            if max_response_size_bytes is None
            else max_response_size_bytes
        )

    def search(self, request: SearchInput) -> dict[str, Any]:
        if request.max_results > self.max_results:
            raise ExecutionLimitExceededException(
                "The search result count exceeds the configured limit."
            )
        results = self.provider.search(request.query, request.max_results)
        response = {"results": results}
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > self.max_response_size_bytes:
            raise ExecutionLimitExceededException(
                "The search output exceeds the configured response-size limit."
            )
        return response
