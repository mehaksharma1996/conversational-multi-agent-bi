"""Integration test for the deterministic analysis pipeline."""

import pandas as pd

from src.analytics.anomaly_detection import recommend_anomaly_features
from src.analytics.pipeline import build_analysis_bundle
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def test_analysis_pipeline_builds_coherent_bundle() -> None:
    dataframe = pd.DataFrame(
        {
            "transaction_date": pd.date_range("2026-01-01", periods=10).astype(str),
            "amount": [10, 12, 11, 13, 12, 14, 11, 10, 13, 999],
            "merchant": ["A", "A", "B", "A", "B", "A", "B", "A", "B", "Z"],
        }
    )
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile)

    bundle = build_analysis_bundle(
        dataframe=dataframe,
        profile=profile,
        schema_mapping=mapping,
        anomaly_features=recommend_anomaly_features(profile, mapping),
        anomaly_contamination=0.05,
        document_status=None,
    )

    assert bundle.analytics_report.trend is not None
    assert bundle.anomaly_report.enabled
    assert bundle.chart_specs
    assert bundle.business_report.sections
