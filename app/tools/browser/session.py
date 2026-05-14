from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

logger = logging.getLogger("app.tools.browser.session")


@dataclass
class BrowserSessionBundle:
    context: Any
    page: Any
    console_logs: list[dict[str, str]] = field(default_factory=list)
    network_errors: list[dict[str, Any]] = field(default_factory=list)


class PlaywrightSessionManager:
    """Production-style: one Chromium launch; isolated ``BrowserContext`` per (agent, task_id)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, BrowserSessionBundle] = {}

    @staticmethod
    def session_key(agent: str, task_id: int | None) -> str:
        return f"{agent.strip().lower()}:{task_id if task_id is not None else 0}"

    async def _launch_unlocked(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImportError(
                "Playwright is required for browser tools. "
                "Install: pip install playwright && playwright install chromium",
            ) from exc
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("playwright chromium launched (headless)")

    async def ensure_browser(self) -> None:
        async with self._lock:
            await self._launch_unlocked()

    async def get_bundle(self, agent: str, task_id: int | None) -> BrowserSessionBundle:
        await self.ensure_browser()
        key = self.session_key(agent, task_id)
        if key in self._sessions:
            return self._sessions[key]

        async with self._lock:
            if key in self._sessions:
                return self._sessions[key]
            await self._launch_unlocked()
            assert self._browser is not None
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            context.set_default_timeout(self._settings.browser_default_timeout_ms)
            page = await context.new_page()
            console_logs: list[dict[str, str]] = []
            network_errors: list[dict[str, Any]] = []

            def on_console(msg: Any) -> None:
                try:
                    console_logs.append({"type": str(msg.type), "text": msg.text})
                except Exception:
                    pass

            def on_request_failed(request: Any) -> None:
                try:
                    fail = getattr(request, "failure", None)
                    txt = str(fail) if fail else "failed"
                    network_errors.append({"url": request.url, "error": txt, "kind": "request_failed"})
                except Exception:
                    pass

            def on_response(response: Any) -> None:
                try:
                    status = response.status
                    if status >= 400:
                        network_errors.append(
                            {"url": response.url, "status": int(status), "kind": "http_error"},
                        )
                except Exception:
                    pass

            page.on("console", on_console)
            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)

            bundle = BrowserSessionBundle(
                context=context,
                page=page,
                console_logs=console_logs,
                network_errors=network_errors,
            )
            self._sessions[key] = bundle
            return bundle

    async def close_session(self, agent: str, task_id: int | None) -> None:
        key = self.session_key(agent, task_id)
        async with self._lock:
            bundle = self._sessions.pop(key, None)
        if bundle is None:
            return
        try:
            await bundle.context.close()
        except Exception:
            logger.exception("browser context close failed")

    async def shutdown(self) -> None:
        async with self._lock:
            for bundle in list(self._sessions.values()):
                try:
                    await bundle.context.close()
                except Exception:
                    logger.exception("context close during shutdown")
            self._sessions.clear()
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    logger.exception("browser close")
            self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    logger.exception("playwright stop")
            self._playwright = None
