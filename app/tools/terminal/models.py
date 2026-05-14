from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalRunResult:
    """Structured outcome for terminal tools (local; swappable for Docker later)."""

    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    def as_tool_message(self, *, max_chars: int = 16000) -> str:
        body = {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        text = json.dumps(body, ensure_ascii=False, indent=2)
        if len(text) <= max_chars:
            return text
        half = (max_chars // 2) - 80
        return json.dumps(
            {
                "exit_code": self.exit_code,
                "timed_out": self.timed_out,
                "stdout": (self.stdout[:half] + "\n…(truncated)"),
                "stderr": (self.stderr[:half] + "\n…(truncated)"),
            },
            ensure_ascii=False,
            indent=2,
        )
