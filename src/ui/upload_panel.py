"""Upload panel for tabular files."""

from __future__ import annotations

import streamlit as st

from config.settings import Settings
from src.documents.chunker import chunk_document
from src.documents.embedding import EmbeddingError, SentenceTransformerEmbedder
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from src.ingestion.pdf_loader import PDFLoadError, load_pdf_file
from src.ingestion.tabular_loader import TabularLoadError, load_tabular_file
from src.memory.session_keys import (
    DOCUMENT_ERROR,
    DOCUMENT_RETRIEVER,
    DOCUMENT_SIGNATURE,
    DOCUMENT_STATUS,
    UPLOADED_TABLE,
    UPLOAD_ERROR,
)


def render_upload_panel(settings: Settings) -> None:
    st.header("Inputs")
    uploaded_file = st.file_uploader(
        "Upload a CSV or Excel file",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=False,
        help="Structured data used for analytics and SQL.",
    )

    if uploaded_file is not None:
        _handle_tabular_upload(uploaded_file)
    elif UPLOADED_TABLE not in st.session_state:
        st.info("Upload a CSV or Excel file to begin.")

    pdf_files = st.file_uploader(
        "Upload PDF document(s)",
        type=["pdf"],
        accept_multiple_files=True,
        help="PDFs are indexed for document question answering.",
    )

    if pdf_files:
        signature = _pdf_signature(pdf_files)
        if st.session_state.get(DOCUMENT_SIGNATURE) != signature:
            _handle_pdf_uploads(pdf_files, settings, signature)
        elif DOCUMENT_STATUS in st.session_state:
            status = st.session_state[DOCUMENT_STATUS]
            st.success(
                f"Indexed {status['document_count']} PDF document(s) "
                f"into {status['chunk_count']} chunk(s)."
            )

    st.divider()
    st.subheader("Configuration")
    st.write("Gemini:", "configured" if settings.gemini_configured else "not configured")
    st.write("Gemini model:", settings.gemini_model)
    st.write("Embedding model:", settings.embedding_model)
    if settings.gemini_configured:
        st.caption("Gemini is ready for SQL and document question answering.")
    else:
        st.caption("Set GEMINI_API_KEY in .env before using LLM-backed agents.")


def _handle_tabular_upload(uploaded_file) -> None:
    try:
        loaded_table = load_tabular_file(
            file=uploaded_file,
            filename=uploaded_file.name,
        )
    except TabularLoadError as exc:
        st.session_state.pop(UPLOADED_TABLE, None)
        st.session_state[UPLOAD_ERROR] = str(exc)
        st.error(str(exc))
        return

    st.session_state[UPLOADED_TABLE] = loaded_table
    st.session_state.pop(UPLOAD_ERROR, None)
    st.success("File loaded successfully.")
    st.write("Rows:", loaded_table.row_count)
    st.write("Columns:", loaded_table.column_count)


def _handle_pdf_uploads(pdf_files, settings: Settings, signature: tuple) -> None:
    try:
        documents = [
            load_pdf_file(file=pdf_file, filename=pdf_file.name)
            for pdf_file in pdf_files
        ]
        chunks = [
            chunk
            for document in documents
            for chunk in chunk_document(document.text, document.filename)
        ]
        store = ChromaDocumentStore(
            persist_dir=settings.chroma_persist_dir,
            embedder=SentenceTransformerEmbedder(settings.embedding_model),
        )
        store.reset()
        store.add_chunks(chunks)
    except (PDFLoadError, EmbeddingError, ValueError) as exc:
        st.session_state.pop(DOCUMENT_RETRIEVER, None)
        st.session_state.pop(DOCUMENT_STATUS, None)
        st.session_state[DOCUMENT_ERROR] = str(exc)
        st.error(str(exc))
        return
    except Exception as exc:
        st.session_state.pop(DOCUMENT_RETRIEVER, None)
        st.session_state.pop(DOCUMENT_STATUS, None)
        st.session_state[DOCUMENT_ERROR] = f"Could not index PDF document(s): {exc}"
        st.error(st.session_state[DOCUMENT_ERROR])
        return

    st.session_state[DOCUMENT_RETRIEVER] = DocumentRetriever(store)
    st.session_state[DOCUMENT_STATUS] = {
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "filenames": [document.filename for document in documents],
    }
    st.session_state[DOCUMENT_SIGNATURE] = signature
    st.session_state.pop(DOCUMENT_ERROR, None)
    st.success(
        f"Indexed {len(documents)} PDF document(s) into {len(chunks)} chunk(s)."
    )


def _pdf_signature(pdf_files) -> tuple:
    return tuple((pdf_file.name, pdf_file.size) for pdf_file in pdf_files)
