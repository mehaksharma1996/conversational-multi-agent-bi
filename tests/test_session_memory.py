"""Tests for deterministic session-memory follow-up answers."""

from __future__ import annotations

import pandas as pd

from src.agents.report_agent import BusinessReport, ReportSection
from src.analytics.anomaly_detection import detect_anomalies
from src.analytics.basic_analytics import run_basic_analytics
from src.memory.session_memory import (
    SessionMemory,
    answer_from_memory,
    build_session_memory,
    remember_question,
)
from src.profiling.capability_detector import detect_capabilities
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _memory() -> SessionMemory:
    return SessionMemory(
        row_count=10,
        column_count=5,
        mapped_fields={"amount": "amount", "date": "transaction_date"},
        missing_fields=["merchant"],
        available_capabilities=["Trend analysis", "Generic anomaly detection"],
        unavailable_capabilities={"Fraud-style analysis": "Missing merchant."},
        analytics_highlights=["Generated trend output with 5 period(s)."],
        anomaly_findings=["Flagged rows: 1", "Row 9 ranked 1: amount is high."],
        chart_summaries=["Amount Trend (line): Total mapped amount over time."],
        report_sections={
            "Executive Summary": ["Analyzed 10 rows across 5 columns."],
            "Limitations": ["Missing canonical fields: merchant"],
        },
        document_summary=["Indexed documents: 2"],
        limitations=["Missing canonical fields: merchant"],
    )


def test_answer_from_memory_returns_missing_fields() -> None:
    answer = answer_from_memory("What data columns were missing?", _memory())

    assert answer is not None
    assert "merchant" in answer


def test_answer_from_memory_returns_capabilities() -> None:
    answer = answer_from_memory("What analysis was possible?", _memory())

    assert answer is not None
    assert "Trend analysis" in answer
    assert "Fraud-style analysis" in answer


def test_answer_from_memory_returns_last_sql_after_question_is_remembered() -> None:
    memory = remember_question(
        _memory(),
        question="Show top merchants",
        sql="SELECT merchant FROM uploaded_data LIMIT 5",
    )

    answer = answer_from_memory("Show me the SQL used", memory)

    assert answer is not None
    assert "SELECT merchant" in answer


def test_answer_from_memory_returns_none_for_unrecognized_question() -> None:
    assert answer_from_memory("What's the weather like?", _memory()) is None


def test_answer_from_memory_returns_none_without_memory() -> None:
    assert answer_from_memory("What is possible?", None) is None


def test_last_sql_answer_before_any_question_is_remembered() -> None:
    answer = answer_from_memory("Show me the SQL used", _memory())

    assert answer == "No SQL has been generated in this session yet."


def test_answer_from_memory_returns_risk_and_anomaly_findings() -> None:
    answer = answer_from_memory("What risks were flagged?", _memory())

    assert answer is not None
    assert "Flagged rows: 1" in answer


def test_answer_from_memory_reports_no_anomaly_findings_yet() -> None:
    memory = _memory()
    memory.anomaly_findings = []

    answer = answer_from_memory("Any anomalies flagged?", memory)

    assert answer == "No anomaly findings are stored in session memory yet."


def test_answer_from_memory_returns_chart_summaries() -> None:
    answer = answer_from_memory("What charts were generated?", _memory())

    assert answer is not None
    assert "Amount Trend" in answer


def test_answer_from_memory_reports_no_charts_yet() -> None:
    memory = _memory()
    memory.chart_summaries = []

    answer = answer_from_memory("Show me the charts", memory)

    assert answer == "No chart metadata is stored in session memory yet."


def test_answer_from_memory_returns_report_summary() -> None:
    answer = answer_from_memory("Summarize the report", _memory())

    assert answer is not None
    assert "Analyzed 10 rows" in answer


def test_answer_from_memory_reports_no_report_summary_yet() -> None:
    memory = _memory()
    memory.report_sections = {}

    answer = answer_from_memory("Give me the summary", memory)

    assert answer == "No generated report summary is stored in session memory yet."


