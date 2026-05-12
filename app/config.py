from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECTS_DIR = Path(r"C:\Users\user\projects")
DEFAULT_FRONTEND_REPO = PROJECTS_DIR / "events-community-frontend"
DEFAULT_BACKEND_REPO = PROJECTS_DIR / "events-community-backend"


def _read_persona_inline_or_file(inline_key: str, file_key: str) -> str | None:
    path_raw = (os.getenv(file_key) or "").strip()
    if path_raw:
        path = Path(path_raw)
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    inline = (os.getenv(inline_key) or "").strip()
    return inline or None


def _optional_agent_ai_model(prefix: str) -> str | None:
    v = (os.getenv(f"{prefix}_AI_MODEL") or "").strip()
    return v or None


def _optional_agent_ai_provider(prefix: str) -> str | None:
    v = (os.getenv(f"{prefix}_AI_PROVIDER") or "").strip()
    return v or None


@dataclass(frozen=True)
class AgentConfig:
    name: str
    token: str
    repo: Path | None
    can_implement: bool
    verify_commands: tuple[list[str], ...]
    persona: str | None = None
    ai_model: str | None = None
    ai_provider: str | None = None


@dataclass(frozen=True)
class Settings:
    """group_chat_id is canonical for send_message; group_chat_id_aliases accepts incoming updates."""

    group_chat_id: int
    group_chat_id_aliases: frozenset[int]
    database_path: Path
    git_remote: str
    codex_model: str | None
    ai_provider: str
    ai_api_key: str | None
    ai_model: str | None
    ai_base_url: str | None
    agents: dict[str, AgentConfig]
    prompt_context_max_tasks: int
    prompt_context_max_chars: int
    qa_ai_review: bool

    @property
    def effective_codex_model(self) -> str | None:
        """CODEX_MODEL wins; otherwise AI_MODEL (PRD-neutral name) for `codex exec --model`."""
        return self.codex_model or self.ai_model

    def effective_codex_model_for_agent(self, agent: AgentConfig) -> str | None:
        """Global CODEX_MODEL wins; else per-agent AI_MODEL; else global AI_MODEL."""
        if self.codex_model:
            return self.codex_model
        if agent.ai_model:
            return agent.ai_model
        return self.ai_model


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def read_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required. Add it to .env or your environment.")
    return value


def read_path_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    return Path(value)


def _group_chat_aliases(raw: int) -> frozenset[int]:
    """Telegram supergroups often use -100xxxxxxxxxx while some UIs show a shorter id; accept both."""
    out: set[int] = {raw}
    if raw >= 0:
        return frozenset(out)
    st = str(raw)
    if st.startswith("-100"):
        digits = str(abs(raw))
        if digits.startswith("100") and len(digits) > 3:
            tail = digits[3:].lstrip("0") or "0"
            try:
                out.add(-int(tail))
            except ValueError:
                pass
    elif abs(raw) >= 10**9:
        try:
            out.add(int(f"-100{abs(raw)}"))
        except ValueError:
            pass
    return frozenset(out)


def _canonical_group_chat_id(raw: int) -> int:
    """Prefer -100… form for Bot API sends when that alias exists."""
    for candidate in _group_chat_aliases(raw):
        if str(candidate).startswith("-100"):
            return candidate
    return raw


