"""INV-11 (#144) S1: durable investigation records, content, idempotency, and reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.api.investigation_store import (
    INTERRUPTED_BY_RESTART,
    InvestigationConflictError,
    InvestigationContentError,
    InvestigationNotFoundError,
    InvestigationRecordStore,
)
from apps.api.metadata_store import MetadataStore, StoredWorkspace
from packages.investigation import InvestigationStatus

S = InvestigationStatus
TENANT = "a" * 32
OTHER_TENANT = "b" * 32
WORKSPACE = "ws_" + "1" * 32
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
DIGEST = "d" * 64


def _metadata(root: Path) -> MetadataStore:
    store = MetadataStore(root / ".metadata" / "metadata.db")
    store.open()
    store.save_workspace(
        StoredWorkspace(
            id=WORKSPACE,
            tenant_id=TENANT,
            authentication_mode="local",
            expires_at=NOW + timedelta(hours=24),
            gemini_configured=False,
            local_only_mode=True,
            consent_notice_version=None,
            consent_accepted_at=None,
            created_at=NOW,
            data_recipients=(),
            consent_recipients=(),
        )
    )
    return store


def _store(root: Path, metadata: MetadataStore | None, key: bytes | None = None) -> Any:
    (root / TENANT / WORKSPACE).mkdir(parents=True, exist_ok=True)
    return InvestigationRecordStore(
        storage_root=root, metadata=metadata, encryption_key=key, clock=lambda: NOW
    )


def _create(store: InvestigationRecordStore, key: str = "key-1", **overrides: str) -> Any:
    fields = {
        "investigation_id": "inv_" + "0" * 32,
        "tenant_id": TENANT,
        "workspace_id": WORKSPACE,
        "conversation_id": "conv_1",
        "message_id": "msg_1",
        "idempotency_key": key,
    }
    return store.create(**(fields | overrides))


@pytest.fixture(params=["durable", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> InvestigationRecordStore:
    metadata = _metadata(tmp_path) if request.param == "durable" else None
    return _store(tmp_path, metadata)


def test_creation_is_idempotent_by_tenant_and_key(store: InvestigationRecordStore) -> None:
    record, created = _create(store)
    assert created and record.status is S.PLANNING
    again, created_again = _create(store, investigation_id="inv_" + "9" * 32)
    assert not created_again and again == record
    with pytest.raises(InvestigationConflictError, match="idempotency_key_reused"):
        _create(store, message_id="msg_2")
    other, created_other = _create(store, tenant_id=OTHER_TENANT, investigation_id="inv_x")
    assert created_other and other.id == "inv_x"  # keys are scoped per tenant


def test_foreign_and_unknown_records_are_indistinguishable(store: InvestigationRecordStore) -> None:
    record, _ = _create(store)
    for investigation_id, tenant in ((record.id, OTHER_TENANT), ("inv_missing", TENANT)):
        with pytest.raises(InvestigationNotFoundError):
            store.get(investigation_id, tenant)


def test_only_legal_transitions_apply_and_terminal_records_never_change(
    store: InvestigationRecordStore,
) -> None:
    record, _ = _create(store)
    with pytest.raises(InvestigationConflictError, match="illegal_transition"):
        store.update(record.id, TENANT, status=S.RUNNING)  # planning -> running is illegal
    store.update(record.id, TENANT, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
    with pytest.raises(InvestigationConflictError, match="plan_digest_conflict"):
        store.update(record.id, TENANT, plan_digest="e" * 64)
    store.update(record.id, TENANT, status=S.PLAN_REJECTED)
    with pytest.raises(InvestigationConflictError):
        store.update(record.id, TENANT, status=S.QUEUED)
    with pytest.raises(InvestigationConflictError, match="investigation_terminal"):
        store.update(record.id, TENANT, explanation_code="anything")
    _create(store, key="k2", investigation_id="inv_2")
    with pytest.raises(ValueError):  # codes are fixed tokens, never free text
        store.update("inv_2", TENANT, explanation_code="Free text with spaces")


def test_records_and_idempotency_survive_a_restart(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    first = _store(tmp_path, metadata)
    record, _ = _create(first)
    first.update(record.id, TENANT, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
    metadata.close()
    reopened = MetadataStore(tmp_path / ".metadata" / "metadata.db")
    reopened.open()
    second = _store(tmp_path, reopened)
    assert second.load() == 1
    loaded = second.get(record.id, TENANT)
    assert (loaded.status, loaded.plan_digest) == (S.AWAITING_PLAN_APPROVAL, DIGEST)
    replayed, created = _create(second, investigation_id="inv_" + "7" * 32)
    assert not created and replayed.id == record.id  # durable idempotency across restarts


def test_reconciliation_interrupts_only_live_records(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    store = _store(tmp_path, metadata)
    live, _ = _create(store, key="live", investigation_id="inv_live")
    done, _ = _create(store, key="done", investigation_id="inv_done")
    store.update(done.id, TENANT, status=S.FAILED, terminal_category="internal")
    changed = store.reconcile()
    assert [record.id for record in changed] == [live.id]
    reloaded = _store(tmp_path, metadata)
    reloaded.load()
    assert reloaded.get(live.id, TENANT).status is S.INTERRUPTED
    assert reloaded.get(live.id, TENANT).explanation_code == INTERRUPTED_BY_RESTART
    assert reloaded.get(done.id, TENANT).status is S.FAILED
    assert reloaded.reconcile() == []  # idempotent


def test_reports_survive_a_restart_and_are_digest_checked(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    store = _store(tmp_path, metadata)
    record, _ = _create(store)
    record = store.update(record.id, TENANT, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
    store.save_plan(record, '{"plan": "text"}')
    stored = store.save_report(record, '{"report": 1}', '{"ev_1": {}}')
    with pytest.raises(InvestigationConflictError, match="report_already_stored"):
        store.save_report(stored, '{"report": 2}', "{}")
    restarted = _store(tmp_path, metadata)
    restarted.load()
    loaded = restarted.get(record.id, TENANT)
    assert restarted.load_report(loaded) == ('{"report": 1}', '{"ev_1": {}}')
    assert restarted.load_plan(loaded) == '{"plan": "text"}'
    import sqlite3

    path = tmp_path / TENANT / WORKSPACE / "investigations.db"
    with sqlite3.connect(path) as connection:  # tamper with the stored report
        connection.execute("UPDATE investigation_reports SET report = '{\"report\": 9}'")
    with pytest.raises(InvestigationContentError, match="report_digest_mismatch"):
        restarted.load_report(loaded)


def test_content_is_encrypted_when_a_key_is_configured(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    store = _store(tmp_path, metadata, key=b"k" * 32)
    record, _ = _create(store)
    record = store.update(record.id, TENANT, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
    store.save_plan(record, '{"plan": "CANARY-plan-text"}')
    raw = (tmp_path / TENANT / WORKSPACE / "investigations.db").read_bytes()
    assert b"CANARY-plan-text" not in raw
    assert store.load_plan(record) == '{"plan": "CANARY-plan-text"}'


def test_metadata_rows_hold_no_content(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path)
    store = _store(tmp_path, metadata)
    record, _ = _create(store, key="client key with secret words")
    record = store.update(record.id, TENANT, status=S.AWAITING_PLAN_APPROVAL, plan_digest=DIGEST)
    store.save_plan(record, '{"plan": "CANARY-plan-text"}')
    metadata.close()
    raw = (tmp_path / ".metadata" / "metadata.db").read_bytes()
    assert b"CANARY-plan-text" not in raw and b"secret words" not in raw


def test_deleting_a_workspace_forgets_its_records(store: InvestigationRecordStore) -> None:
    record, _ = _create(store)
    assert store.delete_workspace(TENANT, WORKSPACE) == (record.id,)
    with pytest.raises(InvestigationNotFoundError):
        store.get(record.id, TENANT)
