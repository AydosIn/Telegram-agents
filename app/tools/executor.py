from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.config import Settings
from app.store import Store
from app.safety import is_unsafe_relative_path
from app.tools import filesystem_ops
from app.tools.models import ToolResult
from app.tools.paths import PathValidationError, is_allowed_for_agent, normalize_workspace_relative, resolve_under_workspace
from app.tools.git.runner import GitToolRunner
from app.tools.browser.session import PlaywrightSessionManager
from app.tools.browser.runner import BrowserToolRunner
from app.tools.terminal.runner import SafeTerminalRunner

logger = logging.getLogger("app.tools.executor")

FILESYSTEM_TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_files"})
TERMINAL_TOOLS = frozenset({"run_command", "run_test", "run_build", "run_lint"})
GIT_TOOLS = frozenset(
    {
        "git_status",
        "git_diff",
        "git_summarize_diff",
        "git_suggest_commit_message",
        "git_create_branch",
        "git_commit",
        "git_checkout",
        "git_push",
        "git_rollback",
    },
)

BROWSER_TOOLS = frozenset(
    {
        "browser_open_page",
        "browser_click",
        "browser_type",
        "browser_screenshot",
        "browser_console_logs",
        "browser_network_errors",
        "browser_responsive_test",
        "browser_close_session",
        "browser_analyze_screenshot",
    },
)

StreamCallback = Callable[[str], Awaitable[None]] | None
CommitSuggestFn = Callable[[str, str], Awaitable[str]]


