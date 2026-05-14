from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

from app.agents.base import BaseAgent


@dataclass(frozen=True)
class CommandPolicy:
    """Result of static validation before any subprocess is spawned."""

    ok: bool
    argv: tuple[str, ...]
    reason: str = ""
    needs_approval: bool = False


_FORBIDDEN_SUBSTRINGS = (
    "&&",
    "||",
    ";",
    "\n",
    "\r",
    "`",
    "$(",
    "${",
)

# Tokens anywhere in argv (lowercased) that block execution.
_FORBIDDEN_TOKENS = frozenset(
    {
        "sudo",
        "su",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "mkfs",
        "dd",
        "curl",
        "wget",
        "nc",
        "netcat",
        "ssh",
        "scp",
        "ftp",
        "telnet",
        "chmod",
        "chown",
        "kill",
        "killall",
        "docker",
        "kubectl",
        "powershell",
        "invoke-expression",
    }
)


def _normalize_exe(token: str) -> str:
    base = os.path.basename(token.strip().strip('"').strip("'"))
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower()


def _argv_needs_approval(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    low = [a.lower() for a in argv]
    if "pip" in low and "install" in low:
        return True
    if "npm" in low and "install" in low:
        return True
    if "npx" in low:
        return True
    if "yarn" in low and any(x in low for x in ("add", "install")):
        return True
    if "pnpm" in low and any(x in low for x in ("add", "install")):
        return True
    return False


def _npm_allowed_for_frontend(argv: tuple[str, ...]) -> bool:
    if len(argv) < 2:
        return False
    sub = argv[1].lower()
    allowed_sub = frozenset(
        {"run", "test", "ci", "exec", "ls", "outdated", "audit", "install", "init", "version", "whoami"},
    )
    return sub in allowed_sub


def _npm_allowed_for_qa(argv: tuple[str, ...]) -> bool:
    if len(argv) < 2:
        return False
    sub = argv[1].lower()
    if sub == "test":
        return True
    if sub == "run" and len(argv) >= 3:
        return argv[2].lower() in {"test", "lint", "build", "ci"}
    return False


def _pip_allowed(argv: tuple[str, ...], agent_name: str) -> bool:
    if agent_name != "backend":
        return False
    if len(argv) < 2:
        return False
    sub = argv[1].lower()
    return sub in {"install", "freeze", "list", "show", "check"}


def _validate_parsed_argv(argv: tuple[str, ...], agent: BaseAgent) -> CommandPolicy:
    if not argv:
        return CommandPolicy(False, (), "Empty argv.")

    for arg in argv:
        base = _normalize_exe(arg)
        if base in _FORBIDDEN_TOKENS:
            return CommandPolicy(False, argv, f"Forbidden token: {arg!r}")
        if base == "rm":
            return CommandPolicy(False, argv, "rm is not allowed; delete files with the edit_file tool.")

    exe = _normalize_exe(argv[0])
    allowed = {a.lower() for a in agent.terminal_allowed_executables}
    if exe not in allowed:
        return CommandPolicy(
            False,
            argv,
            f"Executable {exe!r} is not allowed for agent {agent.name}. Allowed: {sorted(allowed)}",
        )

    if exe == "npm":
        if agent.name == "frontend" and not _npm_allowed_for_frontend(argv):
            return CommandPolicy(False, argv, "That npm invocation is not allowed for the frontend agent.")
        if agent.name == "qa" and not _npm_allowed_for_qa(argv):
            return CommandPolicy(False, argv, "QA may only use npm for test/lint/build-style runs.")
        if agent.name == "backend":
            return CommandPolicy(False, argv, "npm is not allowed for the backend agent.")

    if exe in {"pip", "pip3"}:
        if not _pip_allowed(argv, agent.name):
            return CommandPolicy(False, argv, "pip is restricted for this agent or subcommand.")

    if exe in {"python", "python3"}:
        if agent.name == "frontend":
            return CommandPolicy(False, argv, "python is not allowed for the frontend agent (use npm).")
        if "-c" in argv:
            return CommandPolicy(False, argv, "python -c is not allowed.")
        if "-m" in argv:
            try:
                mi = argv.index("-m")
                mod = argv[mi + 1].lower() if mi + 1 < len(argv) else ""
            except IndexError:
                return CommandPolicy(False, argv, "python -m requires a module name.")
            allowed_mods = frozenset({"pytest", "compileall", "ruff", "pip"})
            if mod not in allowed_mods:
                return CommandPolicy(
                    False,
                    argv,
                    f"python -m {mod} is not allowed (allowed modules: {sorted(allowed_mods)}).",
                )

    if exe == "uvicorn" and agent.name != "backend":
        return CommandPolicy(False, argv, "uvicorn is only allowed for the backend agent.")

    needs = _argv_needs_approval(argv)
    return CommandPolicy(True, argv, "", needs_approval=needs)


def validate_terminal_command(command: str, agent: BaseAgent) -> CommandPolicy:
    raw = (command or "").strip()
    if not raw:
        return CommandPolicy(False, (), "Empty command.")

    for frag in _FORBIDDEN_SUBSTRINGS:
        if frag in raw:
            return CommandPolicy(False, (), f"Shell chaining or substitution is not allowed ({frag!r}).")

    posix_mode = os.name != "nt"
    try:
        tokens = shlex.split(raw, posix=posix_mode)
    except ValueError as exc:
        return CommandPolicy(False, (), f"Could not parse command: {exc}")

    if not tokens:
        return CommandPolicy(False, (), "Empty command after parsing.")

    return _validate_parsed_argv(tuple(tokens), agent)


def validate_terminal_argv(argv: tuple[str, ...], agent: BaseAgent) -> CommandPolicy:
    """Validate an already-parsed argv (used for preset tools)."""
    joined = " ".join(shlex.quote(a) for a in argv)
    return validate_terminal_command(joined, agent)


def resolve_preset_tool(
    tool: str,
    agent_name: str,
    *,
    project: str | None = None,
) -> tuple[str, ...] | None:
    """Map run_test / run_build / run_lint to argv tuples."""
    name = agent_name.lower()
    proj = (project or "").strip().lower()
    if name == "qa" and proj not in {"frontend", "backend", "fe", "be"}:
        return None
    if proj in ("fe", "frontend"):
        proj_key = "frontend"
    elif proj in ("be", "backend"):
        proj_key = "backend"
    else:
        proj_key = None

    if name == "frontend":
        presets = {
            "run_test": ("npm", "test"),
            "run_build": ("npm", "run", "build"),
            "run_lint": ("npm", "run", "lint"),
        }
        return presets.get(tool)

    if name == "backend":
        presets = {
            "run_test": ("pytest",),
            "run_build": ("python", "-m", "compileall", "."),
            "run_lint": ("python", "-m", "ruff", "check", "."),
        }
        return presets.get(tool)

    if name == "qa":
        if proj_key == "frontend":
            mapping = {
                "run_test": ("npm", "test"),
                "run_build": ("npm", "run", "build"),
                "run_lint": ("npm", "run", "lint"),
            }
        elif proj_key == "backend":
            mapping = {
                "run_test": ("pytest",),
                "run_build": ("python", "-m", "compileall", "."),
                "run_lint": ("python", "-m", "ruff", "check", "."),
            }
        else:
            return None
        return mapping.get(tool)
    return None
