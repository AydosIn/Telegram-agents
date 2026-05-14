from __future__ import annotations

from app.agents.base import BaseAgent

FILESYSTEM_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
)

TERMINAL_TOOL_NAMES: tuple[str, ...] = (
    "run_command",
    "run_test",
    "run_build",
    "run_lint",
)

GIT_TOOL_NAMES_FULL: tuple[str, ...] = (
    "git_status",
    "git_diff",
    "git_summarize_diff",
    "git_suggest_commit_message",
    "git_create_branch",
    "git_commit",
    "git_checkout",
    "git_push",
    "git_rollback",
)

GIT_TOOL_NAMES_QA: tuple[str, ...] = (
    "git_status",
    "git_diff",
    "git_summarize_diff",
    "git_suggest_commit_message",
)

BROWSER_TOOL_NAMES: tuple[str, ...] = (
    "browser_open_page",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_console_logs",
    "browser_network_errors",
    "browser_responsive_test",
    "browser_close_session",
    "browser_analyze_screenshot",
)

# Path prefixes relative to `Settings.workspace_dir` (default: C:\\Users\\user\\projects).
WORKSPACE_PROJECT_FRONTEND = "events-community-frontend"
WORKSPACE_PROJECT_BACKEND = "events-community-backend"

_AGENT_ROLES: dict[str, str] = {
    "frontend": (
        f"Owns UI under {WORKSPACE_PROJECT_FRONTEND}/. After `npm run dev`, use browser_* tools on localhost to verify UI."
    ),
    "backend": f"Owns server files under {WORKSPACE_PROJECT_BACKEND}/ only.",
    "qa": (
        f"Cross-checks {WORKSPACE_PROJECT_FRONTEND}/ and {WORKSPACE_PROJECT_BACKEND}/. "
        "Use browser_* on allowed localhost URLs for console/network QA."
    ),
}

_FRONTEND_EXE = frozenset({"npm", "npx", "node"})
_BACKEND_EXE = frozenset({"python", "python3", "pip", "pip3", "pytest", "uvicorn"})
_QA_EXE = frozenset({"npm", "npx", "node", "python", "python3", "pytest"})


def base_agent_for(name: str) -> BaseAgent:
    """
    Map a Telegram agent key (frontend|backend|qa) to tool permissions.

    Workspace root is ``Settings.workspace_dir`` (via WORKSPACE_DIR, default `PROJECTS_DIR`).
    Directory rules (relative to that root):
    - frontend -> events-community-frontend/**
    - backend -> events-community-backend/**
    - qa -> both project trees above (not the full host disk).

    Terminal executables are enforced in ``app.tools.terminal.policy``; dangerous patterns
    are blocked globally. The model still only sees tools listed on the agent profile.
    """
    key = name.strip().lower()
    role = _AGENT_ROLES.get(key, "General development assistance.")
    if key == "frontend":
        all_tools = FILESYSTEM_TOOL_NAMES + TERMINAL_TOOL_NAMES + GIT_TOOL_NAMES_FULL + BROWSER_TOOL_NAMES
        return BaseAgent(
            name=key,
            role=role,
            allowed_directories=(WORKSPACE_PROJECT_FRONTEND,),
            tools=all_tools,
            terminal_allowed_executables=_FRONTEND_EXE,
        )
    if key == "backend":
        all_tools = FILESYSTEM_TOOL_NAMES + TERMINAL_TOOL_NAMES + GIT_TOOL_NAMES_FULL
        return BaseAgent(
            name=key,
            role=role,
            allowed_directories=(WORKSPACE_PROJECT_BACKEND,),
            tools=all_tools,
            terminal_allowed_executables=_BACKEND_EXE,
        )
    if key == "qa":
        all_tools = FILESYSTEM_TOOL_NAMES + TERMINAL_TOOL_NAMES + GIT_TOOL_NAMES_QA + BROWSER_TOOL_NAMES
        return BaseAgent(
            name=key,
            role=role,
            allowed_directories=(WORKSPACE_PROJECT_FRONTEND, WORKSPACE_PROJECT_BACKEND),
            tools=all_tools,
            terminal_allowed_executables=_QA_EXE,
        )
    raise KeyError(f"Unknown agent {name!r}")
