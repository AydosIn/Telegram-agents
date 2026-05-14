from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from app.agents.base import BaseAgent
from app.config import AgentConfig, Settings
from app.formatting import truncate
from app.models import CommandResult
from app.prompts_tools import (
    build_filesystem_tool_chat_prompt,
    classify_model_output,
    try_parse_json_object,
    user_message_implies_filesystem_action,
)
from app.providers import build_ai_provider_for
from app.providers.base import AIProvider
from app.tools.executor import ToolExecutor

logger = logging.getLogger("app.tool_chat")

StreamEmit = Callable[[str], Awaitable[None]] | None

_CORRECTION_USE_TOOLS = (
    "You must use tools instead of describing actions. "
    'Output a single JSON object only: {"tool":"read_file"|"write_file"|"edit_file"|"list_files"|'
    '"run_command"|"run_test"|"run_build"|"run_lint"|"git_status"|"git_diff"|"git_summarize_diff"|'
    '"git_suggest_commit_message"|"git_create_branch"|"git_commit"|"git_checkout"|"git_push"|"git_rollback"|'
    '"browser_open_page"|"browser_click"|"browser_type"|"browser_screenshot"|"browser_console_logs"|'
    '"browser_network_errors"|"browser_responsive_test"|"browser_close_session"|"browser_analyze_screenshot", ...} '
    "with the required fields. No prose."
)

_CORRECTION_INVALID_JSON = (
    "Your last output was not a valid JSON object with a top-level \"tool\" or \"reply\" key. "
    "Reply with exactly one JSON object and nothing else."
)

_CORRECTION_REPLY_TOO_SOON = (
    "The user message requires filesystem work and no tool has run for it yet. "
    'You must output a tool JSON next, not {"reply":...}. Do it now.'
)


