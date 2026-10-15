"""FastAPI application factory for the versioned service boundary."""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    """Retain a lazy ``create_app`` export without constructing or importing the runtime app."""
    if name != "create_app":
        raise AttributeError(name)
    from apps.api.main import create_app

    return create_app
