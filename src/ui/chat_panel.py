"""Conversational SQL question panel."""

from __future__ import annotations

import logging
from io import BytesIO

import pandas as pd
import plotly.express as px
import streamlit as st

from config.settings import Settings
from src.agents.rag_agent import RAGAgentError
from src.agents.sql_agent import SQLAgentError
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
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError

LOGGER = logging.getLogger(__name__)


def render_chat_panel(settings: Settings) -> None:
    st.subheader("Conversation")

    if CHAT_MESSAGES not in st.session_state:
        st.session_state[CHAT_MESSAGES] = [
            {
                "role": "assistant",
                "content": (
                    "Ask about uploaded data, document guidance, or both. The "
                    "workbench will show which specialized route handled the question."
                ),
            }
        ]

    for message_index, message in enumerate(st.session_state[CHAT_MESSAGES]):
        with st.chat_message(message["role"]):
            st.write(message["content"])
            if message.get("route"):
                st.caption(f"Workflow route: {message['route']}")
            if message.get("sql"):
                st.code(message["sql"], language="sql")
            if message.get("dataframe") is not None:
                dataframe = message["dataframe"]
                st.dataframe(dataframe, use_container_width=True)
                figure = _build_result_chart(dataframe)
                if figure is not None:
                    st.plotly_chart(figure, use_container_width=True)
                _render_result_downloads(dataframe, message_index=message_index)
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
    has_memory = SESSION_MEMORY in st.session_state
    if not settings.gemini_configured and not has_memory:
        return "Set GEMINI_API_KEY in .env before asking LLM-backed questions."
    has_data = UPLOADED_TABLE in st.session_state and STORED_TABLE in st.session_state
    has_documents = DOCUMENT_RETRIEVER in st.session_state
    if not has_data and not has_documents and not has_memory:
        return "Upload CSV/Excel data or PDF documents before asking questions."
    return None


def _handle_question(prompt: str, settings: Settings) -> None:
    st.session_state[CHAT_MESSAGES].append({"role": "user", "content": prompt})

    try:
        with st.spinner("Analyzing your question..."):
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
                "route": result.route,
            }
        )
    except (
        LLMConfigurationError,
        LLMGenerationError,
        SQLAgentError,
        RAGAgentError,
        UnsafeQueryError,
        QueryTimeoutError,
        ValueError,
    ) as exc:
        st.session_state[CHAT_MESSAGES].append(
            {"role": "assistant", "content": f"I could not answer safely: {exc}"}
        )
        st.rerun()
        return
    except Exception as exc:
        LOGGER.exception("Unexpected conversational workflow failure", exc_info=exc)
        st.session_state[CHAT_MESSAGES].append(
            {
                "role": "assistant",
                "content": (
                    "The workflow encountered an unexpected internal error. "
                    "Please review the server logs and try again."
                ),
            }
        )
        st.rerun()
        return

    st.rerun()


def _build_result_chart(dataframe: pd.DataFrame):
    if dataframe.empty or len(dataframe.columns) < 2:
        return None
    numeric_columns = list(dataframe.select_dtypes(include="number").columns)
    if not numeric_columns:
        return None
    measure = numeric_columns[0]
    dimensions = [column for column in dataframe.columns if column != measure]
    if not dimensions:
        return None
    dimension = dimensions[0]

    parsed_dates = pd.to_datetime(dataframe[dimension], errors="coerce", format="mixed")
    if parsed_dates.notna().mean() >= 0.8:
        chart_data = dataframe[[dimension, measure]].copy()
        chart_data[dimension] = parsed_dates
        return px.line(chart_data.sort_values(dimension), x=dimension, y=measure, markers=True)

    if dataframe[dimension].nunique(dropna=False) <= 30:
        chart_data = dataframe[[dimension, measure]].head(100)
        return px.bar(chart_data, x=dimension, y=measure)
    return None


def _render_result_downloads(dataframe: pd.DataFrame, message_index: int) -> None:
    csv_bytes = dataframe.to_csv(index=False).encode("utf-8")
    excel_buffer = BytesIO()
    dataframe.to_excel(excel_buffer, index=False)
    csv_column, excel_column = st.columns(2)
    csv_column.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="query_result.csv",
        mime="text/csv",
        key=f"query_csv_{message_index}",
    )
    excel_column.download_button(
        "Download Excel",
        data=excel_buffer.getvalue(),
        file_name="query_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"query_excel_{message_index}",
    )
