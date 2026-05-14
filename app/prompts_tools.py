from __future__ import annotations

import json
import re
from typing import Any

from app.agents.base import BaseAgent
from app.config import AgentConfig

# --- Tool intent heuristics (initial user message) ------------------------------------------

_FS_KEYWORDS = re.compile(
    r"\b("
    r"create|add|write|new\s|edit|modify|update|change|patch|delete|remove|"
    r"refactor|append|replace|touch|mkdir|move|rename|copy|fix\s+(the\s+)?file|"
    r"implement|hook\s+up|wire\s+up|add\s+component"
    r")\b",
    re.IGNORECASE,
)

_FS_ARTIFACT = re.compile(
    r"\b("
    r"file|files|component|package\.json|tsconfig|\.tsx?|\.jsx?|\.py|\.vue|\.md|"
    r"\.yaml|\.yml|\.json|\.css|\.html"
    r"|folder|directory"
    r"|events-community-frontend|events-community-backend"
    r")\b",
    re.IGNORECASE,
)

_READ_LIST = re.compile(
    r"\b(read|open|show|display|print|cat|view|inspect|list|ls|what'?s?\s+in)\b",
    re.IGNORECASE,
)


def user_message_implies_filesystem_action(message: str) -> bool:
    """
    Heuristic: does this request likely require reading or changing files?

    Biases the orchestrator toward tool JSON instead of conversational {"reply"}.
    """
    text = (message or "").strip()
    if not text:
        return False

    if _FS_KEYWORDS.search(text) and _FS_ARTIFACT.search(text):
        return True

    if _READ_LIST.search(text) and _FS_ARTIFACT.search(text):
        return True

    if re.search(r"[/\\][\w./\\-]+\.(tsx?|jsx?|py|json|md|vue|css|html)\b", text, re.I):
        return True

    if re.search(r"\b[\w./-]+\.(txt|md|json|tsx?|jsx?|py|vue|html|css|yml|yaml)\b", text, re.I):
        return True

    if re.search(r"\b(git|commit|push|branch|checkout|diff|merge|rebase)\b", text, re.I):
        return True

    if re.search(r"\b(localhost|127\.0\.0\.1|screenshot|playwright|browser_console|viewport|responsive)\b", text, re.I):
        return True

    return False


# --- Prompt ------------------------------------------------------------------------------


def _workspace_hint(base: BaseAgent) -> str:
    if not base.allowed_directories:
        return "You may access any path under the workspace root."
    joined = ", ".join(f"`{p}/**`" for p in base.allowed_directories)
    return f"You may only access files under: {joined}"


def _persona_hint(agent_cfg: AgentConfig) -> str:
    if not agent_cfg.persona:
        return ""
    return (
        f"\n(Optional tone for final Telegram reply only — never for tool calls.)\n"
        f"{agent_cfg.persona.strip()}\n"
    )


_TOOL_EXAMPLES = """
## Examples (exact output shapes)

User: create hello.txt in events-community-frontend
Assistant:
{{"tool":"write_file","path":"events-community-frontend/hello.txt","content":"hello"}}

User: read package.json
Assistant:
{{"tool":"read_file","path":"events-community-frontend/package.json"}}

User: list the src folder under backend
Assistant:
{{"tool":"list_files","path":"events-community-backend/src"}}

User: change the title in App.tsx from Foo to Bar (you have read the file already)
Assistant:
{{"tool":"edit_file","path":"events-community-frontend/src/App.tsx","old_text":"title: Foo","new_text":"title: Bar"}}

User: run npm test in the frontend app
Assistant:
{{"tool":"run_command","command":"npm test"}}

User: run the default test suite (backend agent)
Assistant:
{{"tool":"run_test"}}

User: QA — run build checks on backend
Assistant:
{{"tool":"run_build","project":"backend"}}

User: show git status (frontend agent)
Assistant:
{{"tool":"git_status"}}

User: summarize the working tree diff
Assistant:
{{"tool":"git_summarize_diff"}}

User: suggest a commit message from staged files
Assistant:
{{"tool":"git_suggest_commit_message"}}

User: create branch feature/foo
Assistant:
{{"tool":"git_create_branch","name":"feature/foo"}}

User: open http://localhost:5173 and screenshot the hero (frontend)
Assistant:
{{"tool":"browser_open_page","url":"http://localhost:5173/"}}

User: take a full-page PNG after that
Assistant:
{{"tool":"browser_screenshot","path":"landing.png","full_page":true}}
"""


