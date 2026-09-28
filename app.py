"""Streamlit entry point for the Conversational BI Platform."""

from __future__ import annotations

import streamlit as st

from config.settings import get_settings
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
    settings = get_settings()

    st.title("Conversational Multi-Agent Business Intelligence Platform")
    st.caption(
        "Upload business data and PDFs, then explore analytics, charts, reports, SQL, RAG, and follow-up questions."
    )

    with st.sidebar:
        render_upload_panel(settings=settings)

    render_dashboard(settings=settings)
    render_chat_panel(settings=settings)


if __name__ == "__main__":
    main()
