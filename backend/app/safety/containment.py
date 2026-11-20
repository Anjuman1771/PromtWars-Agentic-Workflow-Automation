"""FlowPilot AI Workspace Path Containment & Traversal Defense.

Enforces strict file boundary containment:
- Resolves all paths against the configured workspace root
- Rejects path traversal (../, ..\\) and multi-layer URL-encoded traversal (%2e%2e)
- Rejects absolute paths outside the workspace root
- Prevents symlink escapes by resolving physical link targets
- Protects internal .git metadata
- Handles both Windows and POSIX path semantics
"""

from pathlib import Path
from typing import Optional, Union
import urllib.parse

from ..config import settings
from .exceptions import PathTraversalViolation


def sanitize_raw_input(raw_path: str) -> str:
    """Decode multi-layer URL encodings and check for null-byte injections."""
    cleaned = raw_path.strip()
    if not cleaned:
        raise PathTraversalViolation("Path cannot be empty.")

    # Null-byte injection check
    if "\x00" in cleaned:
        raise PathTraversalViolation("Null byte injection detected in path.")

    # Multi-pass URL decoding (handles double-encoded %252e%252e)
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = urllib.parse.unquote(cleaned)

    # Check for direct suspicious backslash-forwardslash traversal attempts
    normalized_slashes = cleaned.replace("\\", "/")
    parts = normalized_slashes.split("/")
    if ".." in parts:
        # We allow resolve to process if within bounds, but flag obvious escape attempts
        pass

    return cleaned


def validate_workspace_path(
    user_path: Union[str, Path],
    workspace_root: Optional[Union[str, Path]] = None,
) -> Path:
    """Validate and resolve a path ensuring it strictly resides within the workspace boundary.

    Args:
        user_path: The requested path (relative or absolute).
        workspace_root: Optional custom workspace root (defaults to settings.workspace_root).

    Returns:
        The verified, fully resolved absolute Path.

    Raises:
        PathTraversalViolation: If the path escapes the workspace root or accesses .git.
    """
    raw_str = sanitize_raw_input(str(user_path))

    # Resolve workspace root to its physical canonical path
    root_value = settings.workspace_root if workspace_root is None else workspace_root
    if not str(root_value).strip():
        raise PathTraversalViolation("Workspace root cannot be empty.")
    root = Path(root_value).resolve()

    candidate = Path(raw_str)

    # Resolve candidate relative to workspace root if relative, or resolve absolute directly
    if candidate.is_absolute():
        resolved_path = candidate.resolve()
    else:
        resolved_path = (root / candidate).resolve()

    # Robust containment verification (not just string prefix)
    try:
        is_contained = resolved_path.is_relative_to(root)
    except (ValueError, TypeError):
        # Different drive letters on Windows (e.g. C: vs D:) will raise ValueError
        is_contained = False

    if not is_contained:
        raise PathTraversalViolation(
            f"Path traversal detected: '{user_path}' escapes workspace root '{root}'.",
            details={"requested_path": str(user_path), "resolved_path": str(resolved_path), "workspace_root": str(root)},
        )

    # Block access to .git metadata directory
    if ".git" in resolved_path.parts:
        raise PathTraversalViolation(
            "Access to .git repository internal directory is forbidden.",
            details={"path": str(resolved_path)},
        )

    return resolved_path
