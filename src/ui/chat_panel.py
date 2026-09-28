"""Conversational SQL question panel."""

from __future__ import annotations

import streamlit as st

from config.settings import Settings
from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.llm.gemini_client import build_gemini_client
from src.memory.session_keys import (
    CHAT_MESSAGES,
    DOCUMENT_RETRIEVER,
    SESSION_MEMORY,
    STORED_TABLE,
    UPLOADED_TABLE,
)
from src.memory.session_memory import remember_question
from src.orchestration.langgraph_orchestrator import QuestionOrchestrator


def render_chat_panel(settings: Settings) -> None:
    st.subheader("Conversation")

    if CHAT_MESSAGES not in st.session_state:
        st.session_state[CHAT_MESSAGES] = [
            {
                "role": "assistant",
                "content": (
                    "Ask a data question for safe read-only SQL, or ask a document "
                    "question using the uploaded PDFs."
                ),
            }
        ]

    for message in st.session_state[CHAT_MESSAGES]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
            if message.get("sql"):
                st.code(message["sql"], language="sql")
            if message.get("dataframe") is not None:
                st.dataframe(message["dataframe"], use_container_width=True)
            if message.get("sources"):
                with st.expander("Retrieved document sources", expanded=False):
                    for source in message["sources"]:
                        st.write(source)

    disabled_reason = _disabled_reason(settings)
    prompt = st.chat_input(
        "Ask a business question",
        disabled=disabled_reason is not None,
    )

    if disabled_reason:
        st.caption(disabled_reason)
        return

    if prompt:
        _handle_question(prompt=prompt, settings=settings)


def _disabled_reason(settings: Settings) -> str | None:
    if not settings.gemini_configured:
        return "Set GEMINI_API_KEY in .env before asking LLM-backed questions."
    has_data = UPLOADED_TABLE in st.session_state and STORED_TABLE in st.session_state
    has_documents = DOCUMENT_RETRIEVER in st.session_state
    if not has_data and not has_documents:
        return "Upload CSV/Excel data or PDF documents before asking questions."
    return None


def _handle_question(prompt: str, settings: Settings) -> None:
    st.session_state[CHAT_MESSAGES].append({"role": "user", "content": prompt})

    try:
        llm_client = build_gemini_client(settings)
        orchestrator = QuestionOrchestrator(
            llm_client=llm_client,
            stored_table=st.session_state.get(STORED_TABLE),
            document_retriever=st.session_state.get(DOCUMENT_RETRIEVER),
            session_memory=st.session_state.get(SESSION_MEMORY),
        )
        result = orchestrator.answer(prompt)
        if SESSION_MEMORY in st.session_state:
            st.session_state[SESSION_MEMORY] = remember_question(
                memory=st.session_state[SESSION_MEMORY],
                question=prompt,
                sql=result.sql,
            )
        st.session_state[CHAT_MESSAGES].append(
            {
                "role": "assistant",
                "content": result.answer,
                "sql": result.sql,
                "dataframe": result.dataframe,
                "sources": result.sources,
            }
        )
    except (LLMConfigurationError, LLMGenerationError, ValueError) as exc:
        st.session_state[CHAT_MESSAGES].append(
            {"role": "assistant", "content": f"I could not answer safely: {exc}"}
        )
        st.rerun()
        return
    except Exception as exc:
        st.session_state[CHAT_MESSAGES].append(
            {"role": "assistant", "content": f"The query failed: {exc}"}
        )
        st.rerun()
        return

    st.rerun()
