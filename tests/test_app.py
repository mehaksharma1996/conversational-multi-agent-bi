"""Minimal Streamlit integration test."""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_initial_application_render_has_no_exceptions() -> None:
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "Conversational Business Intelligence Workbench"
