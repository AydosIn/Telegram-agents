from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaseAgent:
    """
    Logical agent profile for tool permissioning.

    allowed_directories: path prefixes relative to the workspace root using forward slashes.
    An empty tuple means the agent may access any path that stays inside the workspace
    root (full sandbox). Non-empty tuples restrict paths to those subtrees (e.g. one
    repo per agent, or two repos for QA).
    """

    name: str
    role: str
    allowed_directories: tuple[str, ...]
    tools: tuple[str, ...]
    terminal_allowed_executables: frozenset[str] = frozenset()
