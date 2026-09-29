"""Upload panel for tabular files."""

from __future__ import annotations

import logging
import shutil
from io import BytesIO

import streamlit as st

from config.settings import Settings
from src.documents.chunker import chunk_document_pages
from src.documents.embedding import EmbeddingError, SentenceTransformerEmbedder
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from src.ingestion.pdf_loader import PDFLoadError, count_pdf_pages, load_pdf_file
from src.ingestion.tabular_loader import (
    TabularLoadError,
    list_excel_sheets,
    load_tabular_file,
)
from src.memory.session_keys import (
    DOCUMENT_ERROR,
    DOCUMENT_RETRIEVER,
    DOCUMENT_SIGNATURE,
    DOCUMENT_STATUS,
    TABULAR_SIGNATURE,
    UPLOAD_ERROR,
    UPLOADED_TABLE,
    UPLOADER_GENERATION,
)
from src.memory.state_management import (
    clear_all_workflow_state,
    clear_document_state,
    clear_table_state,
)
from src.utils.error_reporting import report_error
from src.utils.hashing import sha256_bytes

LOGGER = logging.getLogger(__name__)


def render_upload_panel(settings: Settings) -> None:
    st.header("Inputs")
    generation = int(st.session_state.get(UPLOADER_GENERATION, 0))
    if st.button("Reset session data", use_container_width=True):
        _close_active_document_store()
        clear_all_workflow_state(st.session_state)
        st.session_state[UPLOADER_GENERATION] = generation + 1
        try:
            _remove_session_storage(settings)
        except OSError as exc:
            st.warning(f"Session state was cleared, but local cleanup failed: {exc}")
        st.rerun()

    uploaded_file = st.file_uploader(
        "Upload a CSV or Excel file",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=False,
        help="Structured data used for analytics and SQL.",
        key=f"tabular_uploader_{generation}",
    )

    if uploaded_file is not None:
        payload = uploaded_file.getvalue()
        if len(payload) > settings.max_tabular_upload_bytes:
            limit_mb = settings.max_tabular_upload_bytes / (1024 * 1024)
            st.session_state.pop(UPLOADED_TABLE, None)
            st.error(f"Tabular uploads are limited to {limit_mb:g} MB.")
        else:
            sheet_name = _select_excel_sheet(payload, uploaded_file.name)
            table_signature = f"{sha256_bytes(payload)}:{sheet_name or ''}"
            if st.session_state.get(TABULAR_SIGNATURE) != table_signature:
                _handle_tabular_upload(
                    payload=payload,
                    filename=uploaded_file.name,
                    signature=table_signature,
                    sheet_name=sheet_name,
                    settings=settings,
                )
            elif UPLOADED_TABLE in st.session_state:
                loaded_table = st.session_state[UPLOADED_TABLE]
                st.success(
                    f"Loaded {loaded_table.row_count:,} rows and "
                    f"{loaded_table.column_count:,} columns."
                )
                _render_column_warnings(loaded_table.column_warnings)
            elif UPLOAD_ERROR in st.session_state:
                st.error(st.session_state[UPLOAD_ERROR])
                if st.button("Retry upload", key=f"retry_tabular_{table_signature}"):
                    st.session_state.pop(TABULAR_SIGNATURE, None)
                    st.rerun()
    else:
        if TABULAR_SIGNATURE in st.session_state:
            _remove_table_storage(settings)
            clear_table_state(st.session_state)
        st.info("Upload a CSV or Excel file to begin.")

    pdf_files = st.file_uploader(
        "Upload PDF document(s)",
        type=["pdf"],
        accept_multiple_files=True,
        help="PDFs are indexed for document question answering.",
        key=f"pdf_uploader_{generation}",
    )

    if pdf_files:
        uploads = [(pdf_file.name, pdf_file.getvalue()) for pdf_file in pdf_files]
        document_signature = _pdf_signature(uploads)
        if st.session_state.get(DOCUMENT_SIGNATURE) != document_signature:
            _handle_pdf_uploads(uploads, settings, document_signature)
        elif DOCUMENT_STATUS in st.session_state:
            status = st.session_state[DOCUMENT_STATUS]
            st.success(
                f"Indexed {status['document_count']} PDF document(s) "
                f"into {status['chunk_count']} chunk(s)."
            )
        elif DOCUMENT_ERROR in st.session_state:
            st.error(st.session_state[DOCUMENT_ERROR])
            if st.button("Retry indexing", key=f"retry_pdf_{document_signature!r}"):
                st.session_state.pop(DOCUMENT_SIGNATURE, None)
                st.rerun()
    elif DOCUMENT_SIGNATURE in st.session_state:
        _close_active_document_store()
        _reset_document_store(settings)
        clear_document_state(st.session_state)

    st.divider()
    st.subheader("Configuration")
    st.write("Gemini:", "configured" if settings.gemini_configured else "not configured")
    st.write("Gemini model:", settings.gemini_model)
    st.write("Embedding model:", settings.embedding_model)
    if settings.gemini_configured:
        st.caption("Gemini is ready for SQL and document question answering.")
    else:
        st.caption("Set GEMINI_API_KEY in .env before using LLM-backed agents.")


