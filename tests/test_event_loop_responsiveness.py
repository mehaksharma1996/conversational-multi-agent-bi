"""Heavy synchronous work must not stall the API event loop (issue #10).

PDF indexing (extraction, embedding, index writes) can run for many seconds. If an ``async`` route
called it directly, every other request on the process, including liveness probes, would wait.
"""

from __future__ import annotations

import asyncio
import threading
from io import BytesIO

import httpx
from reportlab.pdfgen import canvas

from apps.api.main import create_app
from config.settings import Settings


class BlockingEmbedder:
    """Blocks inside ``embed_texts`` until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.entered.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return [[1.0, float(index)] for index, _ in enumerate(texts)]


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


def test_liveness_answers_while_a_document_index_is_running(tmp_path) -> None:
    embedder = BlockingEmbedder()
    app = create_app(settings=_settings(tmp_path), embedder_factory=lambda _model: embedder)

    async def scenario() -> tuple[int, bool, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            workspace = (await client.post("/api/v1/workspaces")).json()["id"]
            indexing = asyncio.create_task(
                client.post(
                    f"/api/v1/workspaces/{workspace}/document-collections",
                    files=[("files", ("policy.pdf", _pdf_bytes(), "application/pdf"))],
                )
            )
            assert await asyncio.to_thread(embedder.entered.wait, 5), "indexing never started"
            health = await asyncio.wait_for(client.get("/health/live"), timeout=3)
            still_indexing = not embedder.finished.is_set()
            embedder.release.set()
            created = await indexing
            return health.status_code, still_indexing, created.status_code

    health_status, still_indexing, created_status = asyncio.run(scenario())

    assert health_status == 200
    assert still_indexing, "the health check was only answered after indexing had finished"
    assert created_status == 201
