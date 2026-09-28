"""Main dashboard for uploaded data preview."""

from __future__ import annotations

import streamlit as st

from config.settings import Settings
from src.analytics.anomaly_detection import AnomalyReport, detect_anomalies
from src.analytics.basic_analytics import AnalyticsReport, run_basic_analytics
from src.agents.report_agent import BusinessReport, generate_business_report, report_to_markdown
from src.charts.chart_builder import ChartSpec, build_charts
from src.memory.session_keys import (
    DOCUMENT_STATUS,
    SESSION_MEMORY,
    STORED_TABLE,
    UPLOADED_TABLE,
    UPLOAD_ERROR,
)
from src.memory.session_memory import SessionMemory, build_session_memory
from src.profiling.capability_detector import (
    CapabilityReport,
    capability_report_to_dataframe,
    detect_capabilities,
)
from src.profiling.data_profiler import DataProfile, profile_dataframe, profile_to_dataframe
from src.profiling.schema_mapper import (
    SchemaMapping,
    map_schema,
    schema_mapping_to_dataframe,
)
from src.reporting.pdf_report import build_report_pdf
from src.storage.query_executor import UnsafeQueryError, execute_read_query
from src.storage.sqlite_store import DEFAULT_TABLE_NAME, SQLiteStore, StoredTable


def render_dashboard(settings: Settings) -> None:
    st.subheader("Data Preview")

    if UPLOAD_ERROR in st.session_state:
        st.warning("Fix the upload issue in the sidebar before continuing.")
        return

    loaded_table = st.session_state.get(UPLOADED_TABLE)
    if loaded_table is None:
        _render_empty_state()
        return

    dataframe = loaded_table.dataframe
    stored_table = _store_uploaded_dataframe(settings, dataframe)
    profile = profile_dataframe(dataframe)
    schema_mapping = map_schema(profile)
    capability_report = detect_capabilities(profile, schema_mapping)
    analytics_report = run_basic_analytics(dataframe, profile, schema_mapping)
    anomaly_report = detect_anomalies(dataframe, profile, schema_mapping)
    chart_specs = build_charts(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=schema_mapping,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
    )
    business_report = generate_business_report(
        profile=profile,
        schema_mapping=schema_mapping,
        capability_report=capability_report,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
        chart_specs=chart_specs,
        document_status=st.session_state.get(DOCUMENT_STATUS),
    )
    st.session_state[SESSION_MEMORY] = build_session_memory(
        profile=profile,
        schema_mapping=schema_mapping,
        capability_report=capability_report,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
        chart_specs=chart_specs,
        business_report=business_report,
        document_status=st.session_state.get(DOCUMENT_STATUS),
        previous_memory=st.session_state.get(SESSION_MEMORY),
    )
    st.success(
        f"Loaded {loaded_table.filename} with "
        f"{loaded_table.row_count:,} rows and {loaded_table.column_count:,} columns."
    )

    metric_cols = st.columns(4)
    metric_cols[0].metric("Rows", f"{loaded_table.row_count:,}")
    metric_cols[1].metric("Columns", f"{loaded_table.column_count:,}")
    metric_cols[2].metric("Duplicate rows", f"{profile.duplicate_row_count:,}")
    metric_cols[3].metric("File type", loaded_table.extension.replace(".", "").upper())

    _render_workflow_status(
        capability_report=capability_report,
        chart_specs=chart_specs,
        anomaly_report=anomaly_report,
        session_memory=st.session_state.get(SESSION_MEMORY),
        document_status=st.session_state.get(DOCUMENT_STATUS),
    )

    preview_tab, profile_tab, capabilities_tab, analytics_tab, charts_tab, anomaly_tab, report_tab, sql_tab = st.tabs(
        [
            "Preview",
            "Profile",
            "Capabilities",
            "Analytics",
            "Charts",
            "Anomalies",
            "Report",
            "SQL",
        ]
    )

    with preview_tab:
        st.write("First 25 rows")
        st.dataframe(dataframe.head(25), use_container_width=True)

    with profile_tab:
        _render_profile_summary(profile)
        st.write("Column profile")
        st.dataframe(profile_to_dataframe(profile), use_container_width=True)

    with capabilities_tab:
        _render_schema_mapping(schema_mapping)
        _render_capability_report(capability_report)

    with analytics_tab:
        _render_analytics_report(analytics_report)

    with charts_tab:
        _render_charts(chart_specs)

    with anomaly_tab:
        _render_anomaly_report(anomaly_report)

    with report_tab:
        _render_business_report(business_report, chart_specs)

    with sql_tab:
        _render_sql_workspace(stored_table)

    st.success("Workflow ready: analytics, charts, report export, SQL, RAG, and follow-up memory are available when their inputs are present.")


