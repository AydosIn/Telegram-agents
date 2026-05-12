from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from app.config import AgentConfig, Settings
from app.formatting import task_title, truncate
from app.models import CommandResult, TaskRecord
from app.prompt_context import build_recent_context_for_agent
from app.providers import build_ai_provider_for
from app.providers.base import AIProvider
from app.safety import is_unsafe_relative_path
from app.store import Store


class AgentRunner:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._providers: dict[str, AIProvider] = {}

    def _normalized_provider_key(self, raw: str) -> str:
        name = raw.strip().lower()
        if name in ("codex", "codex_cli"):
            return "codex_cli"
        if name in ("gemini", "google_gemini"):
            return "gemini"
        if name == "openai":
            return "openai"
        return name

    def _provider_for(self, agent: AgentConfig) -> AIProvider:
        key = self._normalized_provider_key(agent.ai_provider or self.settings.ai_provider)
        if key not in self._providers:
            self._providers[key] = build_ai_provider_for(self.settings, key)
        return self._providers[key]

    def _qa_review_cwd(self) -> Path | None:
        fe = self.settings.agents["frontend"].repo
        be = self.settings.agents["backend"].repo
        if fe is not None:
            return fe
        if be is not None:
            return be
        return None

    async def run_chat(self, agent_name: str, message: str) -> CommandResult:
        agent = self.settings.agents[agent_name]
        return await self._provider_for(agent).run_chat(agent, message)

    async def run_task(self, task: TaskRecord) -> TaskRecord:
        agent = self.settings.agents[task.agent]
        if agent.name == "qa":
            return await self.run_qa_task(task)

        if agent.repo is None:
            return self.store.update_task(
                task.id,
                status="blocked",
                summary=f"{agent.name} does not have a configured repository.",
            )

        self.store.update_task(task.id, status="running")

        dirty = await self.git_status(agent.repo)
        if dirty:
            message = "Repo has uncommitted changes. Clean, commit, or stash them first.\n\n" + dirty
            self.store.add_log(task.id, "warn", message)
            return self.store.update_task(task.id, status="blocked", summary=message)

        branch = self.branch_name(agent.name, task.id, task.request)
        checkout = await self.run_command(["git", "checkout", "-b", branch], agent.repo)
        if not checkout.ok:
            return self.store.update_task(
                task.id,
                status="failed",
                branch=branch,
                summary=f"Could not create branch.\n{checkout.combined_output}",
            )

        self.store.update_task(task.id, branch=branch)
        self.store.add_log(task.id, "info", f"Created branch {branch}.")

        recent = build_recent_context_for_agent(
            self.store,
            agent=agent.name,
            exclude_task_id=task.id,
            max_tasks=self.settings.prompt_context_max_tasks,
            max_chars=self.settings.prompt_context_max_chars,
        )
        ai_result = await self._provider_for(agent).run_agent_work(
            agent,
            task,
            recent_context=recent,
        )
        if not ai_result.ok:
            self.store.add_log(task.id, "error", ai_result.combined_output)
            return self.store.update_task(
                task.id,
                status="failed",
                summary="AI run failed.\n" + truncate(ai_result.combined_output, 1800),
            )

        changed_files = await self.changed_files(agent.repo)
        unsafe_files = self.unsafe_files(changed_files)
        if unsafe_files:
            return self.store.update_task(
                task.id,
                status="failed",
                summary="AI step touched blocked paths. Review manually:\n" + "\n".join(unsafe_files),
            )

        if not changed_files:
            return self.store.update_task(
                task.id,
                status="done",
                summary="AI step completed without file changes.\n\n" + truncate(ai_result.combined_output, 1600),
            )

        verification = await self.verify(agent, task.id)
        if not verification.ok:
            changed_list = "\n".join(f"- {path}" for path in changed_files) or "- (none detected)"
            return self.store.update_task(
                task.id,
                status="failed",
                verification=truncate(verification.combined_output, 1800),
                summary=(
                    "Verification failed. Changes are left on the task branch for inspection.\n\n"
                    "Changed files:\n"
                    + changed_list
                ),
            )

        final_changed_files = await self.changed_files(agent.repo)
        unsafe_files = self.unsafe_files(final_changed_files)
        if unsafe_files:
            return self.store.update_task(
                task.id,
                status="failed",
                summary="Verification changed blocked files. Review manually:\n" + "\n".join(unsafe_files),
            )

        add = await self.run_command(["git", "add", "-A", "--", "."], agent.repo)
        if not add.ok:
            return self.store.update_task(
                task.id,
                status="failed",
                summary="Could not stage files.\n" + truncate(add.combined_output, 1800),
            )

        commit_message = f"{agent.name}: task {task.id} {task_title(task.request)}"
        commit = await self.run_command(["git", "commit", "-m", commit_message], agent.repo)
        if not commit.ok:
            return self.store.update_task(
                task.id,
                status="failed",
                summary="Could not commit files.\n" + truncate(commit.combined_output, 1800),
            )

        commit_hash = await self.run_command(["git", "rev-parse", "--short", "HEAD"], agent.repo)
        summary = "\n".join(
            [
                "Task completed and committed locally.",
                "",
                "Changed files:",
                *[f"- {path}" for path in final_changed_files],
                "",
                "AI summary:",
                truncate(ai_result.combined_output, 1200),
            ]
        )
        return self.store.update_task(
            task.id,
            status="committed",
            commit_hash=commit_hash.stdout.strip() if commit_hash.ok else None,
            summary=summary,
            verification=truncate(verification.combined_output or "Verification passed.", 1200),
        )

    async def run_qa_task(self, task: TaskRecord) -> TaskRecord:
        self.store.update_task(task.id, status="running")
        results: list[str] = []
        all_ok = True

        for agent_name in ("frontend", "backend"):
            agent = self.settings.agents[agent_name]
            if agent.repo is None:
                all_ok = False
                results.append(f"{agent_name}: no repository configured")
                continue

            dirty = await self.git_status(agent.repo)
            if dirty:
                all_ok = False
                results.append(f"{agent_name}: blocked by dirty repo\n{dirty}")
                continue

            verification = await self.verify(agent, task.id)
            if verification.ok:
                results.append(f"{agent_name}: verification passed")
            else:
                all_ok = False
                results.append(f"{agent_name}: verification failed\n{verification.combined_output}")

        bundle = "\n\n".join(results)
        if self.settings.qa_ai_review:
            review_root = self._qa_review_cwd()
            qa_agent = self.settings.agents["qa"]
            if review_root is None:
                results.append("(QA AI review skipped: no frontend or backend repo path for review context.)")
            else:
                try:
                    review = await self._provider_for(qa_agent).run_qa_review(
                        qa_agent,
                        task,
                        bundle,
                        review_cwd=review_root,
                    )
                    if review.ok:
                        results.append("QA AI narrative:\n" + truncate(review.combined_output, 2000))
                    else:
                        results.append(
                            "QA AI review failed:\n"
                            + truncate(review.combined_output or review.stderr, 1800),
                        )
                except Exception as exc:
                    results.append(f"QA AI review error: {exc}")

        final_text = "\n\n".join(results)
        return self.store.update_task(
            task.id,
            status="done" if all_ok else "failed",
            summary=truncate(final_text, 2400),
            verification=truncate(final_text, 2400),
        )

    async def push_task(self, task: TaskRecord) -> TaskRecord:
        agent = self.settings.agents[task.agent]
        if agent.repo is None:
            return self.store.update_task(task.id, status="blocked", summary="This task has no repository.")
        if not task.branch:
            return self.store.update_task(task.id, status="blocked", summary="This task has no branch to push.")
        if not task.commit_hash:
            return self.store.update_task(task.id, status="blocked", summary="This task has no commit to push.")

        dirty = await self.git_status(agent.repo)
        if dirty:
            return self.store.update_task(
                task.id,
                status="blocked",
                summary="Repo has uncommitted changes. Clean, commit, or stash them before pushing.\n\n" + dirty,
            )

        current_branch = await self.run_command(["git", "branch", "--show-current"], agent.repo)
        if current_branch.stdout.strip() != task.branch:
            checkout = await self.run_command(["git", "checkout", task.branch], agent.repo)
            if not checkout.ok:
                return self.store.update_task(
                    task.id,
                    status="failed",
                    summary="Could not check out task branch before push.\n" + checkout.combined_output,
                )

        push = await self.run_command(
            ["git", "push", "-u", self.settings.git_remote, task.branch],
            agent.repo,
        )
        if not push.ok:
            return self.store.update_task(
                task.id,
                status="failed",
                summary="Push failed.\n" + truncate(push.combined_output, 1800),
            )

        return self.store.update_task(task.id, status="pushed", pushed_at=self.sqlite_now())

    async def verify(self, agent: AgentConfig, task_id: int) -> CommandResult:
        outputs: list[str] = []
        for command in agent.verify_commands:
            self.store.add_log(task_id, "info", "Running verification: " + " ".join(command))
            result = await self.run_command(command, agent.repo)
            outputs.append("$ " + " ".join(command) + "\n" + (result.combined_output or "ok"))
            if not result.ok:
                return CommandResult(result.returncode, "\n\n".join(outputs), "")
        return CommandResult(0, "\n\n".join(outputs), "")

    async def git_status(self, repo: Path) -> str:
        result = await self.run_command(["git", "status", "--porcelain"], repo)
        return result.stdout.strip()

    async def changed_files(self, repo: Path) -> list[str]:
        result = await self.run_command(["git", "status", "--porcelain"], repo)
        files: list[str] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path)
        return files

    def unsafe_files(self, files: list[str]) -> list[str]:
        return [path for path in files if is_unsafe_relative_path(path)]

    def branch_name(self, agent: str, task_id: int, request: str) -> str:
        return f"agent/{agent}/task-{task_id}-{task_title(request)}"

    def sqlite_now(self) -> str:
        with self.store.session() as db:
            return str(db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])

    async def run_command(
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

        def command_for_subprocess(env: dict[str, str]) -> list[str]:
            if not command:
                raise ValueError("Cannot run an empty command.")
            executable = command[0]
            resolved = shutil.which(executable, path=env.get("PATH"))
            if not resolved:
                return command
            if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
                return ["cmd.exe", "/d", "/s", "/c", resolved, *command[1:]]
            return [resolved, *command[1:]]

        def call() -> CommandResult:
            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            workdir = Path(cwd).resolve() if cwd else None
            if workdir is not None and not workdir.is_dir():
                return CommandResult(1, "", f"Working directory does not exist: {workdir}")
            prepared_command = command_for_subprocess(env)
            completed = subprocess.run(
                prepared_command,
                cwd=str(workdir) if workdir else None,
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
        except ValueError as exc:
            return CommandResult(1, "", str(exc))
        except FileNotFoundError as exc:
            return CommandResult(127, "", str(exc))
