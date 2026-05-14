from __future__ import annotations

import asyncio
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.config import AgentConfig, Settings
from app.models import CommandResult, TaskRecord
from app.prompts import build_chat_prompt, build_implementation_prompt, build_qa_review_prompt
from app.safety import is_unsafe_relative_path


DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider:
    """Calls the Gemini API and applies full-file writes from structured JSON."""

    provider_name = "Gemini"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = (settings.ai_api_key or "").strip()
        if not key:
            raise RuntimeError(
                "AI_PROVIDER=gemini requires AI_API_KEY or GEMINI_API_KEY in .env.",
            )
        self._api_key = key

    def _model_for_agent(self, agent: AgentConfig) -> str:
        return (agent.ai_model or self.settings.ai_model or DEFAULT_GEMINI_MODEL).strip()

    async def run_chat(
        self,
        agent: AgentConfig,
        message: str,
        *,
        orchestration_prompt: bool = False,
    ) -> CommandResult:
        return await asyncio.to_thread(self._run_chat_sync, agent, message, orchestration_prompt)

    def _run_chat_sync(
        self,
        agent: AgentConfig,
        message: str,
        orchestration_prompt: bool = False,
    ) -> CommandResult:
        """
        When ``orchestration_prompt`` is True, ``message`` is the full tool-loop prompt and must not
        be wrapped with ``build_chat_prompt`` (that layer tells the model to refuse tools).
        """
        prompt = message if orchestration_prompt else build_chat_prompt(agent, message)
        mime: str | None = "application/json" if orchestration_prompt else None
        temp = 0.05 if orchestration_prompt else 0.7
        raw_text, err = self._generate_content(
            prompt,
            self._model_for_agent(agent),
            response_mime_type=mime,
            temperature=temp,
        )
        if err:
            return CommandResult(1, "", err)
        text = (raw_text or "").strip()
        return CommandResult(0, text or "I'm here. Say that one more way?", "")

    async def run_agent_work(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        *,
        recent_context: str | None = None,
    ) -> CommandResult:
        if agent.repo is None:
            return CommandResult(1, "", f"{self.provider_name} provider requires a repository path.")

        return await asyncio.to_thread(self._run_sync, agent, task, recent_context)

    def _run_sync(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        recent_context: str | None,
    ) -> CommandResult:
        repo = agent.repo.resolve()
        listing = self._git_ls_files(repo)
        user_prompt = self._build_user_prompt(agent, task, listing, recent_context=recent_context)
        raw_text, err = self._generate_content(user_prompt, self._model_for_agent(agent))
        if err:
            return CommandResult(1, "", err)

        payload = self._parse_json_payload(raw_text)
        if payload is None:
            return CommandResult(
                1,
                "",
                f"Could not parse JSON from {self.provider_name}. Raw response (truncated):\n"
                + (raw_text or "")[:4000],
            )

        summary = str(payload.get("summary") or "").strip()
        files_obj = payload.get("files")
        if files_obj is None:
            files_obj = payload.get("changes")
        if files_obj is None and self._file_path_from_item(payload):
            files_obj = [payload]
        if files_obj is None:
            return CommandResult(0, summary or raw_text or "No file changes.", "")

        if not isinstance(files_obj, list):
            return CommandResult(1, "", '"files" must be a JSON array or be omitted.')

        written: list[str] = []
        for item in files_obj:
            if not isinstance(item, dict):
                return CommandResult(1, summary, "Each file entry must be an object with path and content.")
            rel = self._file_path_from_item(item)
            content = item.get("content")
            if not isinstance(rel, str) or not rel.strip():
                return CommandResult(1, summary, f"Invalid file path in {self.provider_name} response.")
            if not isinstance(content, str):
                return CommandResult(1, summary, f"Invalid content for path {rel!r}.")

            target, rel_norm, err = self._safe_repo_target(repo, rel)
            if err:
                return CommandResult(1, summary, err)

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(f"{rel_norm} -> {target}")

        out_parts = [summary] if summary else []
        if written:
            out_parts.append("Files written:")
            out_parts.extend(f"- {p}" for p in written)
        else:
            out_parts.append("No files in response; nothing written.")
        return CommandResult(0, "\n\n".join(out_parts), "")

    async def run_qa_review(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        verification_bundle: str,
        *,
        review_cwd: Path,
    ) -> CommandResult:
        _ = review_cwd
        return await asyncio.to_thread(
            self._run_qa_review_sync,
            agent,
            task,
            verification_bundle,
        )

    def _run_qa_review_sync(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        verification_bundle: str,
    ) -> CommandResult:
        user_prompt = f"""{build_qa_review_prompt(agent, task, verification_bundle)}

Respond with JSON only (no markdown fences), using exactly this shape:
{{
  "summary": "Concise Telegram-ready summary.",
  "findings": ["bullet strings"]
}}
"""
        raw_text, err = self._generate_content(user_prompt, self._model_for_agent(agent))
        if err:
            return CommandResult(1, "", err)
        payload = self._parse_json_payload(raw_text)
        if payload is None:
            return CommandResult(
                1,
                "",
                f"Could not parse JSON from {self.provider_name} QA review. Raw (truncated):\n"
                + (raw_text or "")[:4000],
            )
        summary = str(payload.get("summary") or "").strip()
        findings = payload.get("findings")
        parts: list[str] = []
        if summary:
            parts.append(summary)
        if isinstance(findings, list) and findings:
            parts.append("Findings:")
            parts.extend(f"- {item}" for item in findings if isinstance(item, str))
        body = "\n\n".join(parts) if parts else (raw_text or "Empty QA review response.")
        return CommandResult(0, body, "")

    def _git_ls_files(self, repo: Path) -> str:
        try:
            completed = subprocess.run(
                ["git", "ls-files"],
                cwd=str(repo),
                text=True,
                capture_output=True,
                timeout=120,
                shell=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"(git ls-files failed: {exc})"
        if completed.returncode != 0:
            return f"(git ls-files failed: {completed.stderr})"
        text = completed.stdout.strip()
        max_chars = 50_000
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n... (truncated for prompt size)"
        return text or "(no tracked files)"

    def _build_user_prompt(
        self,
        agent: AgentConfig,
        task: TaskRecord,
        listing: str,
        *,
        recent_context: str | None = None,
    ) -> str:
        base = build_implementation_prompt(agent, task, recent_context=recent_context)
        return f"""{base}

Repository absolute path: {agent.repo}
Tracked files listing (may be truncated):
{listing}

You must respond with JSON only (no markdown fences), using this shape:
{{
  "summary": "Short summary for Telegram, including verification advice.",
  "files": [
    {{ "path": "relative/path/from/repo/root", "content": "full new file text" }}
  ]
}}

Rules for the JSON:
- Include only files you are creating or overwriting with full file contents.
- Paths must be relative to the repository root, use forward slashes, no ".." segments.
- Do not include .env, secrets, node_modules, .venv, .next, __pycache__, .sqlite3, or .db paths.
- If no code changes are needed, use an empty "files" array and explain in "summary".
"""

    def _file_path_from_item(self, item: dict[str, Any]) -> object:
        for key in ("path", "file", "filename", "file_path"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return item.get("path")

    def _safe_repo_target(self, repo: Path, rel: str) -> tuple[Path, str, str | None]:
        rel_norm = rel.replace("\\", "/").strip().lstrip("/")
        rel_path = Path(rel_norm)
        if rel_path.is_absolute() or ".." in rel_path.parts or rel_norm.startswith(".."):
            return repo, rel_norm, f"Unsafe path rejected: {rel!r}"

        if is_unsafe_relative_path(rel_norm):
            return repo, rel_norm, f"Blocked path rejected: {rel_norm}"

        target = (repo / rel_norm).resolve()
        try:
            target.relative_to(repo)
        except ValueError:
            return target, rel_norm, f"Path escapes repository: {rel_norm}"
        return target, rel_norm, None

    def _generate_content(
        self,
        user_text: str,
        model: str,
        *,
        response_mime_type: str | None = "application/json",
        temperature: float = 0.2,
    ) -> tuple[str | None, str]:
        template = self.settings.ai_base_url or GEMINI_GENERATE_URL
        url = template.format(model=model)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        if "key=" in url:
            headers.pop("x-goog-api-key", None)

        generation_config: dict[str, Any] = {"temperature": temperature}
        if response_mime_type:
            generation_config["responseMimeType"] = response_mime_type

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": generation_config,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 — URL is built from configured model and key
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            return None, f"Gemini HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001
            return None, f"Gemini request failed: {exc}"

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            return None, f"Invalid JSON from Gemini: {raw_bytes[:500]!r}"

        if not isinstance(payload, dict):
            return None, "Unexpected Gemini response shape."

        err = self._payload_error_message(payload)
        if err:
            return None, err

        text = self._extract_text_from_response(payload)
        if text is None:
            finish_reason = self._finish_reason(payload)
            if finish_reason:
                return None, f"Gemini returned no text (finishReason={finish_reason})."
            return None, "Gemini returned no text."
        return text, ""

    def _finish_reason(self, payload: dict[str, Any]) -> str | None:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None
        first = candidates[0]
        if not isinstance(first, dict):
            return None
        value = first.get("finishReason")
        return str(value) if value else None

    def _payload_error_message(self, payload: dict[str, Any]) -> str | None:
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            return f"Prompt blocked: {feedback.get('blockReason')}"
        return None

    def _extract_text_from_response(self, payload: dict[str, Any]) -> str | None:
        candidates = payload.get("candidates")
        if not candidates or not isinstance(candidates, list):
            return None
        first = candidates[0]
        if not isinstance(first, dict):
            return None
        content = first.get("content")
        if not isinstance(content, dict):
            return None
        parts = content.get("parts")
        if not parts or not isinstance(parts, list):
            return None
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        return "\n".join(texts) if texts else None

    def _parse_json_payload(self, raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        stripped = raw.strip()
        fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", stripped, re.IGNORECASE)
        if fence:
            stripped = fence.group(1).strip()
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                obj = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
        return obj if isinstance(obj, dict) else None
