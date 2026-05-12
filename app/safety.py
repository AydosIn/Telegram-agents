from __future__ import annotations

from pathlib import Path

BLOCKED_DIR_NAMES = {".venv", "node_modules", ".next", "__pycache__"}
BLOCKED_FILE_SUFFIXES = {".pyc", ".sqlite", ".sqlite3", ".db"}


def is_unsafe_relative_path(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    file_name = parts[-1] if parts else normalized
    suffix = Path(file_name).suffix
    if any(part in BLOCKED_DIR_NAMES for part in parts):
        return True
    if file_name == ".env" or file_name.startswith(".env."):
        return True
    if suffix in BLOCKED_FILE_SUFFIXES:
        return True
    return False
