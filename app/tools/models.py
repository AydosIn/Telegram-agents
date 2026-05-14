from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolResult:
    """Structured outcome returned to the model after a tool runs."""

    ok: bool
    tool: str
    message: str
    attachment_paths: tuple[str, ...] = ()
