"""Quick examples: tool executor + agent permissions against configured WORKSPACE_DIR.

Run from repo root (venv Python recommended):

  .venv\\Scripts\\python scripts\\test_workspace_permissions.py

Shows:
- frontend read allowed under events-community-frontend/
- frontend read denied under events-community-backend/
- QA read allowed in both trees
- '..' segments rejected before any filesystem access
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _print(title: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "FAIL"
    clipped = detail.strip()
    if len(clipped) > 420:
        clipped = clipped[:420] + "…"
    print(f"[{status}] {title}")
    print(f"       {clipped}")


async def _run() -> None:
    sys.path.insert(0, str(_ROOT))

    from app.agents.factory import (
        WORKSPACE_PROJECT_BACKEND,
        WORKSPACE_PROJECT_FRONTEND,
        base_agent_for,
    )
    from app.config import load_settings
    from app.safety import is_unsafe_relative_path
    from app.tools.executor import ToolExecutor
    from app.tools.paths import PathValidationError, normalize_workspace_relative

    settings = load_settings()
    workspace = settings.workspace_dir.resolve()

    def first_file_under(repo: Path, *, max_depth: int = 3) -> Path | None:
        if not repo.is_dir():
            return None
        frontier: list[tuple[Path, int]] = [(repo, 0)]
        while frontier:
            current, depth = frontier.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for p in entries:
                if p.name == ".git":
                    continue
                if p.is_file():
                    try:
                        rel_posix = str(p.relative_to(workspace)).replace("\\", "/")
                    except ValueError:
                        continue
                    if not is_unsafe_relative_path(rel_posix):
                        return p
                if p.is_dir() and depth + 1 <= max_depth:
                    frontier.append((p, depth + 1))
        return None

    print(f"Workspace root: {workspace}\n")

    executor = ToolExecutor(workspace)
    frontend = base_agent_for("frontend")
    qa = base_agent_for("qa")

    fe_root = workspace / WORKSPACE_PROJECT_FRONTEND
    be_root = workspace / WORKSPACE_PROJECT_BACKEND
    sample_fe = fe_root / "package.json"
    if not sample_fe.is_file():
        sample_fe = fe_root / "README.md"
    if not sample_fe.is_file():
        found = first_file_under(fe_root)
        sample_fe = found if found else sample_fe

    sample_be = be_root / "pyproject.toml"
    if not sample_be.is_file():
        sample_be = be_root / "README.md"
    if not sample_be.is_file():
        sample_be = be_root / "package.json"
    if not sample_be.is_file():
        found_be = first_file_under(be_root)
        sample_be = found_be if found_be else sample_be

    fe_rel = str(sample_fe.relative_to(workspace)).replace("\\", "/") if sample_fe.is_file() else ""
    be_rel = str(sample_be.relative_to(workspace)).replace("\\", "/") if sample_be.is_file() else ""

    if fe_rel:
        r_ok = await executor.execute(frontend, {"tool": "read_file", "path": fe_rel})
        _print("Frontend may read its project", r_ok.ok, r_ok.message.split("\n", 1)[0])
    else:
        _print(
            "Frontend read example skipped",
            True,
            f"No file found under {WORKSPACE_PROJECT_FRONTEND}/.",
        )

    if be_rel and fe_rel:
        r_deny = await executor.execute(frontend, {"tool": "read_file", "path": be_rel})
        expect_deny = not r_deny.ok
        _print("Frontend denied backend path", expect_deny, r_deny.message)
    else:
        _print(
            "Frontend vs backend denial skipped",
            True,
            "Need both project trees with at least one readable file each.",
        )

    if fe_rel and be_rel:
        r_qa_fe = await executor.execute(qa, {"tool": "read_file", "path": fe_rel})
        r_qa_be = await executor.execute(qa, {"tool": "read_file", "path": be_rel})
        _print("QA may read both projects", r_qa_fe.ok and r_qa_be.ok, f"FE ok={r_qa_fe.ok}, BE ok={r_qa_be.ok}")
    else:
        _print("QA dual read skipped", True, "Missing one of the project paths or files.")

    try:
        normalize_workspace_relative(
            f"{WORKSPACE_PROJECT_FRONTEND}/../{WORKSPACE_PROJECT_BACKEND}/demo.py",
        )
        traversal_ok = False
        detail = "expected PathValidationError"
    except PathValidationError as exc:
        traversal_ok = True
        detail = str(exc)
    _print("Traversal '..' rejected at normalize", traversal_ok, detail)

    print(
        "\nTelegram: set WORKSPACE_DIR to your projects root, FILE_TOOLS_ENABLED=true, "
        "then use /chat or @bot - same executor and permissions apply.",
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
