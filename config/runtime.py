"""Explicit dotenv loading for supported application runtime entry points."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DOTENV_PATH = (REPOSITORY_ROOT / ".env").resolve()
_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})


def load_runtime_dotenv() -> bool:
    """Load the repository dotenv file without overriding the process environment."""
    if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() in _DISABLED_VALUES:
        return False
    return load_dotenv(dotenv_path=RUNTIME_DOTENV_PATH, override=False)