def load_settings() -> Settings:
    app_root = Path(__file__).resolve().parents[1]
    load_dotenv(app_root / ".env")

    database_path = Path(os.getenv("DATABASE_PATH", "agents.sqlite3"))
    if not database_path.is_absolute():
        database_path = app_root / database_path

    frontend_repo = read_path_env("FRONTEND_REPO", DEFAULT_FRONTEND_REPO)
    backend_repo = read_path_env("BACKEND_REPO", DEFAULT_BACKEND_REPO)

    ai_provider = (os.getenv("AI_PROVIDER") or "codex_cli").strip()
    ai_api_key = (os.getenv("AI_API_KEY") or "").strip() or (os.getenv("GEMINI_API_KEY") or "").strip() or None
    ai_model = (os.getenv("AI_MODEL") or "").strip() or None
    ai_base_url = (os.getenv("AI_BASE_URL") or "").strip() or None

    def _effective_provider_for_agent(agent_provider: str | None) -> str:
        return (agent_provider or ai_provider).strip().lower()

    _raw_group = int(read_required_env("TELEGRAM_GROUP_CHAT_ID"))
    _group_aliases = _group_chat_aliases(_raw_group)
    _canonical_group = _canonical_group_chat_id(_raw_group)

    prompt_context_max_tasks = int(os.getenv("PROMPT_CONTEXT_MAX_TASKS", "5"))
    prompt_context_max_chars = int(os.getenv("PROMPT_CONTEXT_MAX_CHARS", "6000"))
    qa_ai_raw = (os.getenv("QA_AI_REVIEW") or "").strip().lower()
    qa_ai_review = qa_ai_raw in ("1", "true", "yes", "on")

    agents_map: dict[str, AgentConfig] = {
        "frontend": AgentConfig(
            name="frontend",
            token=read_required_env("FRONTEND_BOT_TOKEN"),
            repo=frontend_repo,
            can_implement=True,
            verify_commands=(["npm", "run", "build"],),
            persona=_read_persona_inline_or_file("FRONTEND_PERSONA", "FRONTEND_PERSONA_FILE"),
            ai_model=_optional_agent_ai_model("FRONTEND"),
            ai_provider=_optional_agent_ai_provider("FRONTEND"),
        ),
        "backend": AgentConfig(
            name="backend",
            token=read_required_env("BACKEND_BOT_TOKEN"),
            repo=backend_repo,
            can_implement=True,
            verify_commands=(["python", "-m", "compileall", "."],),
            persona=_read_persona_inline_or_file("BACKEND_PERSONA", "BACKEND_PERSONA_FILE"),
            ai_model=_optional_agent_ai_model("BACKEND"),
            ai_provider=_optional_agent_ai_provider("BACKEND"),
        ),
        "qa": AgentConfig(
            name="qa",
            token=read_required_env("QA_BOT_TOKEN"),
            repo=None,
            can_implement=False,
            verify_commands=(),
            persona=_read_persona_inline_or_file("QA_PERSONA", "QA_PERSONA_FILE"),
            ai_model=_optional_agent_ai_model("QA"),
            ai_provider=_optional_agent_ai_provider("QA"),
        ),
    }

    _needs_gemini = _effective_provider_for_agent(None) in ("gemini", "google_gemini") or any(
        _effective_provider_for_agent(a.ai_provider) in ("gemini", "google_gemini")
        for a in agents_map.values()
    )
    _needs_openai = _effective_provider_for_agent(None) == "openai" or any(
        _effective_provider_for_agent(a.ai_provider) == "openai"
        for a in agents_map.values()
    )
    if _needs_gemini and not ai_api_key:
        raise RuntimeError(
            "Gemini is used (AI_PROVIDER or *_AI_PROVIDER=gemini) but AI_API_KEY or GEMINI_API_KEY is missing.",
        )
    if _needs_openai and not (os.getenv("AI_API_KEY") or "").strip():
        raise RuntimeError("OpenAI is used (AI_PROVIDER or *_AI_PROVIDER=openai) but AI_API_KEY is missing.")
    if _needs_openai and not ai_model:
        raise RuntimeError("OpenAI is used (AI_PROVIDER or *_AI_PROVIDER=openai) but AI_MODEL is missing.")

    _settings = Settings(
        group_chat_id=_canonical_group,
        group_chat_id_aliases=_group_aliases,
        database_path=database_path,
        git_remote=os.getenv("GIT_REMOTE", "origin"),
        codex_model=os.getenv("CODEX_MODEL") or None,
        ai_provider=ai_provider,
        ai_api_key=ai_api_key,
        ai_model=ai_model,
        ai_base_url=ai_base_url,
        agents=agents_map,
        prompt_context_max_tasks=prompt_context_max_tasks,
        prompt_context_max_chars=prompt_context_max_chars,
        qa_ai_review=qa_ai_review,
    )
    return _settings
