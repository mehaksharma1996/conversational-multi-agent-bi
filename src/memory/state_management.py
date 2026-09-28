"""State lifecycle helpers shared by the Streamlit UI and tests."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from src.memory.session_keys import (
    ANALYSIS_CACHE,
    ANOMALY_CONTAMINATION,
    ANOMALY_FEATURES,
    CHAT_MESSAGES,
    DOCUMENT_ERROR,
    DOCUMENT_RETRIEVER,
    DOCUMENT_SIGNATURE,
    DOCUMENT_STATUS,
    PDF_BYTES,
    PDF_SIGNATURE,
    PROFILE_CACHE,
    SESSION_MEMORY,
    STORED_TABLE,
    STORED_TABLE_SIGNATURE,
    TABULAR_SIGNATURE,
    UPLOAD_ERROR,
    UPLOADED_TABLE,
)

TABLE_STATE_KEYS = {
    UPLOADED_TABLE,
    TABULAR_SIGNATURE,
    UPLOAD_ERROR,
    STORED_TABLE,
    STORED_TABLE_SIGNATURE,
    SESSION_MEMORY,
    ANOMALY_FEATURES,
    ANOMALY_CONTAMINATION,
    PDF_BYTES,
    PDF_SIGNATURE,
    PROFILE_CACHE,
    ANALYSIS_CACHE,
}

DOCUMENT_STATE_KEYS = {
    DOCUMENT_RETRIEVER,
    DOCUMENT_STATUS,
    DOCUMENT_ERROR,
    DOCUMENT_SIGNATURE,
    SESSION_MEMORY,
    PDF_BYTES,
    PDF_SIGNATURE,
    ANALYSIS_CACHE,
}

CONVERSATION_STATE_KEYS = {CHAT_MESSAGES, SESSION_MEMORY}
TABLE_WIDGET_PREFIXES = (
    "schema_",
    "anomaly_features_",
    "anomaly_contamination_",
)
UPLOAD_WIDGET_PREFIXES = ("tabular_uploader_", "pdf_uploader_")


def clear_keys(state: MutableMapping[str, Any], keys: set[str]) -> None:
    for key in keys:
        state.pop(key, None)


def clear_table_state(state: MutableMapping[str, Any]) -> None:
    """Remove state derived from the current tabular upload."""
    clear_keys(state, TABLE_STATE_KEYS | CONVERSATION_STATE_KEYS)
    for key in list(state):
        if key.startswith(TABLE_WIDGET_PREFIXES):
            state.pop(key, None)


def clear_document_state(state: MutableMapping[str, Any]) -> None:
    """Remove state derived from the current document upload."""
    clear_keys(state, DOCUMENT_STATE_KEYS | CONVERSATION_STATE_KEYS)


def clear_all_workflow_state(state: MutableMapping[str, Any]) -> None:
    """Remove all user-provided and derived workflow state."""
    clear_keys(
        state,
        TABLE_STATE_KEYS | DOCUMENT_STATE_KEYS | CONVERSATION_STATE_KEYS,
    )
    for key in list(state):
        if key.startswith(TABLE_WIDGET_PREFIXES + UPLOAD_WIDGET_PREFIXES):
            state.pop(key, None)
