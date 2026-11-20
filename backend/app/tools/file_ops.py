"""Workspace-confined read, write, and list operations."""

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from ..config import settings
from ..safety.containment import validate_workspace_path
from ..safety.exceptions import (
    ExecutionLimitExceededException,
    PathTraversalViolation,
    ToolExecutionError,
)

_MAX_LIST_ENTRIES = 500


class ReadFileInput(BaseModel):
    """Input schema for reading one UTF-8 text file."""

    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(max_length=4096)


class WriteFileInput(BaseModel):
    """Input schema for writing one UTF-8 text file."""

    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(max_length=4096)
    content: StrictStr


class ListFilesInput(BaseModel):
    """Input schema for listing workspace files."""

    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(default=".", max_length=4096)
    recursive: StrictBool = False


class FileOperations:
    """Perform bounded UTF-8 file operations within one workspace."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        max_file_size_bytes: int | None = None,
        max_response_size_bytes: int | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or settings.workspace_root).resolve()
        self.max_file_size_bytes = max_file_size_bytes or settings.max_file_size_bytes
        self.max_response_size_bytes = (
            max_response_size_bytes or settings.max_response_size_bytes
        )

    def read(self, request: ReadFileInput) -> dict[str, Any]:
        """Read a bounded UTF-8 file without following workspace symlinks."""
        path = self._safe_path(request.path)
        try:
            with path.open("rb") as file:
                content = file.read(self.max_file_size_bytes + 1)
        except FileNotFoundError:
            raise ToolExecutionError("The requested file does not exist.") from None
        except PermissionError:
            raise ToolExecutionError("Permission was denied while reading the file.") from None
        except IsADirectoryError:
            raise ToolExecutionError("The requested path is not a file.") from None
        except OSError:
            raise ToolExecutionError("The file could not be read.") from None

        if len(content) > self.max_file_size_bytes:
            raise ExecutionLimitExceededException(
                "The requested file exceeds the configured file-size limit."
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolExecutionError("The requested file is not valid UTF-8 text.") from None
        self._validate_output_size({"path": self._relative(path), "content": text})
        return {"path": self._relative(path), "content": text}

    def write(self, request: WriteFileInput) -> dict[str, Any]:
        """Write a bounded UTF-8 file, replacing its previous contents."""
        path = self._safe_path(request.path)
        if len(request.content) > self.max_file_size_bytes:
            raise ExecutionLimitExceededException(
                "The requested content exceeds the configured file-size limit."
            )
        try:
            content = request.content.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolExecutionError("The requested content is not valid UTF-8.") from None
        if len(content) > self.max_file_size_bytes:
            raise ExecutionLimitExceededException(
                "The requested content exceeds the configured file-size limit."
            )
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
        except FileNotFoundError:
            raise ToolExecutionError("The parent directory does not exist.") from None
        except PermissionError:
            raise ToolExecutionError("Permission was denied while writing the file.") from None
        except IsADirectoryError:
            raise ToolExecutionError("The requested path is not a file.") from None
        except OSError:
            raise ToolExecutionError("The file could not be written.") from None
        return {"path": self._relative(path), "bytes_written": len(content)}

    def list_files(self, request: ListFilesInput) -> dict[str, Any]:
        """List files deterministically without traversing symlinks."""
        directory = self._safe_path(request.path)
        try:
            if not directory.is_dir():
                raise ToolExecutionError("The requested path is not a directory.")
            results: list[dict[str, Any]] = []
            pending = [directory]
            while pending:
                current = pending.pop()
                with os.scandir(current) as entries:
                    ordered = sorted(
                        entries,
                        key=lambda entry: (entry.name.casefold(), entry.name),
                    )
                child_directories: list[Path] = []
                for entry in ordered:
                    if entry.is_symlink():
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if request.recursive:
                            child_directories.append(entry_path)
                        continue
                    if entry.is_file(follow_symlinks=False):
                        results.append(
                            {
                                "path": self._relative(entry_path),
                                "size_bytes": entry.stat(follow_symlinks=False).st_size,
                            }
                        )
                        if len(results) > _MAX_LIST_ENTRIES:
                            raise ExecutionLimitExceededException(
                                "The file listing exceeds the configured result limit."
                            )
                pending.extend(reversed(child_directories))
        except PermissionError:
            raise ToolExecutionError("Permission was denied while listing files.") from None
        except OSError:
            raise ToolExecutionError("The directory could not be listed.") from None

        results.sort(key=lambda item: str(item["path"]).casefold())
        response = {"files": results}
        self._validate_output_size(response)
        return response

    def iter_file_paths(self, max_files: int) -> list[Path]:
        """Return at most max_files regular paths for the local search provider."""
        result = self.list_files(ListFilesInput(recursive=True))
        paths = [
            self.workspace_root / str(item["path"])
            for item in result["files"]
        ]
        if len(paths) > max_files:
            raise ExecutionLimitExceededException(
                "The workspace contains more files than the search scan limit."
            )
        return paths

    def _safe_path(self, raw_path: str) -> Path:
        path = validate_workspace_path(raw_path, self.workspace_root)
        current = self.workspace_root
        for component in path.relative_to(self.workspace_root).parts:
            current = current / component
            if current.is_symlink():
                raise PathTraversalViolation(
                    "File operations do not follow symbolic links."
                )
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace_root).as_posix()

    def _validate_output_size(self, value: Any) -> None:
        try:
            size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            raise ToolExecutionError("The file operation produced invalid output.") from None
        if size > self.max_response_size_bytes:
            raise ExecutionLimitExceededException(
                "The file operation output exceeds the configured response-size limit."
            )
