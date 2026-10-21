"""Minimal Streamlit integration test."""

from pathlib import Path

import pytest

# Streamlit is an optional, developer-only environment (requirements-streamlit.lock); these UI
# tests run there and in the CI streamlit-dev job, and skip in the production environment.
pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


def test_initial_application_render_has_no_exceptions() -> None:
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "Conversational Business Intelligence Workbench"

    rerun = app.run(timeout=30)
    assert not rerun.exception
    assert rerun.title[0].value == "Conversational Business Intelligence Workbench"
