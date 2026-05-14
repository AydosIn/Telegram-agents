from __future__ import annotations

from pathlib import Path


def baseline_dir(workspace: Path, agent: str, task_id: int | None) -> Path:
    """Directory for future pixel/visual baselines (per agent + optional task)."""
    tid = task_id if task_id is not None else 0
    return (
        workspace
        / "browser-artifacts"
        / agent
        / f"task-{tid}"
        / "baselines"
    )


def capture_relative_path(agent: str, task_id: int | None, filename: str) -> str:
    """Stable relative POSIX path under workspace for captures (not necessarily baselines)."""
    tid = task_id if task_id is not None else 0
    safe = filename.replace("\\", "/").lstrip("/")
    if ".." in safe or safe.startswith("/"):
        raise ValueError("Invalid screenshot filename.")
    return f"browser-artifacts/{agent}/task-{tid}/{safe}"
