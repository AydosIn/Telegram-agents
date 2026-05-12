"""Verify config and AI provider wiring without starting Telegram polling."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    from app.config import load_settings
    from app.providers import build_ai_provider, build_ai_provider_for
    from app.telegram_app import TelegramCoordinator

    settings = load_settings()
    provider = build_ai_provider(settings)
    coordinator = TelegramCoordinator(settings)
    applications = coordinator.build_applications()
    print("smoke_check: OK")
    print(f"  TELEGRAM_GROUP_CHAT_ID={settings.group_chat_id}")
    print(f"  AI_PROVIDER={settings.ai_provider!r}")
    print(f"  provider_class={type(provider).__name__}")
    print(f"  database_path={settings.database_path}")
    print(f"  telegram_applications={len(applications)}")
    if settings.ai_provider.lower() in ("codex_cli", "codex"):
        print(f"  codex_exec_model_global={settings.effective_codex_model!r}")
    for name, agent in settings.agents.items():
        eff = (agent.ai_provider or settings.ai_provider).strip().lower()
        prov = build_ai_provider_for(settings, eff)
        line = f"  agent[{name}] provider_effective={eff!r} provider_instance={type(prov).__name__}"
        if eff in ("codex_cli", "codex"):
            line += f" codex_model={settings.effective_codex_model_for_agent(agent)!r}"
        if eff in ("gemini", "google_gemini"):
            from app.providers.gemini import DEFAULT_GEMINI_MODEL

            line += f" gemini_model={(agent.ai_model or settings.ai_model or DEFAULT_GEMINI_MODEL)!r}"
        print(line)
    print(f"  prompt_context_max_tasks={settings.prompt_context_max_tasks}")
    print(f"  qa_ai_review={settings.qa_ai_review}")


if __name__ == "__main__":
    main()
