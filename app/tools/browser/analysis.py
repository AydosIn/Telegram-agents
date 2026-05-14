from __future__ import annotations

from pathlib import Path


def placeholder_vision_note(path: Path) -> str:
    """
    Hook for future multimodal / vision analysis.

    Wire an AI provider here when BROWSER_VISION_* env flags are added; keep tools stable.
    """
    return (
        f"Screenshot ready at `{path.as_posix()}`. "
        "Vision analysis is not enabled yet — compare manually or add a baseline under "
        "`browser-artifacts/.../baselines/` for regression diffs."
    )
