"""Deterministic business report generation."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.analytics.anomaly_detection import AnomalyReport
from src.analytics.basic_analytics import AnalyticsReport
from src.charts.chart_builder import ChartSpec
from src.profiling.capability_detector import CapabilityReport
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass(frozen=True)
class ReportSection:
    title: str
    bullets: list[str] = field(default_factory=list)
    body: str | None = None


@dataclass(frozen=True)
class BusinessReport:
    title: str
    sections: list[ReportSection]


def generate_business_report(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    capability_report: CapabilityReport,
    analytics_report: AnalyticsReport,
    anomaly_report: AnomalyReport,
    chart_specs: list[ChartSpec],
    document_status: dict | None = None,
) -> BusinessReport:
    """Build a deterministic report from existing analysis outputs."""
    sections = [
        _executive_summary(profile, capability_report, anomaly_report),
        _dataset_overview(profile),
        _schema_section(schema_mapping),
        _capabilities_section(capability_report),
        _analytics_section(analytics_report),
        _anomaly_section(anomaly_report),
        _charts_section(chart_specs),
        _documents_section(document_status),
        _limitations_section(schema_mapping, analytics_report, anomaly_report),
    ]

    return BusinessReport(
        title="Business Intelligence Analysis Report",
        sections=sections,
    )


def report_to_markdown(report: BusinessReport) -> str:
    """Render a business report as Markdown text."""
    lines = [f"# {report.title}", ""]
    for section in report.sections:
        lines.extend([f"## {section.title}", ""])
        if section.body:
            lines.extend([section.body, ""])
        for bullet in section.bullets:
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _executive_summary(
    profile: DataProfile,
    capability_report: CapabilityReport,
    anomaly_report: AnomalyReport,
) -> ReportSection:
    available = capability_report.available_capabilities()
    bullets = [
        f"Analyzed {profile.row_count:,} rows across {profile.column_count:,} columns.",
        f"Detected {len(available)} available analysis capability path(s).",
        f"Flagged {anomaly_report.flagged_count:,} potential anomalous row(s).",
    ]
    if profile.duplicate_row_count:
        bullets.append(f"Found {profile.duplicate_row_count:,} duplicate row(s).")
    return ReportSection(title="Executive Summary", bullets=bullets)


def _dataset_overview(profile: DataProfile) -> ReportSection:
    return ReportSection(
        title="Dataset Overview",
        bullets=[
            f"Rows: {profile.row_count:,}",
            f"Columns: {profile.column_count:,}",
            f"Numeric columns: {len(profile.numeric_columns)}",
            f"Date-like columns: {len(profile.date_columns)}",
            f"Categorical columns: {len(profile.categorical_columns)}",
            f"Possible ID columns: {', '.join(profile.possible_id_columns) or 'None detected'}",
            "Possible label columns: "
            f"{', '.join(profile.possible_label_columns) or 'None detected'}",
        ],
    )


def _schema_section(schema_mapping: SchemaMapping) -> ReportSection:
    bullets = []
    for canonical_field, mapping in schema_mapping.mappings.items():
        source = mapping.source_column or "Not found"
        bullets.append(
            f"{canonical_field}: {source} (confidence {mapping.confidence:.2f}; {mapping.reason})"
        )
    return ReportSection(title="Canonical Schema Mapping", bullets=bullets)


def _capabilities_section(capability_report: CapabilityReport) -> ReportSection:
    bullets = [
        f"{capability.name}: "
        f"{'Available' if capability.available else 'Unavailable'} - "
        f"{capability.reason}"
        for capability in capability_report.capabilities
    ]
    return ReportSection(title="Capability Detection", bullets=bullets)


def _analytics_section(analytics_report: AnalyticsReport) -> ReportSection:
    bullets = []
    if not analytics_report.numeric_summary.empty:
        for _, row in analytics_report.numeric_summary.head(3).iterrows():
            bullets.append(
                f"{row['column']}: total {row['mean'] * row['count']:,.2f}, "
                f"average {row['mean']:,.2f}, range {row['min']:,.2f} to {row['max']:,.2f}."
            )
    if analytics_report.categorical_breakdowns:
        for column, breakdown in list(analytics_report.categorical_breakdowns.items())[:3]:
            if not breakdown.empty:
                top = breakdown.iloc[0]
                bullets.append(f"Top {column}: {top[column]} with {int(top['count']):,} row(s).")
    if analytics_report.amount_by_category:
        for column, breakdown in list(analytics_report.amount_by_category.items())[:3]:
            if breakdown.empty:
                continue
            top = breakdown.iloc[0]
            total = float(breakdown["sum"].sum())
            concentration = float(top["sum"]) / total if total else 0.0
            bullets.append(
                f"Largest {column} by amount: {top[column]} at {float(top['sum']):,.2f} "
                f"({concentration:.1%} of the displayed top groups)."
            )
    if analytics_report.trend is not None:
        trend = analytics_report.trend
        if len(trend) >= 2:
            previous = float(trend.iloc[-2]["sum"])
            latest = float(trend.iloc[-1]["sum"])
            change = (latest - previous) / abs(previous) if previous else 0.0
            bullets.append(
                f"Latest period total was {latest:,.2f}, a {change:+.1%} change "
                "from the prior period."
            )
        elif not trend.empty:
            bullets.append(
                f"Observed one period with a total of {float(trend.iloc[0]['sum']):,.2f}."
            )
    if not bullets:
        bullets.append("No analytics outputs were available.")
    return ReportSection(title="Analytics Highlights", bullets=bullets)


def _anomaly_section(anomaly_report: AnomalyReport) -> ReportSection:
    bullets = [
        f"Method: {anomaly_report.method}",
        f"Feature columns: {', '.join(anomaly_report.feature_columns) or 'None'}",
        f"Flagged rows: {anomaly_report.flagged_count:,}",
    ]
    if not anomaly_report.flagged_rows.empty:
        top_rows = anomaly_report.flagged_rows.head(5)
        for _, row in top_rows.iterrows():
            bullets.append(f"Row {row['source_row']} ranked {row['anomaly_rank']}: {row['reason']}")
    return ReportSection(title="Anomaly Findings", bullets=bullets)


def _charts_section(chart_specs: list[ChartSpec]) -> ReportSection:
    if not chart_specs:
        return ReportSection(
            title="Chart Inventory",
            bullets=["No charts were generated for this dataset."],
        )
    return ReportSection(
        title="Chart Inventory",
        bullets=[
            f"{chart.title} ({chart.chart_type}): {chart.description}" for chart in chart_specs
        ],
    )


def _documents_section(document_status: dict | None) -> ReportSection:
    if not document_status:
        return ReportSection(
            title="Document Context",
            bullets=["No PDF documents were indexed for this session."],
        )

    filenames = ", ".join(document_status.get("filenames", []))
    return ReportSection(
        title="Document Context",
        bullets=[
            f"Indexed documents: {document_status.get('document_count', 0)}",
            f"Document chunks: {document_status.get('chunk_count', 0)}",
            f"Files: {filenames or 'Unknown'}",
        ],
    )


def _limitations_section(
    schema_mapping: SchemaMapping,
    analytics_report: AnalyticsReport,
    anomaly_report: AnomalyReport,
) -> ReportSection:
    bullets = []
    missing_fields = schema_mapping.missing_fields()
    if missing_fields:
        bullets.append("Missing canonical fields: " + ", ".join(missing_fields))
    bullets.extend(analytics_report.limitations)
    bullets.extend(anomaly_report.limitations)
    if not bullets:
        bullets.append("No major deterministic limitations were detected.")
    return ReportSection(title="Limitations", bullets=bullets)
