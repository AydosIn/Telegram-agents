from __future__ import annotations


class TaskStatus:
    """Primary workflow states for tasks (Phase 3)."""

    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"


# Legacy statuses still accepted from old rows until migration runs.
LEGACY_STATUS_MAP: dict[str, str] = {
    "queued": TaskStatus.TODO,
    "running": TaskStatus.IN_PROGRESS,
    "committed": TaskStatus.REVIEW,
    "done": TaskStatus.DONE,
    "pushed": TaskStatus.DONE,
    "failed": TaskStatus.FAILED,
    "blocked": TaskStatus.FAILED,
}


def normalize_task_status(raw: str) -> str:
    """Map legacy DB values to the Phase 3 enum for display and new writes."""
    return LEGACY_STATUS_MAP.get(raw, raw)


def is_terminal_status(status: str) -> bool:
    return status in (TaskStatus.DONE, TaskStatus.FAILED)


def should_stamp_completed_at(status: str) -> bool:
    """Whether transitioning to this status should set completed_at if unset."""
    return status in (TaskStatus.DONE, TaskStatus.FAILED)
