from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BRANCH_RE = re.compile(r"^(?!/|\.|.*\.\.)([a-zA-Z0-9/_.-]+)$")
_REF_RE = re.compile(r"^(?:[0-9a-f]{7,40}|HEAD(?:~\d+)?|[a-zA-Z0-9/_.-]+)$")

_git_cached: tuple[Any, Any, Any] | None = None


def _git_pkg() -> tuple[Any, Any, Any]:
    """Lazy import so GitPython is optional until git tools run, and app can start without it when unused."""
    global _git_cached
    if _git_cached is None:
        try:
            import git
            from git.exc import GitCommandError, InvalidGitRepositoryError
        except ImportError as exc:
            raise ImportError(
                "GitPython is required for git tools. Install with: pip install 'GitPython>=3.1.40'",
            ) from exc
        _git_cached = (git, GitCommandError, InvalidGitRepositoryError)
    return _git_cached


def open_repo(repo_path: Path) -> Any:
    git, _, InvalidGitRepositoryError = _git_pkg()
    p = repo_path.resolve()
    try:
        return git.Repo(p, search_parent_directories=False)
    except InvalidGitRepositoryError as exc:
        raise ValueError(f"Not a git repository: {p}") from exc


def validate_branch_name(name: str) -> str:
    n = (name or "").strip()
    if not n or not _BRANCH_RE.match(n):
        raise ValueError("Invalid branch name (use letters, numbers, /, _, -, .; no spaces or '..').")
    return n


def validate_ref(ref: str) -> str:
    r = (ref or "").strip()
    if not r or not _REF_RE.match(r):
        raise ValueError("Invalid git ref for rollback (branch, HEAD~n, or commit hex).")
    return r


def git_status_porcelain(repo_path: Path) -> str:
    repo = open_repo(repo_path)
    out = repo.git.status(porcelain=True)
    return out if out else "(clean working tree)"


def git_diff_text(
    repo_path: Path,
    *,
    staged: bool = False,
    stat: bool = False,
    paths: list[str] | None = None,
) -> str:
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    args: list[str] = []
    if stat:
        args.append("--stat")
    if staged:
        args.append("--cached")
    tail: list[str] = ["--"]
    if paths:
        tail.extend(paths)
    try:
        if staged or paths:
            diff = repo.git.diff(*args, *tail)
        elif stat:
            diff = repo.git.diff(*args)
        else:
            diff = repo.git.diff()
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc
    return diff if diff else "(no diff)"


def git_create_branch(repo_path: Path, name: str) -> tuple[str, str]:
    """Create and check out a new branch from current HEAD. Returns (previous_head_hex, branch_name)."""
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    b = validate_branch_name(name)
    try:
        old = repo.head.commit.hexsha
        repo.git.checkout("-b", b)
        return old, b
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc


def git_checkout(repo_path: Path, branch: str) -> str:
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    b = validate_branch_name(branch)
    try:
        repo.git.checkout(b)
        return repo.head.commit.hexsha[:12]
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc


def git_commit(repo_path: Path, message: str) -> str:
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    msg = (message or "").strip()
    if not msg:
        raise ValueError("git_commit requires a non-empty message.")
    if not repo.index.diff("HEAD"):
        raise ValueError("Nothing staged to commit. Stage changes first (e.g. terminal git add ...).")
    try:
        commit = repo.index.commit(msg)
        return commit.hexsha[:12]
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc


def git_push(repo_path: Path, remote_name: str, branch: str | None = None) -> str:
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    b = branch or repo.active_branch.name
    validate_branch_name(b)
    try:
        repo.git.push("-u", remote_name, b)
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc
    return f"Pushed {b} to {remote_name}."


def git_reset_hard(repo_path: Path, ref: str) -> str:
    _, GitCommandError, _ = _git_pkg()
    repo = open_repo(repo_path)
    r = validate_ref(ref)
    try:
        repo.git.reset("--hard", r)
        return repo.head.commit.hexsha[:12]
    except GitCommandError as exc:
        raise ValueError(str(exc)) from exc


def git_changed_paths(repo_path: Path) -> list[str]:
    repo = open_repo(repo_path)
    out = repo.git.status(porcelain=True)
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files
