"""Entry point: use the project venv so `python-telegram-bot` (import `telegram`) is available."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_VENV_PYTHON = _ROOT / ".venv" / "Scripts" / "python.exe"


if __name__ == "__main__":
    try:
        from app.telegram_app import main as _run
    except ModuleNotFoundError as exc:
        if exc.name == "telegram" and _VENV_PYTHON.is_file():
            print(
                "Missing dependency: the `telegram` module comes from python-telegram-bot in `.venv`.\n"
                "You ran the wrong Python. Use one of:\n\n"
                f"  {_VENV_PYTHON} {_ROOT / 'main.py'}\n\n"
                "Or activate the venv, then run `python main.py`:\n\n"
                f"  {_ROOT}\\.venv\\Scripts\\Activate.ps1\n"
                "  python main.py\n",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    _run()
