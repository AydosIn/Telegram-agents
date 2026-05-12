from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskRecord:
    id: int
    agent: str
    request: str
    status: str
    branch: str | None
    commit_hash: str | None
    summary: str | None
    verification: str | None
    created_by: str | None
    created_at: str
    updated_at: str
    pushed_at: str | None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined_output(self) -> str:
        output = "\n".join(part for part in (self.stdout, self.stderr) if part)
        return output.strip()