def _render_empty_state() -> None:
    st.info("Upload a CSV or Excel file from the sidebar to start the BI workflow.")
    st.write("Available workflow once data is uploaded:")
    st.write("1. Data preview, profiling, schema mapping, and capability detection")
    st.write("2. Analytics, anomaly detection, charts, report generation, and safe SQL")
    st.write("3. Optional PDF RAG and conversational follow-up memory")


def _render_workflow_status(
    capability_report: CapabilityReport,
    chart_specs: list[ChartSpec],
    anomaly_report: AnomalyReport,
    session_memory: SessionMemory | None,
    document_status: dict | None,
) -> None:
    st.write("Workflow status")
    status_cols = st.columns(6)
    status_cols[0].metric(
        "Capabilities",
        len(capability_report.available_capabilities()),
        help="Analysis paths currently available from detected columns.",
    )
    status_cols[1].metric(
        "Charts",
        len(chart_specs),
        help="Plotly charts generated from analytics outputs.",
    )
    status_cols[2].metric(
        "Anomalies",
        anomaly_report.flagged_count,
        help="Rows flagged by the current anomaly detector.",
    )
    status_cols[3].metric(
        "PDF Chunks",
        document_status.get("chunk_count", 0) if document_status else 0,
        help="Indexed document chunks available for RAG questions.",
    )
    status_cols[4].metric(
        "Memory",
        "Ready" if session_memory else "Pending",
        help="Session memory powers follow-up questions about analysis outputs.",
    )
    status_cols[5].metric(
        "Report",
        "Ready",
        help="A deterministic report and PDF export are available in the Report tab.",
    )


def _render_profile_summary(profile: DataProfile) -> None:
    summary_cols = st.columns(5)
    summary_cols[0].metric("Numeric", len(profile.numeric_columns))
    summary_cols[1].metric("Date-like", len(profile.date_columns))
    summary_cols[2].metric("Categorical", len(profile.categorical_columns))
    summary_cols[3].metric("Possible IDs", len(profile.possible_id_columns))
    summary_cols[4].metric("Possible labels", len(profile.possible_label_columns))

    st.write("Detected column groups")
    groups = {
        "Numeric": profile.numeric_columns,
        "Date-like": profile.date_columns,
        "Categorical": profile.categorical_columns,
        "Boolean": profile.boolean_columns,
        "Possible IDs": profile.possible_id_columns,
        "Possible labels": profile.possible_label_columns,
    }

    for label, columns in groups.items():
        value = ", ".join(columns) if columns else "None detected"
        st.write(f"**{label}:** {value}")


def _render_schema_mapping(schema_mapping: SchemaMapping) -> None:
    st.write("Canonical schema mapping")
    st.dataframe(schema_mapping_to_dataframe(schema_mapping), use_container_width=True)

    missing = schema_mapping.missing_fields()
    if missing:
        st.warning("Missing canonical fields: " + ", ".join(missing))
    else:
        st.success("All canonical fields were mapped.")


def _render_capability_report(report: CapabilityReport) -> None:
    st.write("Detected capabilities")
    st.dataframe(capability_report_to_dataframe(report), use_container_width=True)

    available = report.available_capabilities()
    if available:
        st.success("Available: " + ", ".join(available))
    else:
        st.warning("No analysis capabilities are available yet for this dataset.")


