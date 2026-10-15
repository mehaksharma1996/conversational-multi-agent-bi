"""ASGI runtime factory with explicit application configuration loading."""

from __future__ import annotations

from fastapi import FastAPI

from config.runtime import load_runtime_dotenv


def create_runtime_app() -> FastAPI:
    """Load runtime configuration before importing and constructing the application."""
    load_runtime_dotenv()

    from apps.api.main import create_app

    return create_app()
