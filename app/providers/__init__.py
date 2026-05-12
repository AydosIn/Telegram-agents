from __future__ import annotations

from app.config import Settings
from app.providers.base import AIProvider
from app.providers.codex_cli import CodexCLIProvider
from app.providers.gemini import GeminiProvider
from app.providers.openai_provider import OpenAIProvider


def build_ai_provider_for(settings: Settings, provider_name: str | None = None) -> AIProvider:
    name = (provider_name or settings.ai_provider or "codex_cli").strip().lower()
    if name in ("codex_cli", "codex"):
        return CodexCLIProvider(settings)
    if name in ("gemini", "google_gemini"):
        return GeminiProvider(settings)
    if name == "openai":
        return OpenAIProvider(settings)
    raise RuntimeError(
        f"Unknown AI_PROVIDER={name!r}. Use codex_cli, gemini, or openai.",
    )


def build_ai_provider(settings: Settings) -> AIProvider:
    return build_ai_provider_for(settings, None)


__all__ = [
    "AIProvider",
    "build_ai_provider",
    "build_ai_provider_for",
    "CodexCLIProvider",
    "GeminiProvider",
    "OpenAIProvider",
]
