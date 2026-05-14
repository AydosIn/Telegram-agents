from __future__ import annotations

import re

from app.models import TaskRecord


def truncate(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...[truncated]"


def task_title(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:36].strip("-") or "task"


def format_task(task: TaskRecord) -> str:
    parts = [
        f"Task #{task.id} [{task.agent}]",
        f"Status: {task.status}",
        f"Request: {task.request}",
    ]
    if task.branch:
        parts.append(f"Branch: {task.branch}")
    if task.project_key:
        parts.append(f"Project: {task.project_key}")
    if task.commit_hash:
        parts.append(f"Commit: {task.commit_hash}")
    if task.pushed_at:
        parts.append(f"Pushed: {task.pushed_at}")
    if task.completed_at:
        parts.append(f"Completed: {task.completed_at}")
    if task.files_changed:
        parts.append(f"Files changed: {truncate(task.files_changed, 500)}")
    if task.summary:
        parts.append(f"Summary: {truncate(task.summary, 900)}")
    if task.verification:
        parts.append(f"Verification: {truncate(task.verification, 900)}")
    return "\n".join(parts)
