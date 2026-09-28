"""Main dashboard for uploaded data preview."""

from __future__ import annotations

import streamlit as st

from config.settings import Settings
from src.agents.report_agent import BusinessReport, report_to_markdown
from src.analytics.anomaly_detection import (
    DEFAULT_CONTAMINATION,
    AnomalyReport,
    recommend_anomaly_features,
)
from src.analytics.basic_analytics import AnalyticsReport
from src.analytics.pipeline import AnalysisBundle, build_analysis_bundle
from src.charts.chart_builder import ChartSpec
from src.memory.session_keys import (
    ANALYSIS_CACHE,
    DOCUMENT_SIGNATURE,
    DOCUMENT_STATUS,
    PDF_BYTES,
    PDF_SIGNATURE,
    PROFILE_CACHE,
    SESSION_MEMORY,
    STORED_TABLE,
    STORED_TABLE_SIGNATURE,
    TABULAR_SIGNATURE,
    UPLOAD_ERROR,
    UPLOADED_TABLE,
)
from src.memory.session_memory import SessionMemory, build_session_memory
from src.profiling.capability_detector import (
    CapabilityReport,
    capability_report_to_dataframe,
)
from src.profiling.data_profiler import DataProfile, profile_dataframe, profile_to_dataframe
from src.profiling.schema_mapper import (
    CANONICAL_FIELDS,
    REQUIRED_TYPES,
    SchemaMapping,
    map_schema,
    schema_mapping_to_dataframe,
)
from src.reporting.pdf_report import build_report_pdf, chart_export_error
from src.storage.query_executor import (
    QueryTimeoutError,
    UnsafeQueryError,
    execute_read_query,
)
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
    data_signature = st.session_state.get(TABULAR_SIGNATURE, loaded_table.filename)
    profile = _cached_profile(dataframe, data_signature)
    suggested_mapping = map_schema(profile)
    schema_mapping = _render_schema_controls(
        profile=profile,
        suggested_mapping=suggested_mapping,
        data_signature=data_signature,
    )
    anomaly_features, anomaly_contamination = _render_anomaly_controls(
        profile=profile,
        schema_mapping=schema_mapping,
        data_signature=data_signature,
    )
    stored_table = _store_uploaded_dataframe(
        settings,
        dataframe,
        data_signature,
        schema_mapping,
    )
    bundle = _cached_analysis(
        dataframe=dataframe,
        data_signature=data_signature,
        profile=profile,
        schema_mapping=schema_mapping,
        anomaly_features=tuple(anomaly_features),
        anomaly_contamination=anomaly_contamination,
        document_status=st.session_state.get(DOCUMENT_STATUS),
        document_signature=st.session_state.get(DOCUMENT_SIGNATURE),
    )
    capability_report = bundle.capability_report
    analytics_report = bundle.analytics_report
    anomaly_report = bundle.anomaly_report
    chart_specs = bundle.chart_specs
    business_report = bundle.business_report
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
        report_available=bool(business_report.sections),
        sql_available=stored_table is not None,
    )

    (
        overview_tab,
        analytics_tab,
        anomaly_tab,
        report_tab,
        sql_tab,
    ) = st.tabs(
        [
            "Overview",
            "Analytics",
            "Anomalies",
            "Report",
            "SQL",
        ]
    )

    with overview_tab:
        st.write("First 25 rows")
        st.dataframe(dataframe.head(25), use_container_width=True)
        _render_profile_summary(profile)
        st.write("Column profile")
        st.dataframe(profile_to_dataframe(profile), use_container_width=True)
        _render_schema_mapping(schema_mapping)
        _render_capability_report(capability_report)

    with analytics_tab:
        _render_analytics_report(analytics_report)
        _render_charts(chart_specs)

    with anomaly_tab:
        _render_anomaly_report(anomaly_report)

    with report_tab:
        report_signature = (
            data_signature,
            repr(schema_mapping.mapped_fields()),
            tuple(anomaly_features),
            anomaly_contamination,
            st.session_state.get(DOCUMENT_SIGNATURE),
        )
        _render_business_report(business_report, chart_specs, report_signature)

    with sql_tab:
        _render_sql_workspace(stored_table)

    available_workflows = ["analytics", "charts", "report", "SQL", "memory"]
    if st.session_state.get(DOCUMENT_STATUS):
        available_workflows.extend(["RAG", "hybrid questions"])
    st.success("Ready workflows: " + ", ".join(available_workflows) + ".")


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
    report_available: bool,
    sql_available: bool,
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
        "Ready" if report_available else "Unavailable",
        help="A deterministic report and PDF export are available in the Report tab.",
    )
    st.caption("SQL workspace: " + ("ready" if sql_available else "unavailable"))


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


def _render_schema_controls(
    profile: DataProfile,
    suggested_mapping: SchemaMapping,
    data_signature: str,
) -> SchemaMapping:
    overrides: dict[str, str | None] = {}
    with st.expander("Review schema mapping", expanded=False):
        st.caption("Confirm or correct the business meaning of each column before analysis.")
        columns = st.columns(2)
        for index, field in enumerate(CANONICAL_FIELDS):
            options = [
                None,
                *[
                    column.name
                    for column in profile.columns
                    if column.inferred_type in REQUIRED_TYPES[field]
                ],
            ]
            suggested = suggested_mapping.mappings[field].source_column
            default_index = options.index(suggested) if suggested in options else 0
            overrides[field] = columns[index % 2].selectbox(
                field.replace("_", " ").title(),
                options=options,
                index=default_index,
                format_func=lambda value: "Not mapped" if value is None else value,
                key=f"schema_{data_signature}_{field}",
            )

    try:
        return map_schema(profile, overrides=overrides)
    except ValueError as exc:
        st.error(f"Schema mapping needs attention: {exc}")
        return suggested_mapping


