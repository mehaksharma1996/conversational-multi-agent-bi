"""Tests for upload-derived session state cleanup."""

from src.memory.session_keys import (
    CHAT_MESSAGES,
    DOCUMENT_RETRIEVER,
    DOCUMENT_SIGNATURE,
    SESSION_ID,
    SESSION_MEMORY,
    STORED_TABLE,
    TABULAR_SIGNATURE,
    UPLOADED_TABLE,
)
from src.memory.state_management import (
    clear_all_workflow_state,
    clear_document_state,
    clear_table_state,
)


def _state() -> dict:
    return {
        SESSION_ID: "a" * 32,
        UPLOADED_TABLE: object(),
        TABULAR_SIGNATURE: "table-hash",
        STORED_TABLE: object(),
        DOCUMENT_RETRIEVER: object(),
        DOCUMENT_SIGNATURE: (("policy.pdf", "pdf-hash"),),
        CHAT_MESSAGES: ["old message"],
        SESSION_MEMORY: object(),
        "schema_table-hash_amount": "amount",
        "tabular_uploader_0": object(),
        "pdf_uploader_0": [object()],
    }


def test_clear_table_state_preserves_documents_and_session_identity() -> None:
    state = _state()
    clear_table_state(state)

    assert UPLOADED_TABLE not in state
    assert STORED_TABLE not in state
    assert CHAT_MESSAGES not in state
    assert DOCUMENT_RETRIEVER in state
    assert SESSION_ID in state


def test_clear_document_state_preserves_table() -> None:
    state = _state()
    clear_document_state(state)

    assert DOCUMENT_RETRIEVER not in state
    assert DOCUMENT_SIGNATURE not in state
    assert CHAT_MESSAGES not in state
    assert UPLOADED_TABLE in state


def test_clear_all_workflow_state_preserves_session_identity() -> None:
    state = _state()
    clear_all_workflow_state(state)

    assert state == {SESSION_ID: "a" * 32}
