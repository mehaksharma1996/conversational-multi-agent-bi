"""Streamlit entry point for the Conversational BI Platform."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from config.settings import get_settings
from src.memory.session_keys import SESSION_ID
from src.storage.session_cleanup import maybe_run_periodic_cleanup, touch_session_heartbeat
from src.ui.chat_panel import render_chat_panel
from src.ui.dashboard import render_dashboard
from src.ui.upload_panel import render_upload_panel
from src.utils.identity import LOCAL_DEV_TENANT_ID, derive_tenant_id


def configure_page() -> None:
    st.set_page_config(
        page_title="Conversational BI Platform",
        page_icon="BI",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def _auth_configured() -> bool:
    """Return True only if a real secrets.toml with an [auth] section exists.

    st.secrets raises StreamlitSecretNotFoundError on any access (including
    "in") when no secrets file exists at all, which is the default local-dev
    and CI case. load_if_toml_exists() is the same primitive Streamlit's own
    st.login() implementation uses to distinguish "no file" (fall back) from
    "file present but malformed" (let it raise, fail loud).
    """
    if not st.secrets.load_if_toml_exists():
        return False
    return "auth" in st.secrets


def _resolve_tenant_id() -> str | None:
    """Gate the app behind login when configured; otherwise use the local tenant.

    Returns None if a login wall was rendered and the caller should stop.
    """
    if not _auth_configured():
        st.sidebar.warning(
            "Running without authentication (local/dev mode). Configure "
            ".streamlit/secrets.toml before hosting this app for multiple users."
        )
        return LOCAL_DEV_TENANT_ID

    if not st.user.is_logged_in:
        st.title("Conversational Business Intelligence Workbench")
        st.write("Sign in to continue.")
        st.button("Log in", on_click=st.login)
        return None

    with st.sidebar:
        st.caption(f"Logged in as {st.user.email}")
        st.button("Log out", on_click=st.logout)
    return derive_tenant_id(st.user.sub)


def main() -> None:
    print("Starting Conversational BI Platform")
    configure_page()
    tenant_id = _resolve_tenant_id()
    if tenant_id is None:
        return

    if SESSION_ID not in st.session_state:
        st.session_state[SESSION_ID] = uuid4().hex
    base_settings = get_settings()
    settings = base_settings.for_session(tenant_id, st.session_state[SESSION_ID])

    if settings.session_dir is not None:
        touch_session_heartbeat(settings.session_dir)
    maybe_run_periodic_cleanup(
        base_settings.app_data_dir,
        base_settings.session_retention_hours,
        interval_seconds=base_settings.session_cleanup_interval_minutes * 60,
        exclude_session_ids={st.session_state[SESSION_ID]},
    )

    if settings.sqlite_encryption_key is None:
        st.sidebar.warning(
            "Data at rest is not encrypted. Set APP_ENCRYPTION_KEY before hosting "
            "this app with sensitive data."
        )

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
