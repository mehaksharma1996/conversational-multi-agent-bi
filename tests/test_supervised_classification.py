"""Tests for deterministic supervised classification."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from src.analytics.supervised_classification import (
    assess_classification_eligibility,
    run_supervised_classification,
)
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def _labelled_dataset(rows: int = 200) -> pd.DataFrame:
    random = np.random.default_rng(42)
    base_risk = random.normal(size=rows)
    labels = (base_risk + random.normal(scale=0.8, size=rows) > 0.7).astype(int)
    return pd.DataFrame(
        {
            "transaction_id": [f"tx-{index:04d}" for index in range(rows)],
            "risk_measure": np.round(base_risk, 1),
            "segment": np.where(base_risk > 0, "east", "west"),
            "constant": "same",
            "target_proxy": labels,
            "outcome": labels,
        }
    )


def _profile_and_mapping(dataframe: pd.DataFrame):
    profile = profile_dataframe(dataframe)
    mapping = map_schema(profile, overrides={"label": "outcome"})
    return profile, mapping


def test_classifier_requires_confirmed_eligible_binary_label() -> None:
    dataframe = _labelled_dataset(80)
    profile = profile_dataframe(dataframe)
    without_label = map_schema(profile, overrides={"label": None})

    no_mapping = assess_classification_eligibility(dataframe, profile, without_label)
    assert not no_mapping.enabled
    assert "confirmed label" in no_mapping.reason.lower()

    profile, mapping = _profile_and_mapping(dataframe)
    too_small = assess_classification_eligibility(dataframe, profile, mapping)
    assert not too_small.enabled
    assert "100" in too_small.reason


def test_classifier_rejects_degenerate_or_underrepresented_labels() -> None:
    dataframe = _labelled_dataset()
    dataframe["outcome"] = 0
    profile, mapping = _profile_and_mapping(dataframe)
    single_class = run_supervised_classification(dataframe, profile, mapping)
    assert not single_class.enabled
    assert "exactly two" in single_class.reason

    dataframe.loc[:9, "outcome"] = 1
    profile, mapping = _profile_and_mapping(dataframe)
    sparse_positive = run_supervised_classification(dataframe, profile, mapping)
    assert not sparse_positive.enabled
    assert "at least 20" in sparse_positive.reason


def test_classifier_excludes_identifiers_constants_and_label_proxies() -> None:
    dataframe = _labelled_dataset()
    profile, mapping = _profile_and_mapping(dataframe)

    eligibility = assess_classification_eligibility(dataframe, profile, mapping)

    assert eligibility.enabled
    assert eligibility.feature_columns == ["risk_measure", "segment"]
    assert eligibility.excluded_columns["transaction_id"] == "identifier-like column"
    assert eligibility.excluded_columns["constant"] == "constant column"
    assert eligibility.excluded_columns["target_proxy"] == "deterministic target proxy"


def test_classifier_is_deterministic_and_reports_decision_metrics() -> None:
    dataframe = _labelled_dataset()
    profile, mapping = _profile_and_mapping(dataframe)

    first = run_supervised_classification(dataframe, profile, mapping)
    second = run_supervised_classification(dataframe, profile, mapping)

    assert first.enabled
    assert first.method in {"gradient_boosted_tree", "logistic_regression"}
    assert first.metrics == second.metrics
    assert first.metrics is not None
    assert 0 <= first.metrics.precision <= 1
    assert 0 <= first.metrics.recall <= 1
    assert 0 <= first.metrics.f1 <= 1
    assert 0 <= first.metrics.roc_auc <= 1
    assert 0 <= first.metrics.pr_auc <= 1
    assert first.metrics.pr_auc > first.metrics.positive_rate
    assert (
        first.metrics.true_negative
        + first.metrics.false_positive
        + first.metrics.false_negative
        + first.metrics.true_positive
        == 40
    )
    assert list(first.review_candidates.columns) == [
        "review_rank",
        "source_row",
        "predicted_probability",
        "predicted_positive",
    ]
    assert_frame_equal(first.review_candidates, second.review_candidates)
    assert_frame_equal(first.precision_recall_curve, second.precision_recall_curve)