def test_answer_from_memory_returns_document_summary() -> None:
    answer = answer_from_memory("What documents were uploaded and indexed?", _memory())

    assert answer is not None
    assert "Indexed documents: 2" in answer


def test_answer_from_memory_reports_no_documents_yet() -> None:
    memory = _memory()
    memory.document_summary = []

    answer = answer_from_memory("What PDF sources were loaded?", memory)

    assert answer == "No PDF document summary is stored in session memory yet."


def test_missing_fields_answer_when_nothing_is_missing() -> None:
    memory = _memory()
    memory.missing_fields = []

    answer = answer_from_memory("Any limitations?", memory)

    assert answer is not None
    assert "No canonical fields are currently missing." in answer


def test_capability_answer_when_nothing_is_available() -> None:
    memory = _memory()
    memory.available_capabilities = []
    memory.unavailable_capabilities = {}

    answer = answer_from_memory("What analysis is possible?", memory)

    assert answer is not None
    assert "None detected yet." in answer


def test_remember_question_caps_recent_questions_at_ten() -> None:
    memory = _memory()
    for i in range(15):
        memory = remember_question(memory, question=f"Question {i}")

    assert len(memory.recent_questions) == 10
    assert memory.recent_questions[-1] == "Question 14"
    assert memory.recent_questions[0] == "Question 5"


def _build_real_session_memory(document_status: dict | None = None, previous_memory=None):
    dataframe = pd.DataFrame(
        {
            "transaction_date": [
                "2026-01-01",
                "2026-01-02",
                "2026-01-03",
                "2026-01-04",
                "2026-01-05",
                "2026-01-06",
                "2026-01-07",
                "2026-01-08",
            ],
            "amount": [10, 12, 11, 13, 12, 14, 11, 999],
            "merchant": ["A", "A", "B", "A", "B", "A", "B", "Z"],
        }
    )
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    capabilities = detect_capabilities(profile, mapping)
    analytics = run_basic_analytics(dataframe, profile, mapping)
    anomalies = detect_anomalies(dataframe, profile, mapping)
    business_report = BusinessReport(
        title="Business Intelligence Report",
        sections=[
            ReportSection(title="Executive Summary", bullets=["Analyzed 8 rows."]),
            ReportSection(title="Analytics Highlights", bullets=["Trend detected."]),
            ReportSection(title="Anomaly Findings", bullets=["Flagged rows: 1"]),
            ReportSection(title="Limitations", bullets=["Missing canonical fields: date"]),
        ],
    )
    return build_session_memory(
        profile=profile,
        schema_mapping=mapping,
        capability_report=capabilities,
        analytics_report=analytics,
        anomaly_report=anomalies,
        chart_specs=[],
        business_report=business_report,
        document_status=document_status,
        previous_memory=previous_memory,
    )


def test_build_session_memory_populates_fields_from_analysis_outputs() -> None:
    memory = _build_real_session_memory()

    assert memory.row_count == 8
    assert memory.column_count == 3
    assert memory.analytics_highlights == ["Trend detected."]
    assert memory.anomaly_findings == ["Flagged rows: 1"]
    assert memory.limitations == ["Missing canonical fields: date"]
    assert memory.last_sql is None
    assert memory.recent_questions == []


def test_build_session_memory_includes_document_summary_when_present() -> None:
    memory = _build_real_session_memory(
        document_status={
            "document_count": 2,
            "chunk_count": 9,
            "filenames": ["policy.pdf", "playbook.pdf"],
        }
    )

    assert "Indexed documents: 2" in memory.document_summary
    assert "Files: policy.pdf, playbook.pdf" in memory.document_summary


def test_build_session_memory_has_no_document_summary_without_status() -> None:
    memory = _build_real_session_memory(document_status=None)

    assert memory.document_summary == []


def test_build_session_memory_carries_forward_previous_conversation_state() -> None:
    previous = _build_real_session_memory()
    previous = remember_question(previous, "Show top merchants", sql="SELECT 1")

    refreshed = _build_real_session_memory(previous_memory=previous)

    assert refreshed.last_sql == "SELECT 1"
    assert refreshed.last_sql_question == "Show top merchants"
    assert refreshed.recent_questions == ["Show top merchants"]