class ToolChatOrchestrator:
    """
    Model → parse JSON (tool or reply) → execute tool → append result → repeat.

    Uses ``orchestration_prompt=True`` so providers do not wrap this in ``build_chat_prompt``
    (which previously told models to refuse tools — the main reason they acted like chatbots).
    """

    def __init__(
        self,
        settings: Settings,
        executor: ToolExecutor,
        *,
        max_rounds: int,
        stream_emit: StreamEmit = None,
    ) -> None:
        self._settings = settings
        self._executor = executor
        self._max_rounds = max(1, max_rounds)
        self._providers: dict[str, AIProvider] = {}
        self._stream_emit = stream_emit

    def _assistant_raw_for_transcript(self, raw: str) -> str:
        limit = self._settings.tool_chat_assistant_raw_max_chars
        if limit <= 0 or len(raw) <= limit:
            return raw
        return truncate(raw, limit)

    def _provider_for(self, agent_cfg: AgentConfig) -> AIProvider:
        key = self._normalized_provider_key(agent_cfg.ai_provider or self._settings.ai_provider)
        if key not in self._providers:
            self._providers[key] = build_ai_provider_for(self._settings, key)
        return self._providers[key]

    @staticmethod
    def _normalized_provider_key(raw: str) -> str:
        name = raw.strip().lower()
        if name in ("codex", "codex_cli"):
            return "codex_cli"
        if name in ("gemini", "google_gemini"):
            return "gemini"
        if name == "openai":
            return "openai"
        return name

    async def run(
        self,
        agent_cfg: AgentConfig,
        agent_profile: BaseAgent,
        user_message: str,
        *,
        task_id: int | None = None,
    ) -> CommandResult:
        implies_fs = user_message_implies_filesystem_action(user_message)
        transcript_lines: list[str] = [f"user: {user_message.strip()}"]
        provider = self._provider_for(agent_cfg)
        tools_executed_for_request = 0
        attachment_accum: list[str] = []

        logger.info(
            "tool_chat start agent=%s implies_fs=%s max_rounds=%s",
            agent_profile.name,
            implies_fs,
            self._max_rounds,
        )

        for round_ix in range(self._max_rounds):
            enforce_first = implies_fs and tools_executed_for_request == 0

            tool_payload, structured_reply, raw_fallback = await self._model_turn_with_retries(
                agent_cfg=agent_cfg,
                agent_profile=agent_profile,
                provider=provider,
                transcript_lines=transcript_lines,
                round_ix=round_ix,
                enforce_tool_first=enforce_first,
            )

            if tool_payload is not None:
                tool_result = await self._executor.execute(
                    agent_profile,
                    tool_payload,
                    stream_callback=self._stream_emit,
                    task_id=task_id,
                )
                tools_executed_for_request += 1
                if tool_result.attachment_paths:
                    attachment_accum.extend(tool_result.attachment_paths)
                _dlim = self._settings.tool_chat_tool_result_max_chars
                msg = tool_result.message or ""
                detail = msg if len(msg) <= _dlim else msg[:_dlim] + "…"
                payload_log = {
                    "tool": tool_result.tool,
                    "ok": tool_result.ok,
                    "detail": detail,
                }
                logger.info(
                    "tool_chat executed round=%s tool=%s ok=%s",
                    round_ix,
                    tool_result.tool,
                    tool_result.ok,
                )
                logger.debug("tool_chat tool_result=%s", json.dumps(payload_log, ensure_ascii=False))
                transcript_lines.append(
                    "tool_executor: " + json.dumps(payload_log, ensure_ascii=False),
                )
                continue

            if structured_reply is not None:
                logger.info("tool_chat final_reply round=%s len=%s", round_ix, len(structured_reply))
                return CommandResult(0, structured_reply, "", tuple(attachment_accum))

            if raw_fallback.strip():
                if implies_fs and tools_executed_for_request == 0:
                    logger.warning(
                        "tool_chat round=%s giving fallback prose but fs implied and no tools ran",
                        round_ix,
                    )
                    return CommandResult(
                        1,
                        "",
                        "The model returned conversational text instead of a tool JSON for a filesystem request. "
                        "Check logs (logger `app.tool_chat`).",
                        tuple(attachment_accum),
                    )
                logger.info("tool_chat non_fs conversational reply round=%s", round_ix)
                return CommandResult(0, raw_fallback.strip(), "", tuple(attachment_accum))

            logger.warning("tool_chat empty output round=%s", round_ix)
            return CommandResult(1, "", "The model returned an empty response during tool orchestration.", tuple(attachment_accum))

        return CommandResult(
            1,
            "",
            "Tool round limit reached without a final reply. Increase TOOL_CHAT_MAX_ROUNDS or narrow the request.",
            tuple(attachment_accum),
        )

    async def _model_turn_with_retries(
        self,
        *,
        agent_cfg: AgentConfig,
        agent_profile: BaseAgent,
        provider: AIProvider,
        transcript_lines: list[str],
        round_ix: int,
        enforce_tool_first: bool,
    ) -> tuple[dict | None, str | None, str]:
        """Up to 2 attempts per round: optional correction injection on the first failure."""
        reminder = ""
        last_raw = ""

        for attempt in range(2):
            conversation_base = "\n".join(transcript_lines)
            prompt = build_filesystem_tool_chat_prompt(
                agent_cfg,
                agent_profile,
                conversation_base + reminder,
                enforce_tool_first=enforce_tool_first,
                orchestration_reminder="",
            )

            llm = await provider.run_chat(agent_cfg, prompt, orchestration_prompt=True)
            if not llm.ok:
                logger.error(
                    "tool_chat provider_error round=%s attempt=%s detail=%s",
                    round_ix,
                    attempt,
                    llm.combined_output[:1500],
                )
                return None, None, llm.combined_output or ""

            raw = (llm.stdout or "").strip()
            last_raw = raw
            logger.info(
                "tool_chat model_out round=%s attempt=%s chars=%s",
                round_ix,
                attempt,
                len(raw),
            )
            logger.debug("tool_chat raw_model_output round=%s attempt=%s\n%s", round_ix, attempt, raw)

            tool_pl, reply_pl, raw_fb = classify_model_output(raw)
            logger.info(
                "tool_chat parsed round=%s attempt=%s has_tool=%s has_reply=%s",
                round_ix,
                attempt,
                tool_pl is not None,
                reply_pl is not None,
            )
            if tool_pl is not None:
                logger.debug("tool_chat parsed_tool_payload=%s", json.dumps(tool_pl, ensure_ascii=False)[:4000])
                transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
                return tool_pl, None, raw_fb

            if reply_pl is not None:
                if enforce_tool_first and attempt == 0:
                    logger.warning(
                        "tool_chat retry: structured reply before tools round=%s",
                        round_ix,
                    )
                    transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
                    reminder = f"\nsystem_injection: {_CORRECTION_REPLY_TOO_SOON}\n"
                    continue
                transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
                return None, reply_pl, raw_fb

            had_json = try_parse_json_object(raw) is not None
            if attempt == 0:
                if not enforce_tool_first:
                    # Casual message: do not force a second JSON-only attempt.
                    transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
                    return None, None, raw_fb
                logger.warning(
                    "tool_chat retry: unstructured or bad JSON round=%s had_json_shape=%s",
                    round_ix,
                    had_json,
                )
                transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
                if had_json:
                    reminder = f"\nsystem_injection: {_CORRECTION_INVALID_JSON}\n"
                else:
                    msg = _CORRECTION_REPLY_TOO_SOON if enforce_tool_first else _CORRECTION_USE_TOOLS
                    reminder = f"\nsystem_injection: {msg}\n"
                continue

            logger.error("tool_chat parse failure after retry round=%s raw_prefix=%r", round_ix, raw[:300])
            transcript_lines.append(f"assistant_raw: {self._assistant_raw_for_transcript(raw)}")
            return None, None, raw_fb

        return None, None, last_raw
