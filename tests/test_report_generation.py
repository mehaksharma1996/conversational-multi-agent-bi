"""Tests for business report generation and PDF export."""

from __future__ import annotations

import pandas as pd

from src.agents.report_agent import (
    BusinessReport,
    ReportSection,
    generate_business_report,
    report_to_markdown,
)
from src.analytics.anomaly_detection import detect_anomalies, recommend_anomaly_features
from src.analytics.basic_analytics import run_basic_analytics
from src.analytics.pipeline import build_analysis_bundle
from src.charts.chart_builder import build_charts
from src.profiling.capability_detector import detect_capabilities
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema
from src.reporting.pdf_report import build_report_pdf


def _sample_report_and_charts():
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
            "city": [
                "Toronto",
                "Toronto",
                "Montreal",
                "Toronto",
                "Montreal",
                "Toronto",
                "Montreal",
                "Vancouver",
            ],
            "label": [
                "normal",
                "normal",
                "normal",
                "normal",
                "normal",
                "normal",
                "normal",
                "review",
            ],
        }
    )
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    capabilities = detect_capabilities(profile, mapping)
    analytics = run_basic_analytics(dataframe, profile, mapping)
    anomalies = detect_anomalies(dataframe, profile, mapping)
    charts = build_charts(dataframe, profile, mapping, analytics, anomalies)

    report = generate_business_report(
        profile=profile,
        schema_mapping=mapping,
        capability_report=capabilities,
        analytics_report=analytics,
        anomaly_report=anomalies,
        chart_specs=charts,
        document_status={
            "document_count": 2,
            "chunk_count": 5,
            "filenames": ["policy.pdf", "playbook.pdf"],
        },
    )
    return report, charts


def _sample_report():
    report, _ = _sample_report_and_charts()
    return report


def test_generate_business_report_contains_expected_sections() -> None:
    report = _sample_report()

    section_titles = [section.title for section in report.sections]

    assert report.title == "Business Intelligence Analysis Report"
    assert "Executive Summary" in section_titles
    assert "Dataset Overview" in section_titles
    assert "Capability Detection" in section_titles
    assert "Anomaly Findings" in section_titles
    assert "Document Context" in section_titles


def test_report_to_markdown_renders_sections() -> None:
    markdown = report_to_markdown(_sample_report())

    assert markdown.startswith("# Business Intelligence Analysis Report")
    assert "## Executive Summary" in markdown
    assert "- Analyzed 8 rows across 5 columns." in markdown
    assert "policy.pdf" in markdown


def test_build_report_pdf_returns_pdf_bytes() -> None:
    pdf_bytes = build_report_pdf(_sample_report())

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 1000


def test_build_report_pdf_can_embed_chart_images() -> None:
    report, charts = _sample_report_and_charts()

    pdf_without_charts = build_report_pdf(report)
    pdf_with_charts = build_report_pdf(report, chart_specs=charts[:1])

    assert pdf_with_charts.startswith(b"%PDF")
    assert len(pdf_with_charts) > len(pdf_without_charts)


def test_build_report_pdf_renders_non_latin1_text() -> None:
    """Helvetica (WinAnsi/Latin-1) cannot encode Cyrillic or Greek text; the
    registered Unicode-capable font must be used instead or this raises.
    """
    report = BusinessReport(
        title="Отчёт о транзакциях",
        sections=[
            ReportSection(
                title="Σύνοψη",
                body="Обзор данных за период. Στατιστικά στοιχεία.",
                bullets=["Категория: Επισκόπηση", "Валюта: €"],
            )
        ],
    )

    pdf_bytes = build_report_pdf(report)

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 1000


def _bundle_with_more_flags_than_the_shortlist():
    amounts = [50 + ((index * 37) % 101) * 0.9 + (index % 7) * 0.13 for index in range(400)]
    dataframe = pd.DataFrame(
        {"amount": amounts, "merchant": [f"M{index % 9}" for index in range(400)]}
    )
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)
    return build_analysis_bundle(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=mapping,
        anomaly_features=recommend_anomaly_features(profile, mapping),
        anomaly_contamination=0.1,
        document_status=None,
    )


def test_report_states_the_unique_flagged_total_and_the_capped_shortlist() -> None:
    bundle = _bundle_with_more_flags_than_the_shortlist()
    anomaly = bundle.anomaly_report
    assert (anomaly.total_flagged_count, anomaly.flagged_count) == (40, 20)

    sections = {section.title: section.bullets for section in bundle.business_report.sections}
    summary = " ".join(sections["Executive Summary"])
    assert "Flagged 40 unique row(s) for anomaly review (40 by the model, 0 by rules;" in summary
    assert "a row can be flagged by both" in summary
    assert "Review shortlist: the top 20 of 40 flagged row(s) by anomaly score." in summary
    assert "Flagged 20" not in summary

    findings = sections["Anomaly Findings"]
    assert "Unique rows flagged (model or rules): 40" in findings
    assert (
        "Flagged by the model: 40; flagged by rules: 0 (a row can be flagged by both)." in findings
    )
    assert "Review shortlist: the top 20 of 40 flagged row(s) by anomaly score." in findings
    assert not any(bullet.startswith("Flagged rows:") for bullet in findings)
