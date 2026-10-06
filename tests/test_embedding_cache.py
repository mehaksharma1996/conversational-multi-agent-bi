"""Tests for bounded, workspace-scoped embedding-vector reuse."""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from apps.api.main import create_app
from config.settings import Settings
from packages.observability import InMemoryTelemetrySink
from src.documents.embedding_cache import CachingTextEmbedder, EmbeddingVectorCache
from src.utils.identity import LOCAL_DEV_TENANT_ID


class CountingEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), float(index)] for index, text in enumerate(texts)]


def _wrapper(
    inner: CountingEmbedder,
    cache: EmbeddingVectorCache,
    *,
    tenant: str = "tenant-a",
    workspace: str = "workspace-a",
    model: str = "model-a",
) -> CachingTextEmbedder:
    return CachingTextEmbedder(
        inner,
        cache,
        tenant_id=tenant,
        workspace_id=workspace,
        model_identity=model,
    )


def test_identical_content_hits_but_changed_content_model_or_scope_misses() -> None:
    cache = EmbeddingVectorCache(max_entries=20)
    inner = CountingEmbedder()

    first = _wrapper(inner, cache).embed_texts(["same document", "same document"])
    second = _wrapper(inner, cache).embed_texts(["same document"])
    _wrapper(inner, cache).embed_texts(["changed document"])
    _wrapper(inner, cache, model="model-b").embed_texts(["same document"])
    _wrapper(inner, cache, tenant="tenant-b").embed_texts(["same document"])
    _wrapper(inner, cache, workspace="workspace-b").embed_texts(["same document"])

    assert first[0] == first[1] == second[0]
    assert inner.calls == [
        ["same document"],
        ["changed document"],
        ["same document"],
        ["same document"],
        ["same document"],
    ]


def test_cache_is_lru_bounded_and_workspace_purge_isolated() -> None:
    cache = EmbeddingVectorCache(max_entries=2)
    inner = CountingEmbedder()
    primary = _wrapper(inner, cache)
    other = _wrapper(inner, cache, workspace="workspace-b")

    primary.embed_texts(["one"])
    other.embed_texts(["two"])
    assert cache.entry_count == 2

    cache.delete_workspace("tenant-a", "workspace-a")
    assert cache.entry_count == 1
    other.embed_texts(["two"])
    assert len(inner.calls) == 2

    primary.embed_texts(["three", "four"])
    assert cache.entry_count == 2
    other.embed_texts(["two"])
    assert len(inner.calls) == 4


def _settings(tmp_path) -> Settings:
    return Settings(
        app_data_dir=tmp_path,
        sqlite_db_path=tmp_path / "sqlite" / "app.db",
        chroma_persist_dir=tmp_path / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="fake",
    )


def _pdf_bytes() -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Synthetic policy requires manager review over 500.")
    pdf.save()
    return buffer.getvalue()


def test_reupload_emits_content_free_embedding_cache_hit_and_miss_counts(tmp_path) -> None:
    telemetry = InMemoryTelemetrySink()
    inner = CountingEmbedder()
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: inner,
        telemetry_sink=telemetry,
    )
    client = TestClient(app)
    workspace = client.post("/api/v1/workspaces").json()["id"]
    upload = {"files": [("files", ("policy.pdf", _pdf_bytes(), "application/pdf"))]}

    assert (
        client.post(
            f"/api/v1/workspaces/{workspace}/document-collections",
            **upload,
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/workspaces/{workspace}/document-collections",
            **upload,
        ).status_code
        == 201
    )

    events = telemetry.named("embedding.cache")
    first_misses = events[0].attributes["cache_misses"]
    last_hits = events[-1].attributes["cache_hits"]
    assert isinstance(first_misses, int) and first_misses > 0
    assert isinstance(last_hits, int) and last_hits > 0
    assert all(event.dropped_attributes == 0 for event in events)


def test_workspace_delete_and_expiry_purge_cached_vectors(tmp_path) -> None:
    cache = EmbeddingVectorCache()
    app = create_app(settings=_settings(tmp_path), embedding_cache=cache)
    client = TestClient(app)

    deleted_workspace = client.post("/api/v1/workspaces").json()["id"]
    cache.put((LOCAL_DEV_TENANT_ID, deleted_workspace, "model", "digest"), [1.0])
    assert client.delete(f"/api/v1/workspaces/{deleted_workspace}").status_code == 204
    assert cache.entry_count == 0

    expiring_workspace = client.post("/api/v1/workspaces").json()["id"]
    cache.put((LOCAL_DEV_TENANT_ID, expiring_workspace, "model", "digest"), [2.0])
    expires_at = app.state.repository.get_workspace(
        expiring_workspace,
        LOCAL_DEV_TENANT_ID,
    ).expires_at
    with patch(
        "apps.api.repository._now",
        return_value=expires_at + timedelta(seconds=1),
    ):
        assert client.get(f"/api/v1/workspaces/{expiring_workspace}").status_code == 404
    assert cache.entry_count == 0
