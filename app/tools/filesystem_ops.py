from __future__ import annotations

import os
from pathlib import Path

from app.tools.paths import PathValidationError


def read_file_sync(abs_path: Path) -> str:
    if not abs_path.is_file():
        raise FileNotFoundError(f"Not a file: {abs_path}")
    return abs_path.read_text(encoding="utf-8", errors="replace")


def write_file_sync(abs_path: Path, content: str) -> None:
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8", newline="")


def edit_file_sync(abs_path: Path, old_text: str, new_text: str) -> str:
    if not old_text:
        raise ValueError("old_text must be non-empty for edit_file.")
    if not abs_path.is_file():
        raise FileNotFoundError(f"Not a file: {abs_path}")
    body = abs_path.read_text(encoding="utf-8", errors="replace")
    count = body.count(old_text)
    if count == 0:
        raise ValueError("old_text was not found in the file.")
    if count > 1:
        raise ValueError(
            f"old_text matched {count} times; include more context so the edit is unambiguous.",
        )
    updated = body.replace(old_text, new_text, 1)
    abs_path.write_text(updated, encoding="utf-8", newline="")
    return updated


def list_files_sync(abs_path: Path, *, max_entries: int = 500) -> list[str]:
    if not abs_path.exists():
        raise FileNotFoundError(f"Path does not exist: {abs_path}")
    if not abs_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {abs_path}")
    names = sorted(os.listdir(abs_path))
    if len(names) > max_entries:
        raise ValueError(
            f"Directory lists more than {max_entries} entries; narrow the path.",
        )
    lines: list[str] = []
    for name in names:
        child = abs_path / name
        suffix = "/" if child.is_dir() else ""
        lines.append(f"{name}{suffix}")
    return lines