def _render_analytics_report(report: AnalyticsReport) -> None:
    st.write("Dataset summary")
    summary_cols = st.columns(6)
    for index, (label, value) in enumerate(report.dataset_summary.items()):
        summary_cols[index].metric(label.replace("_", " ").title(), f"{value:,}")

    st.write("Numeric summary")
    if report.numeric_summary.empty:
        st.info("No numeric summary is available.")
    else:
        st.dataframe(report.numeric_summary, use_container_width=True)

    st.write("Categorical breakdowns")
    if report.categorical_breakdowns:
        for column, breakdown in report.categorical_breakdowns.items():
            with st.expander(f"Top values: {column}", expanded=False):
                st.dataframe(breakdown, use_container_width=True)
    else:
        st.info("No categorical breakdowns are available.")

    st.write("Amount by category")
    if report.amount_by_category:
        for column, breakdown in report.amount_by_category.items():
            with st.expander(f"Amount by {column}", expanded=False):
                st.dataframe(breakdown, use_container_width=True)
    else:
        st.info("Amount-by-category analysis is not available.")

    st.write("Trend")
    if report.trend is not None:
        st.dataframe(report.trend, use_container_width=True)
    else:
        st.info("Trend analysis is not available.")

    if report.limitations:
        st.write("Limitations")
        for limitation in report.limitations:
            st.warning(limitation)


def _render_charts(chart_specs: list[ChartSpec]) -> None:
    st.write("Generated charts")
    if not chart_specs:
        st.info("No charts are available for this dataset yet.")
        return

    for chart in chart_specs:
        st.write(chart.title)
        st.caption(chart.description)
        st.plotly_chart(chart.figure, use_container_width=True)
        with st.expander("Chart metadata", expanded=False):
            st.json(chart.metadata)


def _render_anomaly_report(report: AnomalyReport) -> None:
    st.write("Anomaly detection")
    metric_cols = st.columns(3)
    metric_cols[0].metric("Enabled", "Yes" if report.enabled else "No")
    metric_cols[1].metric("Flagged rows", report.flagged_count)
    metric_cols[2].metric("Features", len(report.feature_columns))

    st.write("Feature columns")
    st.write(", ".join(report.feature_columns) if report.feature_columns else "None")

    if report.flagged_rows.empty:
        st.info("No flagged rows to show.")
    else:
        st.dataframe(report.flagged_rows, use_container_width=True)

    if report.limitations:
        st.write("Limitations")
        for limitation in report.limitations:
            st.warning(limitation)


def _render_business_report(
    report: BusinessReport,
    chart_specs: list[ChartSpec],
) -> None:
    st.write("Generated report")
    markdown = report_to_markdown(report)
    st.markdown(markdown)

    try:
        pdf_bytes = build_report_pdf(report, chart_specs=chart_specs)
    except Exception as exc:
        st.warning(f"Could not generate PDF report: {exc}")
        return

    st.download_button(
        label="Download PDF report",
        data=pdf_bytes,
        file_name="business_intelligence_report.pdf",
        mime="application/pdf",
    )


def _store_uploaded_dataframe(settings: Settings, dataframe) -> StoredTable | None:
    try:
        stored_table = SQLiteStore(settings.sqlite_db_path).save_dataframe(
            dataframe,
            table_name=DEFAULT_TABLE_NAME,
        )
    except Exception as exc:
        st.session_state.pop(STORED_TABLE, None)
        st.error(f"Could not store uploaded data in SQLite: {exc}")
        return None

    st.session_state[STORED_TABLE] = stored_table
    return stored_table


def _render_sql_workspace(stored_table: StoredTable | None) -> None:
    if stored_table is None:
        st.warning("SQLite storage is unavailable for this upload.")
        return

    st.write("SQLite table")
    table_cols = st.columns(3)
    table_cols[0].metric("Table", stored_table.table_name)
    table_cols[1].metric("Rows", f"{stored_table.row_count:,}")
    table_cols[2].metric("Columns", f"{stored_table.column_count:,}")
    st.caption(f"Database: {stored_table.database_path}")

    with st.expander("Columns", expanded=False):
        st.write(", ".join(stored_table.columns))

    default_query = f"SELECT * FROM {stored_table.table_name} LIMIT 20"
    query = st.text_area(
        "Read-only SQL query",
        value=default_query,
        height=120,
        help="Only a single SELECT statement is allowed.",
    )

    if st.button("Run query", type="primary"):
        try:
            result = execute_read_query(stored_table.database_path, query)
        except UnsafeQueryError as exc:
            st.error(str(exc))
            return
        except Exception as exc:
            st.error(f"Query failed: {exc}")
            return

        st.success(f"Returned {len(result):,} row(s).")
        st.dataframe(result, use_container_width=True)