def _handle_tabular_upload(
    payload: bytes,
    filename: str,
    signature: str,
    settings: Settings,
    sheet_name: str | None = None,
) -> None:
    """Load and store a tabular upload, keeping the previous table on failure.

    The signature is recorded immediately so an unchanged file is not
    reprocessed on every rerun; a failed attempt leaves any previously
    loaded table and its storage untouched and surfaces a Retry option,
    rather than silently discarding it or requiring a full session reset.
    """
    st.session_state[TABULAR_SIGNATURE] = signature
    try:
        loaded_table = load_tabular_file(
            file=BytesIO(payload),
            filename=filename,
            max_rows=settings.max_tabular_rows + 1,
            sheet_name=sheet_name or 0,
        )
    except TabularLoadError as exc:
        st.session_state[UPLOAD_ERROR] = str(exc)
        st.error(str(exc))
        return

    if loaded_table.row_count > settings.max_tabular_rows:
        st.session_state[UPLOAD_ERROR] = (
            f"The upload contains {loaded_table.row_count:,} rows; the current "
            f"limit is {settings.max_tabular_rows:,}."
        )
        st.error(st.session_state[UPLOAD_ERROR])
        return

    _remove_table_storage(settings)
    clear_table_state(st.session_state)
    st.session_state[TABULAR_SIGNATURE] = signature
    st.session_state[UPLOADED_TABLE] = loaded_table
    st.session_state.pop(UPLOAD_ERROR, None)
    st.success("File loaded successfully.")
    st.write("Rows:", loaded_table.row_count)
    st.write("Columns:", loaded_table.column_count)
    _render_column_warnings(loaded_table.column_warnings)


def _render_column_warnings(column_warnings: dict[str, list[str]]) -> None:
    for column, warnings in column_warnings.items():
        for warning in warnings:
            st.warning(f"'{column}': {warning}")


def _select_excel_sheet(payload: bytes, filename: str) -> str | None:
    if not filename.lower().endswith((".xls", ".xlsx")):
        return None
    try:
        sheet_names = list_excel_sheets(BytesIO(payload))
    except TabularLoadError as exc:
        st.error(str(exc))
        return None
    if not sheet_names:
        return None
    return st.selectbox(
        "Excel sheet",
        options=sheet_names,
        key=f"excel_sheet_{sha256_bytes(payload)}",
    )


