from __future__ import annotations

import re


def summarize_diff_text(diff_text: str, *, max_files: int = 25, max_line_chars: int = 12_000) -> str:
    """
    Produce a short human-readable summary of a unified diff or `--stat` output.
    Not a substitute for reading the patch; caps size for Telegram/context limits.
    """
    raw = (diff_text or "").strip()
    if not raw or raw == "(no diff)":
        return "(no changes to summarize)"

    if len(raw) > max_line_chars:
        raw = raw[: max_line_chars - 40].rstrip() + "\n…[diff truncated for summary]"

    files: list[str] = []
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            m = re.search(r"b/(.+)$", line)
            if m:
                files.append(m.group(1).strip())
        elif line.startswith("+++ b/"):
            files.append(line[6:].strip())

    if not files:
        # Likely `--stat` output
        stat_lines = [ln for ln in raw.splitlines() if "|" in ln and ln.strip().endswith("bytes")]
        if stat_lines:
            head = "\n".join(stat_lines[:max_files])
            return "Change stats (sample):\n" + head
        return "Summary: non-standard diff format; first lines:\n" + "\n".join(raw.splitlines()[:15])

    seen: list[str] = []
    for f in files:
        if f not in seen:
            seen.append(f)
        if len(seen) >= max_files:
            break
    extra = len(files) - len(seen)
    tail = f"\n… and {extra} more paths" if extra > 0 else ""
    return f"Files touched ({len(seen)} shown):\n" + "\n".join(f"- {p}" for p in seen) + tail
