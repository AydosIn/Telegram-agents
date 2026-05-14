from __future__ import annotations

from app.config import AgentConfig, Settings
from app.providers import build_ai_provider_for


def _provider_key(settings: Settings, agent: AgentConfig) -> str:
    raw = (agent.ai_provider or settings.ai_provider).strip().lower()
    if raw in ("codex", "codex_cli"):
        return "codex_cli"
    if raw in ("gemini", "google_gemini"):
        return "gemini"
    if raw == "openai":
        return "openai"
    return raw


async def suggest_commit_message_ai(
    settings: Settings,
    agent: AgentConfig,
    *,
    diff_excerpt: str,
) -> str:
    """
    Single-line conventional-commit style message from staged/unstaged diff text.
    Falls back to a short heuristic if the provider fails.
    """
    excerpt = (diff_excerpt or "").strip()
    if not excerpt or excerpt == "(no diff)":
        return "chore: update files"

    prompt = (
        "You write git commit messages. Reply with ONE LINE only: a concise conventional-commit "
        "subject (max 72 chars), no body, no quotes.\n\nDiff / stat:\n"
        + excerpt[:8000]
    )
    try:
        provider = build_ai_provider_for(settings, _provider_key(settings, agent))
        result = await provider.run_chat(agent, prompt)
        if result.ok:
            line = (result.combined_output or "").strip().splitlines()[0].strip()
            if line:
                return line[:120]
    except Exception:
        pass

    first = excerpt.splitlines()[0][:72] if excerpt else ""
    return (first or "chore: update")[:120]
