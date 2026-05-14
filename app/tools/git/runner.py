from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.config import Settings
from app.store import Store, make_git_push_fingerprint
from app.task_lifecycle import TaskStatus
from app.tools.git import operations as git_ops
from app.tools.git.diff_summary import summarize_diff_text

logger = logging.getLogger("app.tools.git.runner")

CommitSuggestFn = Callable[[str, str], Awaitable[str]]


class GitToolRunner:
    """GitPython-backed tools with Telegram approval for every push."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        commit_suggest_fn: CommitSuggestFn | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._commit_suggest_fn = commit_suggest_fn

    def _resolve_repo(self, agent: BaseAgent, payload: dict[str, Any]) -> Path:
        target = (payload.get("repo_target") or payload.get("project") or "").strip().lower()
        if agent.name in {"frontend", "backend"}:
            cfg = self._settings.agents[agent.name]
            if cfg.repo is None:
                raise ValueError(f"No repository configured for `{agent.name}` agent.")
            return cfg.repo.resolve()

        if agent.name == "qa":
            if target not in {"frontend", "backend"}:
                raise ValueError(
                    'QA git tools require "repo_target":"frontend" or "backend" (or legacy "project").',
                )
            cfg = self._settings.agents[target]
            if cfg.repo is None:
                raise ValueError(f"No repository configured for `{target}`.")
            return cfg.repo.resolve()

        raise ValueError("Git tools are not available for this agent.")

    async def execute(
        self,
        agent: BaseAgent,
        tool: str,
        payload: dict[str, Any],
        *,
        task_id: int | None = None,
    ) -> tuple[bool, str]:
        try:
            repo = self._resolve_repo(agent, payload)
        except ValueError as exc:
            return False, str(exc)

        try:
            if tool == "git_status":
                out = await asyncio.to_thread(git_ops.git_status_porcelain, repo)
                return True, f"git status --porcelain:\n{out}"

            if tool == "git_diff":
                staged = bool(payload.get("staged"))
                stat = bool(payload.get("stat"))
                paths = payload.get("paths")
                path_list = [str(p) for p in paths] if isinstance(paths, list) else None
                diff = await asyncio.to_thread(
                    git_ops.git_diff_text,
                    repo,
                    staged=staged,
                    stat=stat,
                    paths=path_list,
                )
                cap = 24_000
                body = diff if len(diff) <= cap else diff[: cap - 40] + "\n…[truncated]"
                return True, body

            if tool == "git_summarize_diff":
                staged = bool(payload.get("staged"))
                diff = await asyncio.to_thread(git_ops.git_diff_text, repo, staged=staged, stat=False)
                summary = summarize_diff_text(diff)
                return True, summary

            if tool == "git_suggest_commit_message":
                if self._commit_suggest_fn is None:
                    return False, "Commit message suggestions are not configured for this runtime."
                diff = await asyncio.to_thread(git_ops.git_diff_text, repo, staged=True, stat=True)
                suggestion = await self._commit_suggest_fn(agent.name, diff)
                return True, f"Suggested commit message:\n{suggestion}"

            if tool == "git_create_branch":
                if agent.name == "qa":
                    return False, "QA cannot create branches (read-only git)."
                name = str(payload.get("name") or "").strip()
                old_head, branch_name = await asyncio.to_thread(git_ops.git_create_branch, repo, name)
                extra = ""
                if task_id is not None:
                    self._store.update_task(
                        task_id,
                        branch=branch_name,
                        rollback_ref=old_head,
                    )
                    extra = f"\n(task #{task_id}: rollback_ref saved as {old_head[:12]})."
                return True, f"Created and checked out branch `{branch_name}` from {old_head[:12]}.{extra}"

            if tool == "git_checkout":
                if agent.name == "qa":
                    return False, "QA cannot check out branches (read-only git)."
                branch = str(payload.get("branch") or "").strip()
                head = await asyncio.to_thread(git_ops.git_checkout, repo, branch)
                return True, f"Checked out `{branch}` (HEAD {head})."

            if tool == "git_commit":
                if agent.name == "qa":
                    return False, "QA cannot commit (read-only git)."
                message = str(payload.get("message") or "").strip()
                if not message:
                    return False, "git_commit requires string `message`."
                short = await asyncio.to_thread(git_ops.git_commit, repo, message)
                files = await asyncio.to_thread(git_ops.git_changed_paths, repo)
                if task_id is not None and files:
                    self._store.update_task(task_id, files_changed=json.dumps(files))
                return True, f"Committed as {short}.\nWorking tree status may still show further changes."

            if tool == "git_push":
                if agent.name == "qa":
                    return False, "QA cannot push."
                remote = str(payload.get("remote") or self._settings.git_remote).strip() or "origin"
                br_raw = payload.get("branch")
                branch_opt = str(br_raw).strip() if br_raw else ""
                branch = branch_opt or None
                repo_s = str(repo.resolve())
                active_branch = await asyncio.to_thread(lambda: git_ops.open_repo(repo).active_branch.name)
                effective_branch = branch or active_branch

                fp = make_git_push_fingerprint(
                    remote=remote,
                    branch=effective_branch,
                    repo_path=repo_s,
                    agent=agent.name,
                )
                approval_raw = payload.get("approval_id")

                if approval_raw is None:
                    pending = self._store.insert_git_push_pending(
                        agent=agent.name,
                        remote=remote,
                        branch=effective_branch,
                        repo_path=repo_s,
                    )
                    msg = (
                        f"PENDING_APPROVAL pending_id={pending} kind=git_push\n"
                        f"Remote: {remote}\nBranch: {effective_branch}\nRepo: {repo_s}\n"
                        f"A human **must** run `/approve_git_push {pending}` in Telegram, then repeat this tool call "
                        f'with the same arguments plus `"approval_id": {pending}`.'
                    )
                    logger.info("git_push pending approval id=%s agent=%s", pending, agent.name)
                    return False, msg

                if not self._store.verify_and_delete_git_push_approval(
                    int(approval_raw),
                    agent=agent.name,
                    fingerprint=fp,
                ):
                    return (
                        False,
                        "approval_id invalid, not approved, or fingerprint mismatch. Request a new approval.",
                    )

                try:
                    msg = await asyncio.to_thread(git_ops.git_push, repo, remote, effective_branch)
                except ValueError as exc:
                    return False, str(exc)
                if task_id is not None:
                    self._store.update_task(
                        task_id,
                        pushed_at=self._store.current_timestamp(),
                        status=TaskStatus.DONE,
                    )
                return True, msg

            if tool == "git_rollback":
                if agent.name == "qa":
                    return False, "QA cannot roll back (read-only git)."
                ref_raw = payload.get("ref")
                ref = str(ref_raw).strip() if ref_raw else None
                if not ref and task_id is not None:
                    task = self._store.get_task(task_id)
                    ref = task.rollback_ref
                if not ref:
                    return False, (
                        "git_rollback needs `ref` or a task with `rollback_ref` "
                        "(create a branch with git_create_branch on a task thread)."
                    )
                head = await asyncio.to_thread(git_ops.git_reset_hard, repo, ref)
                return True, f"Hard reset complete. HEAD is now {head} (ref was {ref})."

        except ValueError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("git tool failed")
            return False, str(exc)

        return False, f"Unknown git tool {tool!r}."
