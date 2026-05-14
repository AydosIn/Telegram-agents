from __future__ import annotations

import os
from pathlib import Path


class PathValidationError(ValueError):
    """Raised when a workspace-relative path is unsafe or out of bounds."""


def normalize_workspace_relative(path_raw: str) -> str:
    """Return a POSIX-style relative path with no traversal segments."""
    cleaned = (path_raw or "").strip().replace("\\", "/")
    if not cleaned or cleaned == ".":
        return "."
    parts: list[str] = []
    for part in cleaned.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise PathValidationError("Path segments '..' are not allowed.")
        if os.path.isabs(part):
            raise PathValidationError("Absolute paths are not allowed.")
        parts.append(part)
    return "/".join(parts) if parts else "."


def resolve_under_workspace(workspace_root: Path, relative_posix: str) -> Path:
    """Resolve a normalized relative path strictly inside workspace_root."""
    root = workspace_root.resolve()
    target = (root / relative_posix.replace("/", os.sep)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PathValidationError("Path escapes the workspace directory.") from exc
    return target


def is_allowed_for_agent(
    *,
    workspace_root: Path,
    relative_posix: str,
    allowed_directory_prefixes: tuple[str, ...],
) -> bool:
    """
    If allowed_directory_prefixes is empty, any path under workspace_root is allowed.
    Otherwise relative_posix must match one of the prefixes (or equal the prefix).
    Prefixes use POSIX segments relative to workspace root (e.g. \"events-community-frontend\").
    """
    if not allowed_directory_prefixes:
        return True
    rel = relative_posix
    if rel == ".":
        return False
    for prefix in allowed_directory_prefixes:
        norm_prefix = prefix.replace("\\", "/").strip("/")
        if not norm_prefix:
            return True
        if rel == norm_prefix:
            return True
        if rel.startswith(norm_prefix + "/"):
            return True
    return False