def _handle_pdf_uploads(
    uploads: list[tuple[str, bytes]],
    settings: Settings,
    signature: tuple[tuple[str, str], ...],
) -> None:
    """Index a PDF upload, preserving the previous good index on failure.

    ChromaDocumentStore.replace_chunks() only swaps the new content into the
    canonical collection after every chunk has been embedded and added
    successfully, so a failure never leaves a partial index. If indexing
    fails, the previous index (if any) is reopened so RAG keeps working
    against the last known-good documents while a Retry option is shown.
    """
    st.session_state[DOCUMENT_SIGNATURE] = signature
    oversized = [
        filename for filename, payload in uploads if len(payload) > settings.max_pdf_upload_bytes
    ]
    if oversized:
        limit_mb = settings.max_pdf_upload_bytes / (1024 * 1024)
        st.session_state[DOCUMENT_ERROR] = f"Each PDF is limited to {limit_mb:g} MB: " + ", ".join(
            oversized
        )
        st.error(st.session_state[DOCUMENT_ERROR])
        return
    if sum(len(payload) for _, payload in uploads) > settings.max_total_pdf_bytes:
        limit_mb = settings.max_total_pdf_bytes / (1024 * 1024)
        st.session_state[DOCUMENT_ERROR] = f"The combined PDF upload is limited to {limit_mb:g} MB."
        st.error(st.session_state[DOCUMENT_ERROR])
        return

    _close_active_document_store()

    try:
        total_pages = sum(
            count_pdf_pages(file=BytesIO(payload), filename=filename)
            for filename, payload in uploads
        )
        if total_pages > settings.max_pdf_pages:
            raise ValueError(
                f"PDF uploads are limited to {settings.max_pdf_pages:,} combined pages."
            )
        documents = [
            (
                load_pdf_file(file=BytesIO(payload), filename=filename),
                sha256_bytes(payload),
            )
            for filename, payload in uploads
        ]
        chunks = [
            chunk
            for document, document_id in documents
            for chunk in chunk_document_pages(
                document.pages,
                document.filename,
                document_id=document_id,
            )
        ]
        if len(chunks) > settings.max_document_chunks:
            raise ValueError(
                f"Document indexing is limited to {settings.max_document_chunks:,} chunks."
            )
        store = ChromaDocumentStore(
            persist_dir=settings.chroma_persist_dir,
            embedder=_get_embedder(settings.embedding_model),
        )
        store.replace_chunks(chunks)
    except (PDFLoadError, EmbeddingError, ValueError) as exc:
        st.session_state[DOCUMENT_ERROR] = str(exc)
        st.error(str(exc))
        _restore_previous_document_retriever(settings)
        return
    except Exception as exc:
        st.session_state[DOCUMENT_ERROR] = report_error(
            LOGGER, "Could not index PDF document(s)", exc
        )
        st.error(st.session_state[DOCUMENT_ERROR])
        _restore_previous_document_retriever(settings)
        return

    st.session_state[DOCUMENT_RETRIEVER] = DocumentRetriever(
        store,
        max_distance=settings.retrieval_max_distance,
        default_top_k=settings.retrieval_top_k,
    )
    st.session_state[DOCUMENT_STATUS] = {
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "filenames": [document.filename for document, _ in documents],
        "document_hashes": [document_id for _, document_id in documents],
    }
    st.session_state.pop(DOCUMENT_ERROR, None)
    st.success(f"Indexed {len(documents)} PDF document(s) into {len(chunks)} chunk(s).")


def _restore_previous_document_retriever(settings: Settings) -> None:
    """Reopen a retriever for the last successfully indexed documents, if any."""
    if DOCUMENT_STATUS not in st.session_state:
        return
    store = ChromaDocumentStore(
        persist_dir=settings.chroma_persist_dir,
        embedder=_get_embedder(settings.embedding_model),
    )
    st.session_state[DOCUMENT_RETRIEVER] = DocumentRetriever(
        store,
        max_distance=settings.retrieval_max_distance,
        default_top_k=settings.retrieval_top_k,
    )


def _pdf_signature(
    uploads: list[tuple[str, bytes]],
) -> tuple[tuple[str, str], ...]:
    return tuple((filename, sha256_bytes(payload)) for filename, payload in uploads)


@st.cache_resource(show_spinner=False)
def _get_embedder(model_name: str) -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(model_name)


def _reset_document_store(settings: Settings) -> None:
    if not settings.chroma_persist_dir.exists():
        return
    store = ChromaDocumentStore(
        persist_dir=settings.chroma_persist_dir,
        embedder=_get_embedder(settings.embedding_model),
    )
    store.reset()
    store.close()


def _close_active_document_store() -> None:
    retriever = st.session_state.get(DOCUMENT_RETRIEVER)
    close = getattr(retriever, "close", None)
    if callable(close):
        close()


def _remove_session_storage(settings: Settings) -> None:
    if not settings.tenant_id:
        raise ValueError("Refusing to remove session storage without a known tenant_id.")
    session_dir = settings.session_dir
    if session_dir is None or not session_dir.exists():
        return

    expected_parent = (settings.app_data_dir / "sessions" / settings.tenant_id).resolve()
    resolved_session_dir = session_dir.resolve()
    if resolved_session_dir.parent != expected_parent:
        raise ValueError("Refusing to remove storage outside the current tenant's sessions.")
    shutil.rmtree(resolved_session_dir)


def _remove_table_storage(settings: Settings) -> None:
    database_path = settings.sqlite_db_path
    if not database_path.exists():
        return

    session_dir = settings.session_dir
    if session_dir is None or not database_path.resolve().is_relative_to(session_dir.resolve()):
        raise ValueError("Refusing to remove a database outside the session directory.")
    database_path.unlink()
