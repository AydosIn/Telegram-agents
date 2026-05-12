from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.config import AgentConfig, Settings
from app.providers.gemini import GeminiProvider


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(GeminiProvider):
    """Calls the OpenAI Chat Completions API using the Gemini provider result format."""

    provider_name = "OpenAI"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = (settings.ai_api_key or "").strip()
        if not key:
            raise RuntimeError("AI_PROVIDER=openai requires AI_API_KEY in .env.")
        if not (settings.ai_model or "").strip():
            raise RuntimeError("AI_PROVIDER=openai requires AI_MODEL in .env.")
        self._api_key = key

    def _model_for_agent(self, agent: AgentConfig) -> str:
        _ = agent
        model = (self.settings.ai_model or "").strip()
        if not model:
            raise RuntimeError("AI_PROVIDER=openai requires AI_MODEL in .env.")
        return model

    def _chat_completions_url(self) -> str:
        base_url = (self.settings.ai_base_url or DEFAULT_OPENAI_BASE_URL).strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def _generate_content(
        self,
        user_text: str,
        model: str,
        *,
        response_mime_type: str | None = "application/json",
        temperature: float = 0.2,
    ) -> tuple[str | None, str]:
        url = self._chat_completions_url()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "python-requests/2.31.0",
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a careful coding assistant. Follow the user's requested response format exactly.",
                },
                {"role": "user", "content": user_text},
            ],
            "temperature": temperature,
        }
        if response_mime_type == "application/json":
            body["response_format"] = {"type": "json_object"}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - URL is configured by the application.
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
            return None, f"OpenAI HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001
            return None, f"OpenAI request failed: {exc}"

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            return None, f"Invalid JSON from OpenAI: {raw_bytes[:500]!r}"

        if not isinstance(payload, dict):
            return None, "Unexpected OpenAI response shape."

        err = self._payload_error_message(payload)
        if err:
            return None, err

        text = self._extract_text_from_response(payload)
        if text is None:
            finish_reason = self._finish_reason(payload)
            if finish_reason:
                return None, f"OpenAI returned no text (finish_reason={finish_reason})."
            return None, "OpenAI returned no text."
        return text, ""

    def _finish_reason(self, payload: dict[str, Any]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        value = first.get("finish_reason")
        return str(value) if value else None

    def _payload_error_message(self, payload: dict[str, Any]) -> str | None:
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return None

    def _extract_text_from_response(self, payload: dict[str, Any]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        texts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        return "\n".join(texts) if texts else None