def _render_anomaly_controls(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    data_signature: str,
) -> tuple[list[str], float]:
    recommended = recommend_anomaly_features(profile, schema_mapping)
    with st.expander("Anomaly model settings", expanded=False):
        features = st.multiselect(
            "Numeric features",
            options=profile.numeric_columns,
            default=recommended,
            key=f"anomaly_features_{data_signature}",
            help="Identifier and target-like columns are excluded by default.",
        )
        contamination = st.slider(
            "Expected anomaly fraction",
            min_value=0.01,
            max_value=0.25,
            value=DEFAULT_CONTAMINATION,
            step=0.01,
            key=f"anomaly_contamination_{data_signature}",
            help=(
                "This controls model sensitivity; flagged rows are review "
                "candidates, not confirmed fraud."
            ),
        )
    return features, contamination


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

    st.caption(
        "Anomaly flags indicate unusual numeric patterns for review; they are not "
        "proof of fraud, misconduct, or business risk."
    )


def _render_business_report(
    report: BusinessReport,
    chart_specs: list[ChartSpec],
    report_signature: tuple,
) -> None:
    st.write("Generated report")
    markdown = report_to_markdown(report)
    st.markdown(markdown)
    st.download_button(
        label="Download Markdown report",
        data=markdown.encode("utf-8"),
        file_name="business_intelligence_report.md",
        mime="text/markdown",
    )

    if st.session_state.get(PDF_SIGNATURE) != report_signature:
        st.session_state.pop(PDF_BYTES, None)
        st.session_state.pop(PDF_SIGNATURE, None)

    if st.button("Prepare PDF report"):
        export_problem = chart_export_error(chart_specs)
        if export_problem:
            st.warning(export_problem)
        try:
            with st.spinner("Rendering the report and chart images..."):
                st.session_state[PDF_BYTES] = _cached_pdf(report, chart_specs)
                st.session_state[PDF_SIGNATURE] = report_signature
        except Exception as exc:
            st.warning(f"Could not generate PDF report: {exc}")
            return

    pdf_bytes = st.session_state.get(PDF_BYTES)
    if pdf_bytes is None:
        st.caption("Prepare the PDF only when needed to keep dashboard reruns fast.")
        return

    st.download_button(
        label="Download PDF report",
        data=pdf_bytes,
        file_name="business_intelligence_report.pdf",
        mime="application/pdf",
    )


def _store_uploaded_dataframe(
    settings: Settings,
    dataframe,
    data_signature: str,
    schema_mapping: SchemaMapping,
) -> StoredTable | None:
    storage_signature = (data_signature, tuple(schema_mapping.mapped_fields().items()))
    if (
        st.session_state.get(STORED_TABLE_SIGNATURE) == storage_signature
        and STORED_TABLE in st.session_state
    ):
        return st.session_state[STORED_TABLE]

    try:
        stored_table = SQLiteStore(settings.sqlite_db_path).save_dataframe(
            dataframe,
            table_name=DEFAULT_TABLE_NAME,
            canonical_mapping=schema_mapping.mapped_fields(),
        )
    except Exception as exc:
        st.session_state.pop(STORED_TABLE, None)
        st.error(f"Could not store uploaded data in SQLite: {exc}")
        return None

    st.session_state[STORED_TABLE] = stored_table
    st.session_state[STORED_TABLE_SIGNATURE] = storage_signature
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
    st.caption("Stored in a session-isolated, read-only query workspace.")

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
            result = execute_read_query(
                stored_table.database_path,
                query,
                allowed_tables={stored_table.table_name},
                allowed_columns={stored_table.table_name: set(stored_table.columns)},
            )
        except (UnsafeQueryError, QueryTimeoutError) as exc:
            st.error(str(exc))
            return
        except Exception as exc:
            st.error(f"Query failed: {exc}")
            return

        st.success(f"Returned {len(result):,} row(s).")
        st.dataframe(result, use_container_width=True)


def _cached_profile(dataframe, data_signature: str) -> DataProfile:
    cached = st.session_state.get(PROFILE_CACHE)
    if cached is not None and cached[0] == data_signature:
        return cached[1]

    profile = profile_dataframe(dataframe)
    st.session_state[PROFILE_CACHE] = (data_signature, profile)
    return profile


def _cached_analysis(
    dataframe,
    data_signature: str,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    anomaly_features: tuple[str, ...],
    anomaly_contamination: float,
    document_status: dict | None,
    document_signature: tuple | None,
) -> AnalysisBundle:
    cache_key = (
        data_signature,
        tuple(schema_mapping.mapped_fields().items()),
        anomaly_features,
        anomaly_contamination,
        document_signature,
    )
    cached = st.session_state.get(ANALYSIS_CACHE)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    bundle = build_analysis_bundle(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=schema_mapping,
        anomaly_features=list(anomaly_features),
        anomaly_contamination=anomaly_contamination,
        document_status=document_status,
    )
    st.session_state[ANALYSIS_CACHE] = (cache_key, bundle)
    return bundle


def _cached_pdf(report: BusinessReport, chart_specs: list[ChartSpec]) -> bytes:
    return build_report_pdf(report, chart_specs=chart_specs)
