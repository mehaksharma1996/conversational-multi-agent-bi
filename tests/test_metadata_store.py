"""The durable metadata store: migrations, version refusal, corruption handling, and round trips."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.api.metadata_store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    MetadataCorruptError,
    MetadataError,
    MetadataStore,
    MetadataVersionError,
    Migration,
    StoredUpload,
    StoredWorkspace,
    file_sha256,
    key_hash,
    write_payload_atomically,
)

TENANT = "a" * 32
WORKSPACE = "ws_" + "1" * 32
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

SECOND_MIGRATION = Migration(
    2,
    "add a label column",
    ("ALTER TABLE workspaces ADD COLUMN label TEXT",),
)


def _workspace(workspace_id: str = WORKSPACE, **overrides: object) -> StoredWorkspace:
    values: dict[str, object] = {
        "id": workspace_id,
        "tenant_id": TENANT,
        "authentication_mode": "local",
        "expires_at": NOW + timedelta(hours=24),
        "gemini_configured": True,
        "local_only_mode": False,
        "consent_notice_version": "2026-09",
        "consent_accepted_at": NOW,
        "created_at": NOW,
        "data_recipients": ("gemini",),
        "consent_recipients": ("gemini", "ollama"),
    }
    values.update(overrides)
    return StoredWorkspace(**values)  # type: ignore[arg-type]


def _upload(upload_id: str = "upl_" + "2" * 32, workspace_id: str = WORKSPACE) -> StoredUpload:
    return StoredUpload(
        id=upload_id,
        workspace_id=workspace_id,
        tenant_id=TENANT,
        filename="sales.csv",
        content_type="text/csv",
        size_bytes=12,
        sha256="f" * 64,
        created_at=NOW,
    )


def test_a_new_database_applies_every_migration_and_a_reopen_applies_none(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "meta" / "metadata.db")

    first = store.open()
    second = store.open()

    assert first.schema_version == SCHEMA_VERSION
    assert first.migrations_applied == tuple(m.version for m in MIGRATIONS)
    assert second.migrations_applied == ()
    assert store.integrity_problems() == []
    store.close()


def test_a_future_migration_is_applied_forward_and_keeps_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    old = MetadataStore(path)
    old.open()
    old.save_workspace(_workspace())
    old.save_upload(_upload())
    old.close()

    upgraded = MetadataStore(path, migrations=(*MIGRATIONS, SECOND_MIGRATION))
    result = upgraded.open()

    assert result.schema_version == 2 and result.migrations_applied == (2,)
    assert [w.id for w in upgraded.load_workspaces()] == [WORKSPACE]
    assert [u.id for u in upgraded.load_uploads()] == [_upload().id]
    upgraded.close()


def test_a_database_from_a_newer_build_is_refused_not_guessed_at(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    newer = MetadataStore(path, migrations=(*MIGRATIONS, SECOND_MIGRATION))
    newer.open()
    newer.close()

    with pytest.raises(MetadataVersionError, match="newer than this build"):
        MetadataStore(path).open()


def test_an_altered_recorded_migration_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    MetadataStore(path).open()
    altered = Migration(1, MIGRATIONS[0].description, (*MIGRATIONS[0].statements, "SELECT 1"))

    with pytest.raises(MetadataVersionError, match="different contents"):
        MetadataStore(path, migrations=(altered,)).open()


def test_migrations_must_be_numbered_consecutively() -> None:
    with pytest.raises(ValueError, match="consecutively"):
        MetadataStore(Path("unused.db"), migrations=(SECOND_MIGRATION,))


def test_a_failed_migration_rolls_back_completely(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    MetadataStore(path).open()
    broken = Migration(
        2,
        "half applied",
        ("ALTER TABLE workspaces ADD COLUMN ok_column TEXT", "THIS IS NOT SQL"),
    )

    with pytest.raises(MetadataCorruptError):
        MetadataStore(path, migrations=(*MIGRATIONS, broken)).open()

    store = MetadataStore(path)
    assert store.open().schema_version == SCHEMA_VERSION  # version 2 was never recorded
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(workspaces)")]
    assert "ok_column" not in columns
    store.close()


def test_an_unreadable_file_is_reported_and_can_be_quarantined_then_reopened(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata.db"
    path.write_bytes(b"this is definitely not a sqlite database" * 50)
    store = MetadataStore(path)

    with pytest.raises(MetadataCorruptError):
        store.open()
    moved = store.quarantine("20261006T120000Z")

    assert moved is not None and moved.name == "metadata.db.corrupt-20261006T120000Z"
    assert moved.exists() and not path.exists()
    assert store.open().migrations_applied == tuple(m.version for m in MIGRATIONS)
    store.close()


def test_operations_on_a_closed_store_fail_loudly(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")

    with pytest.raises(MetadataError, match="not open"):
        store.load_workspaces()
    assert store.ping() is False


def test_workspaces_round_trip_with_consent_and_recipients(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()
    store.save_workspace(_workspace())
    store.save_workspace(
        _workspace(
            "ws_" + "3" * 32,
            consent_notice_version=None,
            consent_accepted_at=None,
            data_recipients=(),
            consent_recipients=(),
            gemini_configured=False,
        )
    )

    first, second = store.load_workspaces()

    assert first == _workspace()
    assert second.consent_notice_version is None and second.consent_recipients == ()
    store.close()


def test_saving_again_updates_consent_and_expiry_but_not_identity(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()
    store.save_workspace(_workspace(consent_notice_version=None, consent_accepted_at=None))

    store.save_workspace(_workspace(tenant_id="b" * 32, consent_notice_version="2026-10"))
    store.update_expiry(WORKSPACE, NOW + timedelta(hours=1))
    (stored,) = store.load_workspaces()

    assert stored.tenant_id == TENANT, "an upsert must never reassign a workspace to a tenant"
    assert stored.consent_notice_version == "2026-10"
    assert stored.expires_at == NOW + timedelta(hours=1)
    store.close()


def test_idempotency_keys_are_stored_only_as_digests(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()
    store.save_workspace(_workspace())
    store.save_creation_key(TENANT, key_hash("client-secret-token"), WORKSPACE)

    ((tenant, stored_key, workspace),) = store.load_creation_keys()
    store.close()

    assert (tenant, workspace) == (TENANT, WORKSPACE)
    assert stored_key == key_hash("client-secret-token") and "secret" not in stored_key
    assert b"client-secret-token" not in (tmp_path / "metadata.db").read_bytes()


def test_deleting_a_workspace_removes_its_keys_and_uploads(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()
    store.save_workspace(_workspace())
    store.save_workspace(_workspace("ws_" + "4" * 32))
    store.save_creation_key(TENANT, key_hash("k"), WORKSPACE)
    store.save_upload(_upload())

    store.delete_workspace(WORKSPACE)

    assert [w.id for w in store.load_workspaces()] == ["ws_" + "4" * 32]
    assert store.load_creation_keys() == [] and store.load_uploads() == []
    store.close()


def test_an_upload_cannot_reference_a_missing_workspace(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()

    with pytest.raises(sqlite3.IntegrityError):
        store.save_upload(_upload())
    store.close()


def test_the_online_backup_is_a_consistent_openable_copy(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata.db")
    store.open()
    store.save_workspace(_workspace())

    store.backup_to(tmp_path / "copy" / "metadata.db")
    store.close()
    copy = MetadataStore(tmp_path / "copy" / "metadata.db")
    copy.open()

    assert [w.id for w in copy.load_workspaces()] == [WORKSPACE]
    copy.close()


def test_payloads_are_written_atomically_and_hashed(tmp_path: Path) -> None:
    target = tmp_path / "ws" / "uploads" / "upl_x.bin"

    digest = write_payload_atomically(target, b"a,b\n1,2\n")

    assert target.read_bytes() == b"a,b\n1,2\n"
    assert digest == file_sha256(target)
    assert not list(target.parent.glob(".*.tmp")), "no temporary file may remain"
