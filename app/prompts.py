from __future__ import annotations

from app.config import AgentConfig
from app.models import TaskRecord


CHAT_ROLE_DESCRIPTIONS = {
    "frontend": "You think about UI, layout, client-side behavior, product feel, and user experience.",
    "backend": "You think about APIs, data flow, validation, reliability, and server-side tradeoffs.",
    "qa": "You think about bugs, risks, edge cases, clarity, and practical checks.",
}


def build_chat_prompt(agent: AgentConfig, message: str) -> str:
    lines: list[str] = [
        f"You are {agent.name}, a Telegram agent in the Events Community development group.",
        CHAT_ROLE_DESCRIPTIONS.get(agent.name, "You give practical, concise help."),
    ]
    if agent.persona:
        lines.extend(
            [
                "",
                "Persona and tone:",
                agent.persona.strip(),
            ]
        )
    lines.extend(
        [
            "",
            "The user addressed you in the Telegram group:",
            message.strip(),
            "",
            "Reply rules:",
            "- Answer as yourself, not as a generic assistant.",
            "- Be short, natural, and useful: usually 1-4 sentences.",
            "- Stay in your role, but do not pretend you have inspected code or run tools.",
            "- Do not create tasks, mention branches, run commands, or propose that you changed files.",
            "- If the user asks for implementation work, say you can help plan it now and code access comes later.",
            "- Return plain Telegram-ready text only.",
        ]
    )
    return "\n".join(lines).strip()


def build_implementation_prompt(
    agent: AgentConfig,
    task: TaskRecord,
    *,
    recent_context: str | None = None,
) -> str:
    lines: list[str] = [
        f"You are the {agent.name} Telegram subagent for the Events Community project.",
    ]
    if agent.persona:
        lines.extend(
            [
                "",
                "Persona and tone (stay in character; do not contradict safety rules below):",
                agent.persona.strip(),
            ]
        )
    lines.extend(
        [
            "",
            f"Task #{task.id}:",
            task.request,
        ]
    )
    if recent_context:
        lines.extend(
            [
                "",
                "Recent work context for this agent (may omit irrelevant lines):",
                recent_context.strip(),
            ]
        )
    lines.extend(
        [
            "",
            "Rules:",
            "- Work only in this repository.",
            "- Keep edits scoped to the task.",
            "- Do not edit secrets, .env files, node_modules, .venv, .next, __pycache__, databases, or caches.",
            "- Do not commit or push. The Telegram coordinator will verify and commit.",
            "- If the task needs the other repo, explain the exact handoff needed in your final message.",
            "- Finish with a concise summary of changed files and verification advice.",
        ]
    )
    return "\n".join(lines).strip()


def build_qa_review_prompt(agent: AgentConfig, task: TaskRecord, verification_bundle: str) -> str:
    persona = ""
    if agent.persona:
        persona = f"\nPersona: {agent.persona.strip()}\n"

    return f"""
You are the QA Telegram agent for the Events Community project. You only analyze and report; do not modify any files or run destructive commands.
{persona}
Task #{task.id}:
{task.request}

Verification output from frontend and backend checks:
{verification_bundle}

Respond with:
1) Overall pass/fail style assessment (based on the logs, not guesswork).
2) Top risks, regressions, or follow-up checks.
3) Suggested next step for the owner (e.g. handoff to frontend/backend) if needed.

Keep the answer concise and actionable.
""".strip()