def build_filesystem_tool_chat_prompt(
    agent_cfg: AgentConfig,
    base: BaseAgent,
    conversation: str,
    *,
    enforce_tool_first: bool = False,
    orchestration_reminder: str = "",
) -> str:
    """
    Prompt for tool-loop turns. Must not encourage conversational deferral.

    enforce_tool_first: user request likely needs filesystem work and no tool has run yet.
    orchestration_reminder: injected correction text (retry / invalid JSON).
    """
    tool_list = ", ".join(base.tools)

    hard_rules = """
## HARD RULES — TOOL AGENT (non-negotiable)

- Do NOT roleplay, apologize, or chat about what you "would" do.
- Do NOT explain your intentions or plans in prose before acting.
- Do NOT ask for permission to use the filesystem or pretend you lack access.
- Shell commands are only allowed via `run_command` / `run_test` / `run_build` / `run_lint` tools — never raw shell in prose.
- Browser/UI actions (`browser_open_page`, clicks, screenshots, console/network inspection) are only via `browser_*` tools — never raw Playwright or puppeteer in prose. URLs must be http(s) to **allowlisted hosts** (default: localhost / 127.0.0.1 / ::1).
- After `read_file` for a path succeeds, use that content in the log: do **not** call `read_file` again for the same path unless you need a fresh version after an edit.

When any file must be read, listed, created, edited, a **whitelisted** shell command must run, **git** state must be inspected/updated, or a **browser** check is required, your **entire** response must be **one** JSON object:
either a tool call shape, or (only after tools have run and you are done) `{{"reply":"..."}}`.

If the user asked for a filesystem, git, or **browser/UI validation** action: output the tool JSON **immediately**. No preamble, no markdown.
""".strip()

    phase_hint = ""
    if enforce_tool_first:
        phase_hint = """
## CURRENT PHASE
The user's message **requires a filesystem tool** and you have **not** executed a tool for it yet.
Your next message MUST be a tool JSON object (read/write/edit/list files, run_command / …, git_* tools, or browser_* tools).
Do NOT output {{"reply":...}} until after tool results appear in the conversation below.
""".strip()

    reminder_block = ""
    if orchestration_reminder.strip():
        reminder_block = f"\n## CORRECTION (follow this now)\n{orchestration_reminder.strip()}\n"

    return f"""You are the `{base.name}` agent with **filesystem, sandboxed terminal, git, and Playwright browser** tools. Emit JSON only.

{hard_rules}

{_TOOL_EXAMPLES}

Role (scope only): {base.role}
{_workspace_hint(base)}
{_persona_hint(agent_cfg)}

Tools: {tool_list}
Paths: relative to workspace root, forward slashes.
Optional `cwd` for terminal tools: same style (e.g. `events-community-frontend`). QA must set `project` to `frontend` or `backend` for preset runs.
If a tool result says `PENDING_APPROVAL pending_id=N`, stop and wait for human approval; then repeat the same tool JSON adding `"approval_id": N`. Terminal approvals: `/approve_terminal`. **Git push always** requires `/approve_git_push` before repeating `git_push` with `approval_id`.

Output format — exactly **one** of:
- {{"tool":"read_file","path":"<path>"}}
- {{"tool":"write_file","path":"<path>","content":"<full file text>"}}
- {{"tool":"edit_file","path":"<path>","old_text":"<exact snippet>","new_text":"<replacement>"}}
- {{"tool":"list_files","path":"<directory path>"}}
- {{"tool":"run_command","command":"<single shell-free command string>"}}  optional: "cwd","timeout_sec","approval_id"
- {{"tool":"run_test"}}  optional: "cwd","project" (qa), "timeout_sec"
- {{"tool":"run_build"}}  optional: "cwd","project" (qa), "timeout_sec"
- {{"tool":"run_lint"}}   optional: "cwd","project" (qa), "timeout_sec"
- {{"tool":"git_status"}}  optional: "repo_target" (qa: `frontend`|`backend`)
- {{"tool":"git_diff"}}  optional: "staged":true/false, "stat":true/false, "paths":[...], "repo_target" (qa)
- {{"tool":"git_summarize_diff"}}  optional: "staged", "repo_target" (qa)
- {{"tool":"git_suggest_commit_message"}}  optional: "repo_target" (qa)
- {{"tool":"git_create_branch","name":"<branch>"}}  (not available to qa)
- {{"tool":"git_checkout","branch":"<name>"}}  (not qa)
- {{"tool":"git_commit","message":"<text>"}}  (stage files first, e.g. via `git add` in `run_command` if needed)
- {{"tool":"git_push"}}  optional: "remote","branch","approval_id" (required after Telegram approval)
- {{"tool":"git_rollback"}}  optional: "ref" (or uses task rollback_ref when running in a workflow task)
- {{"tool":"browser_open_page","url":"http://localhost:5173/"}}  (allowlisted hosts only)
- {{"tool":"browser_click","selector":"button[data-testid='cta']"}}
- {{"tool":"browser_type","selector":"#email","text":"a@b.com"}}
- {{"tool":"browser_screenshot","path":"ui.png"}}  optional: "full_page":true|false — saves under `browser-artifacts/<agent>/task-<id>/`
- {{"tool":"browser_console_logs"}}
- {{"tool":"browser_network_errors"}}
- {{"tool":"browser_responsive_test"}}  optional: "basename":"landing"
- {{"tool":"browser_close_session"}} — dispose isolated Playwright context for this task
- {{"tool":"browser_analyze_screenshot","path":"<workspace-relative path>"}} — placeholder for future vision; returns guidance
- {{"reply":"<Telegram message>"}} — **only** when no further tools are needed for the user's request.

Workflow hint (frontend): start dev server via `run_command` / `run_test`, then `browser_open_page` on your Vite/Next port, then `browser_screenshot` + `browser_console_logs` before replying.

`run_command` rules: no `&&`, `|`, `;`, subshells, or newlines. Only whitelisted executables for this agent (frontend: npm/npx/node; backend: python/pytest/pip/uvicorn; qa: npm/npx/node/python/pytest). Dangerous tokens (sudo, docker, curl, rm, …) are rejected. `pip install` / `npm install` / `npx` require human approval via Telegram (`/approve_terminal`).

{phase_hint}
{reminder_block}
## Conversation and tool results (verbatim log)
{conversation.strip()}
""".strip()


