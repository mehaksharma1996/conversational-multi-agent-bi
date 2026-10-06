"""The per-workspace content database: fidelity, encryption, schema guard, and cleanup."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from apps.api.content_store import (
    CONTENT_VERSION,
    ContentStoreError,
    StoredConversation,
    StoredExport,
    StoredMessage,
    WorkspaceContentStore,
)
from src.memory.session_memory import SessionMemory
from src.orchestration.graph_state import AnswerDiagnostics

TENANT = "a" * 32
WORKSPACE = "ws_" + "1" * 32
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
KEY = bytes(range(32))


def _memory() -> SessionMemory:
    return SessionMemory(
        row_count=3,
        column_count=2,
        mapped_fields={"amount": "amount"},
        missing_fields=["label"],
        available_capabilities=["basic_analytics"],
        unavailable_capabilities={"classification": "needs a label"},
        analytics_highlights=["Total amount is 125"],
        anomaly_findings=[],
        chart_summaries=["Amount over time"],
        report_sections={"Summary": ["one", "two"]},
        document_summary=["Indexed documents: 1"],
        limitations=["Small sample"],
        last_sql='SELECT "amount" FROM "uploaded_data"',
        last_sql_question="biggest amounts?",
        recent_questions=["biggest amounts?"],
        classification_findings=[],
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "merchant": ["=2+2", "Café ☕", None],
            "amount": [75, 40, 12],
            "ratio": [0.5, None, 2.25],
            "flag": [True, False, True],
            "when": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
        }
    )


def _message(message_id: str = "msg_" + "2" * 32, **overrides: object) -> StoredMessage:
    values: dict[str, object] = {
        "id": message_id,
        "conversation_id": "conv_" + "3" * 32,
        "tenant_id": TENANT,
        "question": "Which transactions are large?",
        "answer": "Two rows match.",
        "route": "sql",
        "sql": 'SELECT "merchant" FROM "uploaded_data"',
        "dataframe": _frame(),
        "sources": ("policy.pdf p.1",),
        "request_id": "req-1",
        "diagnostics": AnswerDiagnostics(
            sql_row_count=3,
            grounding_status="checked_no_issues",
            criteria_keys=("thresholds",),
            llm_estimated_cost_usd=0.002,
        ),
        "status": "complete",
        "checkpoint_thread_id": None,
        "created_at": NOW,
    }
    values.update(overrides)
    return StoredMessage(**values)  # type: ignore[arg-type]


def _conversation() -> StoredConversation:
    return StoredConversation(
        id="conv_" + "3" * 32,
        tenant_id=TENANT,
        dataset_id="ds_" + "4" * 32,
        document_collection_id=None,
        memory=_memory(),
        message_ids=("msg_" + "2" * 32,),
        created_at=NOW,
    )


def _export() -> StoredExport:
    return StoredExport(
        id="export_" + "5" * 32,
        message_id="msg_" + "2" * 32,
        tenant_id=TENANT,
        format="csv",
        filename="query-result.csv",
        media_type="text/csv",
        payload=b"merchant,amount\n'=2+2,75\n\x00\xff",
        created_at=NOW,
    )


def test_conversations_messages_and_exports_round_trip_exactly(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_conversation(WORKSPACE, _conversation())
    store.save_message(WORKSPACE, _message())
    store.save_export(WORKSPACE, _export())

    snapshot = store.load(TENANT, WORKSPACE)

    assert snapshot.conversations == [_conversation()]
    (message,) = snapshot.messages
    expected = _message()
    loaded = message.dataframe
    assert loaded is not None and expected.dataframe is not None
    # Datetime resolution (ns vs us) is a pandas inference detail and does not change any value or
    # exported text; every other column must keep its exact dtype and values.
    assert loaded.dtypes.drop("when").equals(expected.dataframe.dtypes.drop("when"))
    pd.testing.assert_frame_equal(
        loaded.assign(when=loaded["when"].astype("datetime64[us]")),
        expected.dataframe.assign(when=expected.dataframe["when"].astype("datetime64[us]")),
    )
    assert replace(message, dataframe=None) == replace(expected, dataframe=None)
    assert snapshot.exports == [_export()], "binary payloads must survive byte for byte"


def test_a_message_without_a_result_frame_round_trips(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_conversation(WORKSPACE, _conversation())
    store.save_message(WORKSPACE, _message(dataframe=None, route="rag"))

    (message,) = store.load(TENANT, WORKSPACE).messages

    assert message.dataframe is None and message.route == "rag"


def test_updating_a_conversation_and_deleting_messages_removes_their_exports(
    tmp_path: Path,
) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_conversation(WORKSPACE, _conversation())
    store.save_message(WORKSPACE, _message())
    store.save_message(WORKSPACE, _message("msg_" + "6" * 32))
    store.save_export(WORKSPACE, _export())

    store.update_conversation(TENANT, WORKSPACE, _conversation().id, None, ("msg_" + "6" * 32,))
    store.delete_messages(TENANT, WORKSPACE, ["msg_" + "2" * 32])
    snapshot = store.load(TENANT, WORKSPACE)

    assert snapshot.conversations[0].memory is None
    assert snapshot.conversations[0].message_ids == ("msg_" + "6" * 32,)
    assert [m.id for m in snapshot.messages] == ["msg_" + "6" * 32]
    assert snapshot.exports == []


def test_resource_ids_list_everything_without_loading_content(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_conversation(WORKSPACE, _conversation())
    store.save_message(WORKSPACE, _message())
    store.save_export(WORKSPACE, _export())

    assert sorted(store.resource_ids(TENANT, WORKSPACE)) == sorted(
        [_conversation().id, _message().id, _export().id]
    )


def test_the_database_is_encrypted_when_a_key_is_configured(tmp_path: Path) -> None:
    keyed = WorkspaceContentStore(tmp_path, KEY)
    keyed.save_conversation(WORKSPACE, _conversation())
    keyed.save_message(WORKSPACE, _message())
    path = keyed.path(TENANT, WORKSPACE)

    assert b"Which transactions are large?" not in path.read_bytes()
    with closing(sqlite3.connect(path)) as plain, pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT * FROM sqlite_master").fetchall()
    assert keyed.load(TENANT, WORKSPACE).messages[0].question == "Which transactions are large?"
    with pytest.raises(ContentStoreError):
        WorkspaceContentStore(tmp_path, bytes(32)).load(TENANT, WORKSPACE)
    with pytest.raises(ContentStoreError):
        WorkspaceContentStore(tmp_path, None).load(TENANT, WORKSPACE)


def test_unencrypted_content_is_readable_as_plain_sqlite_by_design(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_message(WORKSPACE, _message())

    assert b"Which transactions are large?" in store.path(TENANT, WORKSPACE).read_bytes()


def test_a_database_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_message(WORKSPACE, _message())
    with closing(sqlite3.connect(store.path(TENANT, WORKSPACE), isolation_level=None)) as raw:
        raw.execute(f"PRAGMA user_version = {CONTENT_VERSION + 1}")

    with pytest.raises(ContentStoreError, match="newer build"):
        store.load(TENANT, WORKSPACE)


def test_identifiers_that_could_escape_the_storage_root_are_rejected(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)

    with pytest.raises(ContentStoreError):
        store.path("../x", WORKSPACE)
    with pytest.raises(ContentStoreError):
        store.path(TENANT, "ws_" + "../" * 11)


def test_reading_a_workspace_without_content_fails_without_creating_a_file(
    tmp_path: Path,
) -> None:
    store = WorkspaceContentStore(tmp_path, None)

    with pytest.raises(ContentStoreError, match="no content database"):
        store.load(TENANT, WORKSPACE)
    assert not store.exists(TENANT, WORKSPACE)
    assert not (tmp_path / TENANT).exists()


def test_delete_all_removes_the_database_and_leaves_no_open_handle(tmp_path: Path) -> None:
    store = WorkspaceContentStore(tmp_path, None)
    store.save_message(WORKSPACE, _message())
    path = store.path(TENANT, WORKSPACE)

    store.delete_all(TENANT, WORKSPACE)

    assert not path.exists()
    # Renaming the directory would fail on Windows if any handle were still open.
    path.parent.rename(path.parent.with_name("moved"))
