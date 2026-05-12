from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.config import AgentConfig
from app.models import CommandResult, TaskRecord


class AIProvider(Protocol):
    async def run_chat(
        self,
        agent: AgentConfig,
        message: str,
    ) -> CommandResult:
        """Return a short plain-text conversational response. Must not modify repositories."""

    async def run_agent_work(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        *,
        recent_context: str | None = None,
    ) -> CommandResult:
        """Run the AI implementation step for a task. Must not commit or push."""

    async def run_qa_review(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        verification_bundle: str,
        *,
        review_cwd: Path,
    ) -> CommandResult:
        """Read-only narrative QA pass; must not modify repositories."""
