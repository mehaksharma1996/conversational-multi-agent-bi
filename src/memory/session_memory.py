"""Session memory for deterministic conversational follow-up answers."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.agents.report_agent import BusinessReport
from src.analytics.anomaly_detection import AnomalyReport
from src.analytics.basic_analytics import AnalyticsReport
from src.charts.chart_builder import ChartSpec
from src.profiling.capability_detector import CapabilityReport
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass
class SessionMemory:
    """Compact, serializable-ish memory for the current Streamlit session."""

    row_count: int
    column_count: int
    mapped_fields: dict[str, str]
    missing_fields: list[str]
    available_capabilities: list[str]
    unavailable_capabilities: dict[str, str]
    analytics_highlights: list[str]
    anomaly_findings: list[str]
    chart_summaries: list[str]
    report_sections: dict[str, list[str]]
    document_summary: list[str]
    limitations: list[str]
    last_sql: str | None = None
    last_sql_question: str | None = None
    recent_questions: list[str] = field(default_factory=list)


def build_session_memory(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    capability_report: CapabilityReport,
    analytics_report: AnalyticsReport,
    anomaly_report: AnomalyReport,
    chart_specs: list[ChartSpec],
    business_report: BusinessReport,
    document_status: dict | None,
    previous_memory: SessionMemory | None = None,
) -> SessionMemory:
    """Build or refresh memory from the latest deterministic analysis outputs."""
    report_sections = {
        section.title: list(section.bullets)
        for section in business_report.sections
    }

    return SessionMemory(
        row_count=profile.row_count,
        column_count=profile.column_count,
        mapped_fields=schema_mapping.mapped_fields(),
        missing_fields=schema_mapping.missing_fields(),
        available_capabilities=capability_report.available_capabilities(),
        unavailable_capabilities={
            capability.name: capability.reason
            for capability in capability_report.capabilities
            if not capability.available
        },
        analytics_highlights=report_sections.get("Analytics Highlights", []),
        anomaly_findings=report_sections.get("Anomaly Findings", []),
        chart_summaries=[
            f"{chart.title} ({chart.chart_type}): {chart.description}"
            for chart in chart_specs
        ],
        report_sections=report_sections,
        document_summary=_document_summary(document_status),
        limitations=report_sections.get("Limitations", []),
        last_sql=previous_memory.last_sql if previous_memory else None,
        last_sql_question=previous_memory.last_sql_question if previous_memory else None,
        recent_questions=list(previous_memory.recent_questions) if previous_memory else [],
    )


def remember_question(
    memory: SessionMemory,
    question: str,
    sql: str | None = None,
) -> SessionMemory:
    """Record a user question and optional SQL generated for it."""
    recent_questions = [*memory.recent_questions, question][-10:]
    memory.recent_questions = recent_questions
    if sql:
        memory.last_sql = sql
        memory.last_sql_question = question
    return memory


def answer_from_memory(question: str, memory: SessionMemory | None) -> str | None:
    """Return a deterministic answer for supported session-memory questions."""
    if memory is None:
        return None

    lowered = question.lower()
    if _mentions_any(lowered, {"sql", "query"}) and _mentions_any(
        lowered,
        {"show", "used", "last", "generated", "previous"},
    ):
        return _last_sql_answer(memory)

    if _mentions_any(lowered, {"missing", "unavailable", "cannot", "limitation"}):
        return _missing_and_limitations_answer(memory)

    if _mentions_any(lowered, {"possible", "capability", "capabilities", "analysis"}):
        return _capability_answer(memory)

    if _mentions_any(lowered, {"risk", "risks", "anomaly", "anomalies", "flagged"}):
        return _risk_answer(memory)

    if _mentions_any(lowered, {"chart", "charts", "visual", "plot", "graph"}):
        return _chart_answer(memory)

    if _mentions_any(lowered, {"report", "summary", "summarize", "findings"}):
        return _report_answer(memory)

    if _mentions_any(lowered, {"document", "pdf", "source"}) and _mentions_any(
        lowered,
        {"uploaded", "indexed", "loaded"},
    ):
        return _document_answer(memory)

    return None


def _last_sql_answer(memory: SessionMemory) -> str:
    if not memory.last_sql:
        return "No SQL has been generated in this session yet."
    question = memory.last_sql_question or "the previous data question"
    return f"SQL used for '{question}':\n\n```sql\n{memory.last_sql}\n```"


def _missing_and_limitations_answer(memory: SessionMemory) -> str:
    lines = []
    if memory.missing_fields:
        lines.append("Missing canonical fields: " + ", ".join(memory.missing_fields) + ".")
    else:
        lines.append("No canonical fields are currently missing.")

    if memory.limitations:
        lines.append("Limitations:")
        lines.extend(f"- {limitation}" for limitation in memory.limitations)
    return "\n".join(lines)


def _capability_answer(memory: SessionMemory) -> str:
    lines = ["Available analysis paths:"]
    if memory.available_capabilities:
        lines.extend(f"- {capability}" for capability in memory.available_capabilities)
    else:
        lines.append("- None detected yet.")

    if memory.unavailable_capabilities:
        lines.append("Unavailable analysis paths:")
        lines.extend(
            f"- {name}: {reason}"
            for name, reason in memory.unavailable_capabilities.items()
        )
    return "\n".join(lines)


def _risk_answer(memory: SessionMemory) -> str:
    if not memory.anomaly_findings:
        return "No anomaly findings are stored in session memory yet."
    return "Top risk and anomaly context:\n" + "\n".join(
        f"- {finding}" for finding in memory.anomaly_findings[:8]
    )


def _chart_answer(memory: SessionMemory) -> str:
    if not memory.chart_summaries:
        return "No chart metadata is stored in session memory yet."
    return "Charts generated in this session:\n" + "\n".join(
        f"- {summary}" for summary in memory.chart_summaries
    )


def _report_answer(memory: SessionMemory) -> str:
    executive_summary = memory.report_sections.get("Executive Summary", [])
    if not executive_summary:
        return "No generated report summary is stored in session memory yet."
    return "Current report summary:\n" + "\n".join(
        f"- {bullet}" for bullet in executive_summary
    )


def _document_answer(memory: SessionMemory) -> str:
    if not memory.document_summary:
        return "No PDF document summary is stored in session memory yet."
    return "Document context:\n" + "\n".join(
        f"- {summary}" for summary in memory.document_summary
    )


def _document_summary(document_status: dict | None) -> list[str]:
    if not document_status:
        return []
    filenames = document_status.get("filenames", [])
    return [
        f"Indexed documents: {document_status.get('document_count', 0)}",
        f"Document chunks: {document_status.get('chunk_count', 0)}",
        "Files: " + (", ".join(filenames) if filenames else "Unknown"),
    ]


def _mentions_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)