class ToolExecutor:
    """Filesystem tools plus gated terminal execution (Phase 2)."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        store: Store | None = None,
        settings: Settings | None = None,
        commit_suggest_fn: CommitSuggestFn | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._store = store
        self._settings = settings
        self._terminal: SafeTerminalRunner | None = None
        self._git: GitToolRunner | None = None
        self._browser_sessions: PlaywrightSessionManager | None = None
        self._browser: BrowserToolRunner | None = None
        if store is not None and settings is not None:
            self._terminal = SafeTerminalRunner(self._workspace_root, store, settings)
            if settings.git_tools_enabled:
                self._git = GitToolRunner(store, settings, commit_suggest_fn=commit_suggest_fn)
            if settings.browser_tools_enabled:
                self._browser_sessions = PlaywrightSessionManager(settings)
                self._browser = BrowserToolRunner(
                    self._workspace_root,
                    settings,
                    sessions=self._browser_sessions,
                )

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def shutdown_browser(self) -> None:
        if self._browser_sessions is not None:
            try:
                await self._browser_sessions.shutdown()
            except Exception:
                logging.exception("Playwright shutdown failed")
            self._browser_sessions = None
        self._browser = None

    async def execute(
        self,
        agent: BaseAgent,
        payload: dict[str, Any],
        *,
        stream_callback: StreamCallback = None,
        task_id: int | None = None,
    ) -> ToolResult:
        tool_raw = payload.get("tool")
        tool = str(tool_raw or "").strip()
        if tool not in agent.tools:
            self._log_tool(agent, tool, payload, ok=False, message="Tool not available for this agent.")
            return ToolResult(False, tool or "(missing)", "This agent cannot run that tool.")

        if tool in TERMINAL_TOOLS:
            if self._settings is None or not self._settings.terminal_tools_enabled:
                msg = "Terminal tools are disabled (set TERMINAL_TOOLS_ENABLED=true)."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            if self._store is None or self._terminal is None:
                msg = "Terminal tools need a configured Store."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            try:
                result = await self._terminal.execute(agent, tool, payload, stream=stream_callback)
            except Exception as exc:  # noqa: BLE001
                logging.exception("terminal tool failed")
                self._log_tool(agent, tool, payload, ok=False, message=str(exc))
                return ToolResult(False, tool, str(exc))
            self._log_tool(agent, tool, payload, ok=result.ok, message=result.message[:500])
            return result

        if tool in GIT_TOOLS:
            if self._settings is None or not self._settings.git_tools_enabled:
                msg = "Git tools are disabled (set GIT_TOOLS_ENABLED=true)."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            if self._store is None or self._git is None:
                msg = "Git tools need a configured Store."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            try:
                ok, body = await self._git.execute(agent, tool, payload, task_id=task_id)
            except Exception as exc:  # noqa: BLE001
                logging.exception("git tool failed")
                self._log_tool(agent, tool, payload, ok=False, message=str(exc))
                return ToolResult(False, tool, str(exc))
            self._log_tool(agent, tool, payload, ok=ok, message=body[:500])
            return ToolResult(ok, tool, body)

        if tool in BROWSER_TOOLS:
            if self._settings is None or not self._settings.browser_tools_enabled:
                msg = "Browser tools are disabled (set BROWSER_TOOLS_ENABLED=true)."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            if self._browser is None:
                msg = "Browser tools are not initialized."
                self._log_tool(agent, tool, payload, ok=False, message=msg)
                return ToolResult(False, tool, msg)
            try:
                ok, body, att = await self._browser.execute(agent, tool, payload, task_id=task_id)
            except Exception as exc:  # noqa: BLE001
                logging.exception("browser tool failed")
                self._log_tool(agent, tool, payload, ok=False, message=str(exc))
                return ToolResult(False, tool, str(exc))
            self._log_tool(agent, tool, payload, ok=ok, message=body[:500])
            return ToolResult(ok, tool, body, att)

        if tool not in FILESYSTEM_TOOLS:
            self._log_tool(agent, tool, payload, ok=False, message="Unknown tool.")
            return ToolResult(False, tool, "Unknown tool.")

        try:
            if tool == "read_file":
                result = await self._read_file(agent, payload)
            elif tool == "write_file":
                result = await self._write_file(agent, payload)
            elif tool == "edit_file":
                result = await self._edit_file(agent, payload)
            else:
                result = await self._list_files(agent, payload)
        except PathValidationError as exc:
            self._log_tool(agent, tool, payload, ok=False, message=str(exc))
            return ToolResult(False, tool, str(exc))
        except (OSError, ValueError) as exc:
            self._log_tool(agent, tool, payload, ok=False, message=str(exc))
            return ToolResult(False, tool, str(exc))

        self._log_tool(agent, tool, payload, ok=result.ok, message=result.message)
        return result

    def _log_tool(
        self,
        agent: BaseAgent,
        tool: str,
        payload: dict[str, Any],
        *,
        ok: bool,
        message: str,
    ) -> None:
        safe_args: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "content" and isinstance(value, str):
                safe_args[key] = f"<{len(value)} chars>"
            elif key in {"old_text", "new_text"} and isinstance(value, str):
                safe_args[key] = f"<{len(value)} chars>"
            elif key == "command" and isinstance(value, str):
                safe_args[key] = value[:400]
            else:
                safe_args[key] = value
        logger.info(
            "tool_usage agent=%s role=%s tool=%s ok=%s args=%s detail=%s",
            agent.name,
            agent.role,
            tool,
            ok,
            safe_args,
            message[:500],
        )

    def _guard_path(self, agent: BaseAgent, relative_posix: str) -> Path:
        if relative_posix not in {".", ""} and is_unsafe_relative_path(relative_posix):
            raise PathValidationError("This path is blocked by workspace safety rules.")
        if not is_allowed_for_agent(
            workspace_root=self._workspace_root,
            relative_posix=relative_posix,
            allowed_directory_prefixes=agent.allowed_directories,
        ):
            raise PathValidationError("Path is outside this agent's allowed directories.")
        return resolve_under_workspace(self._workspace_root, relative_posix)

    async def _read_file(self, agent: BaseAgent, payload: dict[str, Any]) -> ToolResult:
        rel = normalize_workspace_relative(str(payload.get("path", "")))
        abs_path = self._guard_path(agent, rel)
        text = await asyncio.to_thread(filesystem_ops.read_file_sync, abs_path)
        snippet = text if len(text) <= 12000 else text[:12000] + "\n\n…(truncated)"
        return ToolResult(True, "read_file", f"Contents of {rel}:\n\n{snippet}")

    async def _write_file(self, agent: BaseAgent, payload: dict[str, Any]) -> ToolResult:
        rel = normalize_workspace_relative(str(payload.get("path", "")))
        if rel in {".", ""}:
            raise PathValidationError("write_file requires a file path.")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("write_file requires string 'content'.")
        abs_path = self._guard_path(agent, rel)
        await asyncio.to_thread(filesystem_ops.write_file_sync, abs_path, content)
        return ToolResult(True, "write_file", f"Wrote {rel} ({len(content)} chars).")

    async def _edit_file(self, agent: BaseAgent, payload: dict[str, Any]) -> ToolResult:
        rel = normalize_workspace_relative(str(payload.get("path", "")))
        if rel in {".", ""}:
            raise PathValidationError("edit_file requires a file path.")
        old_text = payload.get("old_text")
        new_text = payload.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ValueError("edit_file requires string 'old_text' and 'new_text'.")
        abs_path = self._guard_path(agent, rel)
        await asyncio.to_thread(filesystem_ops.edit_file_sync, abs_path, old_text, new_text)
        return ToolResult(True, "edit_file", f"Updated {rel} (single replacement).")

    async def _list_files(self, agent: BaseAgent, payload: dict[str, Any]) -> ToolResult:
        rel = normalize_workspace_relative(str(payload.get("path", ".")))
        if rel == "." and agent.allowed_directories and len(agent.allowed_directories) == 1:
            rel = agent.allowed_directories[0].replace("\\", "/").strip("/")
        elif rel == "." and len(agent.allowed_directories) > 1:
            options = ", ".join(f"{p}/" for p in agent.allowed_directories)
            raise PathValidationError(
                "list_files needs a concrete directory when multiple project roots are allowed. "
                f"Use one of: {options}",
            )
        abs_path = self._guard_path(agent, rel)
        lines = await asyncio.to_thread(filesystem_ops.list_files_sync, abs_path)
        body = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult(True, "list_files", f"Listing {rel}:\n{body}")
