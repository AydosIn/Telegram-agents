from __future__ import annotations

from app.formatting import truncate
from app.store import Store


def build_recent_context_for_agent(
    store: Store,
    *,
    agent: str,
    exclude_task_id: int,
    max_tasks: int,
    max_chars: int,
) -> str | None:
    """Return a bounded text block of prior tasks and handoffs, or None if disabled or empty."""
    if max_tasks <= 0 or max_chars <= 0:
        return None

    chunks: list[str] = []
    tasks = store.list_tasks_for_agent(agent, limit=max_tasks + 8)
    shown = 0
    for t in tasks:
        if t.id == exclude_task_id:
            continue
        if shown >= max_tasks:
            break
        line = f"- Task #{t.id} [{t.status}]: {truncate(t.request, 280)}"
        if t.summary:
            line += f" | summary: {truncate(t.summary, 200)}"
        chunks.append(line)
        shown += 1

    handoff_lines = store.recent_handoff_lines_for_agent(agent, limit=5)
    if handoff_lines:
        chunks.append("Recent handoffs:")
        chunks.extend(handoff_lines)

    if not chunks:
        return None

    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[: max_chars - 24].rstrip() + "\n...[context truncated]"
    return text
