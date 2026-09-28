"""Streamlit entry point for the Conversational BI Platform."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from config.settings import get_settings
from src.memory.session_keys import SESSION_ID
from src.storage.session_cleanup import cleanup_stale_sessions
from src.ui.chat_panel import render_chat_panel
from src.ui.dashboard import render_dashboard
from src.ui.upload_panel import render_upload_panel


def configure_page() -> None:
    st.set_page_config(
        page_title="Conversational BI Platform",
        page_icon="BI",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def main() -> None:
    configure_page()
    if SESSION_ID not in st.session_state:
        st.session_state[SESSION_ID] = uuid4().hex
    base_settings = get_settings()
    if not st.session_state.get("session_cleanup_completed"):
        cleanup_stale_sessions(
            base_settings.app_data_dir,
            base_settings.session_retention_hours,
            exclude_session_ids={st.session_state[SESSION_ID]},
        )
        st.session_state["session_cleanup_completed"] = True
    settings = base_settings.for_session(st.session_state[SESSION_ID])

    st.title("Conversational Business Intelligence Workbench")
    st.caption(
        "Upload business data and PDFs, then coordinate deterministic analytics, "
        "read-only SQL, document retrieval, and report workflows."
    )

    with st.sidebar:
        render_upload_panel(settings=settings)

    render_dashboard(settings=settings)
    render_chat_panel(settings=settings)


if __name__ == "__main__":
    main()
