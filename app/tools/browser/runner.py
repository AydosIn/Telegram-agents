from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from app.agents.base import BaseAgent
from app.config import Settings
from app.tools.browser.analysis import placeholder_vision_note
from app.tools.browser.regression import capture_relative_path
from app.tools.browser.session import PlaywrightSessionManager
from app.tools.browser.url_policy import is_url_allowed
from app.tools.paths import PathValidationError, is_allowed_for_agent, normalize_workspace_relative, resolve_under_workspace

logger = logging.getLogger("app.tools.browser.runner")


async def _retry(
    factory: Callable[[], Any],
    *,
    attempts: int,
    delay_sec: float,
) -> Any:
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("browser op retry %s/%s: %s", i + 1, attempts, exc)
            if i + 1 < attempts:
                await asyncio.sleep(delay_sec)
    assert last is not None
    raise last


class BrowserToolRunner:
    """Playwright-backed browser tools with per-task isolation and Telegram-friendly attachments."""

    def __init__(
        self,
        workspace_root: Path,
        settings: Settings,
        *,
        sessions: PlaywrightSessionManager | None = None,
    ) -> None:
        self._workspace = workspace_root.resolve()
        self._settings = settings
        self._sessions = sessions or PlaywrightSessionManager(settings)

    def _resolve_screenshot_path(
        self,
        agent: BaseAgent,
        task_id: int | None,
        filename: str,
    ) -> Path:
        rel = capture_relative_path(agent.name, task_id, filename)
        n = normalize_workspace_relative(rel)
        parts = n.split("/")
        under_artifacts = (
            len(parts) >= 2 and parts[0] == "browser-artifacts" and parts[1] == agent.name
        )
        allowed_project = is_allowed_for_agent(
            workspace_root=self._workspace,
            relative_posix=n,
            allowed_directory_prefixes=agent.allowed_directories,
        )
        if not under_artifacts and not allowed_project:
            raise PathValidationError(
                "Screenshot path must be under "
                f"browser-artifacts/{agent.name}/... or the agent's project directories.",
            )
        parent_rel = str(Path(n).parent)
        if parent_rel and parent_rel != ".":
            resolve_under_workspace(self._workspace, parent_rel).mkdir(parents=True, exist_ok=True)
        return resolve_under_workspace(self._workspace, n)

    async def execute(
        self,
        agent: BaseAgent,
        tool: str,
        payload: dict[str, Any],
        *,
        task_id: int | None,
    ) -> tuple[bool, str, tuple[str, ...]]:
        attachments: list[str] = []
        timeout = self._settings.browser_default_timeout_ms
        retries = max(1, self._settings.browser_max_retries)
        retry_delay = self._settings.browser_retry_delay_sec

        try:
            if tool == "browser_close_session":
                await self._sessions.close_session(agent.name, task_id)
                return True, "Browser context closed for this task/session.", ()

            bundle = await self._sessions.get_bundle(agent.name, task_id)
            page = bundle.page

            if tool == "browser_open_page":
                url = str(payload.get("url") or "").strip()
                if not is_url_allowed(url, self._settings):
                    return (
                        False,
                        f"URL not allowed. Allowed hosts: {list(self._settings.browser_url_allowlist_hosts)}.",
                        (),
                    )

                async def _goto() -> None:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

                await _retry(_goto, attempts=retries, delay_sec=retry_delay)
                return True, f"Opened {url} (domcontentloaded).", ()

            if tool == "browser_click":
                selector = str(payload.get("selector") or "").strip()
                if not selector:
                    return False, "browser_click requires non-empty selector.", ()

                async def _click() -> None:
                    await page.click(selector, timeout=timeout, strict=True)

                await _retry(_click, attempts=retries, delay_sec=retry_delay)
                return True, f"Clicked {selector!r}.", ()

            if tool == "browser_type":
                selector = str(payload.get("selector") or "").strip()
                text = payload.get("text")
                if not selector or not isinstance(text, str):
                    return False, "browser_type requires selector and string text.", ()

                async def _fill() -> None:
                    await page.fill(selector, text, timeout=timeout, strict=True)

                await _retry(_fill, attempts=retries, delay_sec=retry_delay)
                return True, f"Typed into {selector!r}.", ()

            if tool == "browser_screenshot":
                raw_name = str(payload.get("path") or payload.get("filename") or "shot.png").strip()
                if not raw_name.lower().endswith(".png"):
                    raw_name += ".png"
                abs_path = self._resolve_screenshot_path(agent, task_id, raw_name)
                full_page = bool(payload.get("full_page", True))

                async def _shot() -> None:
                    await page.screenshot(path=str(abs_path), full_page=full_page, type="png")

                await _retry(_shot, attempts=retries, delay_sec=retry_delay)
                attachments.append(str(abs_path.resolve()))
                note = placeholder_vision_note(abs_path)
                return (
                    True,
                    f"Screenshot saved: {abs_path.relative_to(self._workspace)}\n{note}",
                    tuple(attachments),
                )

            if tool == "browser_console_logs":
                logs = bundle.console_logs[-200:]
                body = json.dumps(logs, ensure_ascii=False, indent=2) if logs else "[]"
                cap = 12_000
                if len(body) > cap:
                    body = body[: cap - 30] + "\n…[truncated]"
                return True, f"Console logs (tail {len(logs)} of {len(bundle.console_logs)}):\n{body}", ()

            if tool == "browser_network_errors":
                errs = bundle.network_errors[-200:]
                body = json.dumps(errs, ensure_ascii=False, indent=2) if errs else "[]"
                cap = 12_000
                if len(body) > cap:
                    body = body[: cap - 30] + "\n…[truncated]"
                return (
                    True,
                    f"Network issues (tail {len(errs)} of {len(bundle.network_errors)}):\n{body}",
                    (),
                )

            if tool == "browser_responsive_test":
                raw_base = str(payload.get("basename") or "responsive").strip() or "responsive"
                viewports: list[tuple[int, int]] = [
                    (375, 667),
                    (768, 1024),
                    (1280, 720),
                ]
                lines: list[str] = []
                for w, h in viewports:
                    await page.set_viewport_size({"width": w, "height": h})
                    fname = f"{raw_base}_{w}x{h}.png"
                    abs_path = self._resolve_screenshot_path(agent, task_id, fname)

                    async def _cap(p: Path = abs_path) -> None:
                        await page.screenshot(path=str(p), full_page=True, type="png")

                    await _retry(_cap, attempts=retries, delay_sec=retry_delay)
                    attachments.append(str(abs_path.resolve()))
                    lines.append(f"- {w}x{h} → {abs_path.relative_to(self._workspace).as_posix()}")
                await page.set_viewport_size({"width": 1280, "height": 720})
                msg = "Responsive captures:\n" + "\n".join(lines)
                return True, msg, tuple(attachments)

            if tool == "browser_analyze_screenshot":
                raw = str(payload.get("path") or "").strip()
                if not raw:
                    return False, "browser_analyze_screenshot requires workspace-relative path.", ()
                rel = normalize_workspace_relative(raw)
                abs_path = resolve_under_workspace(self._workspace, rel)
                if not abs_path.is_file():
                    return False, f"File not found: {rel}", ()
                note = placeholder_vision_note(abs_path)
                return True, note, ()

            return False, f"Unknown browser tool {tool!r}.", ()

        except Exception as exc:  # noqa: BLE001
            logger.exception("browser tool failed")
            return False, str(exc), tuple(attachments)
