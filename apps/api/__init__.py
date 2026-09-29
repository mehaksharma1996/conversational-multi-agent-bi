"""FastAPI entry point for the versioned service boundary."""

from apps.api.main import app, create_app

__all__ = ["app", "create_app"]
