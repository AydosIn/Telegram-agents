from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from app.config import AgentConfig, Settings
from app.models import CommandResult, TaskRecord
from app.prompts import build_chat_prompt, build_implementation_prompt, build_qa_review_prompt


class CodexCLIProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run_chat(
        self,
        agent: AgentConfig,
        message: str,
    ) -> CommandResult:
        prompt = build_chat_prompt(agent, message)
        log_dir = self.settings.database_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = log_dir / f"chat-{agent.name}-last-message.txt"
        command = [
            "codex",
            "exec",
            "--cd",
            str(self.settings.database_path.parent),
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        model = self.settings.effective_codex_model_for_agent(agent)
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        result = await self._run_command(command, self.settings.database_path.parent, timeout=900)
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
            if last_message:
                return CommandResult(result.returncode, last_message, result.stderr)
        return result

    async def run_agent_work(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        *,
        recent_context: str | None = None,
    ) -> CommandResult:
        prompt = build_implementation_prompt(agent, task, recent_context=recent_context)
        log_dir = self.settings.database_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = log_dir / f"task-{task.id}-last-message.txt"
        command = [
            "codex",
            "exec",
            "--cd",
            str(agent.repo),
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        model = self.settings.effective_codex_model_for_agent(agent)
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        result = await self._run_command(command, agent.repo, timeout=1800)
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
            if last_message:
                stdout = result.stdout + "\n\nFinal message:\n" + last_message
                return CommandResult(result.returncode, stdout, result.stderr)
        return result

    async def run_qa_review(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        verification_bundle: str,
        *,
        review_cwd: Path,
    ) -> CommandResult:
        prompt = build_qa_review_prompt(agent, task, verification_bundle)
        log_dir = self.settings.database_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = log_dir / f"task-{task.id}-qa-review-last-message.txt"
        command = [
            "codex",
            "exec",
            "--cd",
            str(review_cwd),
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        model = self.settings.effective_codex_model_for_agent(agent)
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        result = await self._run_command(command, review_cwd, timeout=900)
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
            if last_message:
                stdout = result.stdout + "\n\nQA review (final message):\n" + last_message
                return CommandResult(result.returncode, stdout, result.stderr)
        return result

    async def _run_command(
        self,
        command: list[str],
        cwd: Path | None,
        timeout: int = 600,
    ) -> CommandResult:
        def as_text(value: str | bytes | None) -> str:
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value

        def call() -> CommandResult:
            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                shell=False,
            )
            return CommandResult(completed.returncode, completed.stdout, completed.stderr)

        try:
            return await asyncio.to_thread(call)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                124,
                as_text(exc.stdout),
                as_text(exc.stderr) + f"\nCommand timed out after {timeout} seconds.",
            )
        except FileNotFoundError as exc:
            return CommandResult(127, "", str(exc))