# --- Parsing ------------------------------------------------------------------------------


def classify_model_output(raw_text: str) -> tuple[dict[str, Any] | None, str | None, str]:
    """
    Parse model output for the tool loop.

    Returns:
        (tool_payload, reply_text, raw_fallback)
        - tool_payload: use executor when set.
        - reply_text: structured final answer from JSON \"reply\" key only.
        - raw_fallback: original-ish text for unstructured model output or errors.
    """
    text = (raw_text or "").strip()
    if not text:
        return None, None, ""

    stripped = _strip_assistant_noise(text)
    for candidate in (stripped, text):
        parsed = try_parse_json_object(candidate)
        if not isinstance(parsed, dict):
            continue
        tool = parsed.get("tool")
        if isinstance(tool, str) and tool.strip():
            return parsed, None, text
        reply = parsed.get("reply")
        if isinstance(reply, str) and reply.strip():
            return None, reply.strip(), text
        return None, None, text
    return None, None, text


def parse_model_turn(raw_text: str) -> tuple[str | None, dict[str, Any] | None]:
    """Backward-compatible (reply_text, tool_payload)."""
    tool, reply, raw = classify_model_output(raw_text)
    if tool is not None:
        return None, tool
    if reply is not None:
        return reply, None
    return (raw if raw else None), None


def try_parse_json_object(text: str) -> Any | None:
    """Extract and parse a JSON object; tolerates fences and trailing prose."""
    text = (text or "").strip()
    if not text:
        return None

    direct = _loads_if_object(text)
    if direct is not None:
        return direct

    for match in re.finditer(r"```(?:json)?\s*", text, flags=re.IGNORECASE):
        start = match.end()
        end_fence = text.find("```", start)
        chunk = text[start:end_fence] if end_fence != -1 else text[start:]
        blob = _loads_if_object(chunk.strip())
        if blob is not None:
            return blob

    try_positions = [m.start() for m in re.finditer(r"\{", text[:200_000])][:40]
    for start in try_positions:
        candidate = _extract_balanced_json(text, start)
        if candidate is None:
            continue
        blob = _loads_if_object(candidate)
        if blob is not None:
            return blob
    return None


def _loads_if_object(s: str) -> Any | None:
    try:
        val = json.loads(s)
    except json.JSONDecodeError:
        return None
    return val if isinstance(val, dict) else None


def _strip_assistant_noise(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(assistant|model)\s*:\s*", "", t, flags=re.IGNORECASE)
    return t.strip()


def _extract_balanced_json(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escape = False
    string_char: str | None = None
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_char:
                in_string = False
                string_char = None
            continue

        if ch in {'"', "'"}:
            in_string = True
            string_char = ch
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None
