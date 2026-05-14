from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.factory import WORKSPACE_PROJECT_BACKEND, WORKSPACE_PROJECT_FRONTEND
from app.config import Settings
from app.store import Store, make_terminal_fingerprint
from app.tools.models import ToolResult
from app.tools.paths import PathValidationError, is_allowed_for_agent, normalize_workspace_relative, resolve_under_workspace
from app.tools.terminal.environment import LocalExecutionEnvironment
from app.tools.terminal.policy import CommandPolicy, resolve_preset_tool, validate_terminal_argv, validate_terminal_command

logger = logging.getLogger("app.tools.terminal.runner")

StreamCb = Callable[[str], Awaitable[None]] | None


class SafeTerminalRunner:
    """
    Phase-2 local subprocess runner behind policy + optional approvals.
    Swap `LocalExecutionEnvironment` for a Docker-backed implementation later without changing AI tool JSON.
    """

    def __init__(self, workspace_root: Path, store: Store, settings: Settings) -> None:
        self._workspace = workspace_root.resolve()
        self._store = store
        self._settings = settings
        self._env = LocalExecutionEnvironment()

    async def execute(
        self,
        agent: BaseAgent,
        tool: str,
        payload: dict[str, Any],
        *,
        stream: StreamCb = None,
    ) -> ToolResult:
        timeout = int(payload.get("timeout_sec") or self._settings.terminal_default_timeout_sec)
        timeout = max(5, min(timeout, 3600))

        try:
            argv, cwd, policy = self._resolve_argv_cwd_policy(agent, tool, payload)
        except PathValidationError as exc:
            return ToolResult(False, tool, str(exc))
        except ValueError as exc:
            return ToolResult(False, tool, str(exc))

        cwd_str = str(cwd.resolve())
        fp = make_terminal_fingerprint(argv, cwd_str)

        if not policy.ok:
            return ToolResult(False, tool, policy.reason)

        approval_id = payload.get("approval_id")
        if policy.needs_approval:
            if approval_id is None:
                pending = self._store.insert_terminal_pending(agent.name, argv, cwd_str)
                cmd_s = " ".join(argv)
                msg = (
                    f"PENDING_APPROVAL pending_id={pending}. Command: {cmd_s}\n"
                    f"Workspace cwd: {cwd_str}\n"
                    f"A human must run /approve_terminal {pending} in Telegram, then repeat this tool call "
                    f'with the same arguments plus "approval_id": {pending}.'
                )
                logger.info("terminal pending approval id=%s agent=%s cmd=%s", pending, agent.name, cmd_s)
                return ToolResult(False, tool, msg)
            if not self._store.verify_and_delete_terminal_approval(
                int(approval_id),
                agent=agent.name,
                fingerprint=fp,
            ):
                return ToolResult(
                    False,
                    tool,
                    "approval_id invalid, not approved, or fingerprint mismatch. Request a new approval.",
                )
        elif approval_id is not None:
            logger.debug("terminal ignoring spurious approval_id for tool=%s", tool)

        stream_wrapped = self._throttle_stream(stream, self._settings.terminal_stream_interval_sec)

        started = time.perf_counter_ns()
        result = await self._env.run(
            list(argv),
            cwd=cwd,
            env=None,
            timeout_sec=timeout,
            on_stream_chunk=stream_wrapped,
            stream_config=None,
        )
        elapsed_ms = int((time.perf_counter_ns() - started) / 1_000_000)

        self._store.log_terminal_execution(
            agent=agent.name,
            cwd=cwd_str,
            argv=argv,
            tool=tool,
            exit_code=result.exit_code,
            approved=(policy.needs_approval and approval_id is not None) or not policy.needs_approval,
            timed_out=result.timed_out,
            stdout=result.stdout,
            stderr=result.stderr,
        )

        logger.info(
            "terminal_done agent=%s tool=%s exit=%s timeout=%s ms=%s",
            agent.name,
            tool,
            result.exit_code,
            result.timed_out,
            elapsed_ms,
        )
        body = result.as_tool_message()
        ok = not result.timed_out and result.exit_code == 0
        return ToolResult(ok, tool, body)

    @staticmethod
    def _throttle_stream(stream: StreamCb, min_interval_sec: float) -> StreamCb:
        if stream is None:
            return None
        state = {"t": -min_interval_sec}

        async def _wrapped(chunk: str) -> None:
            now = time.monotonic()
            if now - state["t"] < min_interval_sec:
                return
            state["t"] = now
            await stream(chunk)

        return _wrapped

    def _resolve_argv_cwd_policy(
        self,
        agent: BaseAgent,
        tool: str,
        payload: dict[str, Any],
    ) -> tuple[tuple[str, ...], Path, CommandPolicy]:
        project = payload.get("project")
        cwd = self._resolve_cwd(agent, payload)

        if tool == "run_command":
            cmd = payload.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                raise ValueError('run_command requires string "command".')
            pol = validate_terminal_command(cmd.strip(), agent)
            if not pol.ok:
                raise ValueError(pol.reason)
            return pol.argv, cwd, pol

        preset = resolve_preset_tool(tool, agent.name, project=str(project) if project else None)
        if preset is None:
            raise ValueError(
                f"Could not resolve {tool} for agent {agent.name}. "
                f'QA agents must pass "project":"frontend" or "backend".',
            )
        pol = validate_terminal_argv(preset, agent)
        if not pol.ok:
            raise ValueError(pol.reason)
        return preset, cwd, pol

    def _resolve_cwd(self, agent: BaseAgent, payload: dict[str, Any]) -> Path:
        rel = (payload.get("cwd") or payload.get("working_directory") or "").strip()
        project = (payload.get("project") or "").strip().lower()

        if rel:
            n = normalize_workspace_relative(rel)
            if not is_allowed_for_agent(
                workspace_root=self._workspace,
                relative_posix=n,
                allowed_directory_prefixes=agent.allowed_directories,
            ):
                raise PathValidationError("cwd is outside this agent's allowed directories.")
            return resolve_under_workspace(self._workspace, n)

        if agent.name == "qa" and len(agent.allowed_directories) > 1:
            if project in ("fe", "frontend"):
                sub = WORKSPACE_PROJECT_FRONTEND
            elif project in ("be", "backend"):
                sub = WORKSPACE_PROJECT_BACKEND
            else:
                raise PathValidationError(
                    'QA terminal tools require "project":"frontend" or "backend", or an explicit cwd.',
                )
            if not is_allowed_for_agent(
                workspace_root=self._workspace,
                relative_posix=sub,
                allowed_directory_prefixes=agent.allowed_directories,
            ):
                raise PathValidationError("Invalid project for QA.")
            return resolve_under_workspace(self._workspace, sub)

        if len(agent.allowed_directories) == 1:
            sub = agent.allowed_directories[0].replace("\\", "/").strip("/")
            return resolve_under_workspace(self._workspace, sub)

        raise PathValidationError("Multiple project roots; pass an explicit cwd relative to the workspace.")
