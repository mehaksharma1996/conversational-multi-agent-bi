"""Pure analysis pipeline used by the cached Streamlit layer."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.agents.report_agent import BusinessReport, generate_business_report
from src.analytics.anomaly_detection import AnomalyReport, detect_anomalies
from src.analytics.basic_analytics import AnalyticsReport, run_basic_analytics
from src.analytics.supervised_classification import (
    SupervisedClassificationReport,
    run_supervised_classification,
)
from src.charts.chart_builder import ChartSpec, build_charts
from src.profiling.capability_detector import CapabilityReport, detect_capabilities
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass(frozen=True)
class AnalysisBundle:
    capability_report: CapabilityReport
    analytics_report: AnalyticsReport
    anomaly_report: AnomalyReport
    classification_report: SupervisedClassificationReport
    chart_specs: list[ChartSpec]
    business_report: BusinessReport


def build_analysis_bundle(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    anomaly_features: list[str],
    anomaly_contamination: float,
    document_status: dict | None,
) -> AnalysisBundle:
    classification_report = run_supervised_classification(dataframe, profile, schema_mapping)
    capability_report = detect_capabilities(profile, schema_mapping, dataframe)
    analytics_report = run_basic_analytics(dataframe, profile, schema_mapping)
    anomaly_report = detect_anomalies(
        dataframe,
        profile,
        schema_mapping,
        feature_columns=anomaly_features,
        contamination=anomaly_contamination,
    )
    chart_specs = build_charts(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=schema_mapping,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
        classification_report=classification_report,
    )
    business_report = generate_business_report(
        profile=profile,
        schema_mapping=schema_mapping,
        capability_report=capability_report,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
        classification_report=classification_report,
        chart_specs=chart_specs,
        document_status=document_status,
    )
    return AnalysisBundle(
        capability_report=capability_report,
        analytics_report=analytics_report,
        anomaly_report=anomaly_report,
        classification_report=classification_report,
        chart_specs=chart_specs,
        business_report=business_report,
    )
