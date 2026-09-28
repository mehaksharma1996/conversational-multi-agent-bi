"""Tests for deterministic session-memory follow-up answers."""

from __future__ import annotations

from src.memory.session_memory import (
    SessionMemory,
    answer_from_memory,
    remember_question,
)


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
