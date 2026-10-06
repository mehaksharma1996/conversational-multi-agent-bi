"""Deterministic, leakage-aware binary classification for confirmed labels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import (
    StratifiedKFold,
    cross_val_predict,
    cross_val_score,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping

MIN_LABELLED_ROWS = 100
MIN_CLASS_ROWS = 20
MAX_REVIEW_CANDIDATES = 50
RANDOM_STATE = 42
POSITIVE_TOKENS = {
    "1",
    "true",
    "yes",
    "positive",
    "fraud",
    "fraudulent",
    "flagged",
    "review",
    "anomaly",
}


@dataclass(frozen=True)
class ClassificationMetrics:
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    cv_pr_auc: float
    threshold: float
    positive_rate: float
    true_negative: int
    false_positive: int
    false_negative: int
    true_positive: int


@dataclass(frozen=True)
class SupervisedClassificationReport:
    enabled: bool
    reason: str
    method: str = "disabled"
    label_column: str | None = None
    positive_label: str | None = None
    feature_columns: list[str] = field(default_factory=list)
    excluded_columns: dict[str, str] = field(default_factory=dict)
    metrics: ClassificationMetrics | None = None
    review_candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    precision_recall_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    limitations: list[str] = field(default_factory=list)


def assess_classification_eligibility(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> SupervisedClassificationReport:
    """Return a disabled report or an eligible report containing safe feature choices."""
    label_column = schema_mapping.mapped_fields().get("label")
    if label_column is None:
        return _disabled("A confirmed label mapping is required.")
    if label_column not in dataframe.columns:
        return _disabled("The mapped label column is not present.", label_column=label_column)

    labelled = dataframe[dataframe[label_column].notna()]
    if len(labelled) < MIN_LABELLED_ROWS:
        return _disabled(
            f"At least {MIN_LABELLED_ROWS} labelled rows are required; found {len(labelled)}.",
            label_column=label_column,
        )

    class_counts = labelled[label_column].value_counts(dropna=True)
    if len(class_counts) != 2:
        return _disabled(
            "The confirmed label must contain exactly two non-null classes.",
            label_column=label_column,
        )
    if int(class_counts.min()) < MIN_CLASS_ROWS:
        return _disabled(
            f"Each class requires at least {MIN_CLASS_ROWS} rows; the minority class has "
            f"{int(class_counts.min())}.",
            label_column=label_column,
        )

    positive_value = _choose_positive_class(class_counts)
    target = labelled[label_column].eq(positive_value).astype(int)
    feature_columns, excluded_columns = _select_features(
        labelled,
        target,
        profile,
        schema_mapping,
        label_column,
    )
    if not feature_columns:
        return _disabled(
            "No usable non-identifier, non-leaking feature columns remain.",
            label_column=label_column,
            positive_label=str(positive_value),
            excluded_columns=excluded_columns,
        )

    return SupervisedClassificationReport(
        enabled=True,
        reason="The confirmed binary label and usable feature rows meet training requirements.",
        label_column=label_column,
        positive_label=str(positive_value),
        feature_columns=feature_columns,
        excluded_columns=excluded_columns,
    )


def run_supervised_classification(
    dataframe: pd.DataFrame,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> SupervisedClassificationReport:
    """Train and evaluate deterministic candidate classifiers when the dataset qualifies."""
    eligibility = assess_classification_eligibility(dataframe, profile, schema_mapping)
    if not eligibility.enabled or eligibility.label_column is None:
        return eligibility

    label_column = eligibility.label_column
    labelled = dataframe[dataframe[label_column].notna()].copy()
    class_counts = labelled[label_column].value_counts(dropna=True)
    positive_value = _choose_positive_class(class_counts)
    target = labelled[label_column].eq(positive_value).astype(int)
    features = _prepare_features(labelled[eligibility.feature_columns], profile)

    train_features, test_features, train_target, test_target = train_test_split(
        features,
        target,
        test_size=0.2,
        random_state=RANDOM_STATE,
        stratify=target,
    )
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
    candidates = _candidate_models(profile, eligibility.feature_columns)
    candidate_scores = {
        name: float(
            cross_val_score(
                model,
                train_features,
                train_target,
                cv=folds,
                scoring="average_precision",
            ).mean()
        )
        for name, model in candidates.items()
    }
    selected_name = max(candidate_scores, key=lambda name: (candidate_scores[name], name))
    selected_model = candidates[selected_name]

    training_probabilities = cross_val_predict(
        selected_model,
        train_features,
        train_target,
        cv=folds,
        method="predict_proba",
    )[:, 1]
    threshold = _select_threshold(train_target.to_numpy(), training_probabilities)
    selected_model.fit(train_features, train_target)
    test_probabilities = selected_model.predict_proba(test_features)[:, 1]
    test_predictions = (test_probabilities >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(test_target, test_predictions, labels=[0, 1]).ravel()

    metrics = ClassificationMetrics(
        precision=float(precision_score(test_target, test_predictions, zero_division=0)),
        recall=float(recall_score(test_target, test_predictions, zero_division=0)),
        f1=float(f1_score(test_target, test_predictions, zero_division=0)),
        roc_auc=float(roc_auc_score(test_target, test_probabilities)),
        pr_auc=float(average_precision_score(test_target, test_probabilities)),
        cv_pr_auc=candidate_scores[selected_name],
        threshold=float(threshold),
        positive_rate=float(target.mean()),
        true_negative=int(tn),
        false_positive=int(fp),
        false_negative=int(fn),
        true_positive=int(tp),
    )
    curve_precision, curve_recall, curve_thresholds = precision_recall_curve(
        test_target,
        test_probabilities,
    )
    curve = pd.DataFrame(
        {
            "recall": curve_recall,
            "precision": curve_precision,
            "threshold": np.append(curve_thresholds, np.nan),
        }
    ).sort_values(["recall", "precision"], kind="stable")

    ranking_model = candidates[selected_name]
    ranking_model.fit(features, target)
    all_probabilities = ranking_model.predict_proba(features)[:, 1]
    review_candidates = (
        pd.DataFrame(
            {
                "source_row": labelled.index,
                "predicted_probability": all_probabilities,
                "predicted_positive": all_probabilities >= threshold,
            }
        )
        .sort_values(
            ["predicted_probability", "source_row"],
            ascending=[False, True],
            kind="stable",
        )
        .head(MAX_REVIEW_CANDIDATES)
        .reset_index(drop=True)
    )
    review_candidates.insert(0, "review_rank", range(1, len(review_candidates) + 1))

    return SupervisedClassificationReport(
        enabled=True,
        reason=eligibility.reason,
        method=selected_name,
        label_column=label_column,
        positive_label=str(positive_value),
        feature_columns=eligibility.feature_columns,
        excluded_columns=eligibility.excluded_columns,
        metrics=metrics,
        review_candidates=review_candidates,
        precision_recall_curve=curve.reset_index(drop=True),
        limitations=[
            "Holdout metrics estimate association, not causation or future performance.",
            "Class labels and historical patterns can encode bias or data-quality errors.",
            "Ranked candidates require human review and must not trigger automated decisions.",
            "Thresholds and model quality should be revalidated when data changes.",
        ],
    )


def _disabled(
    reason: str,
    *,
    label_column: str | None = None,
    positive_label: str | None = None,
    excluded_columns: dict[str, str] | None = None,
) -> SupervisedClassificationReport:
    return SupervisedClassificationReport(
        enabled=False,
        reason=reason,
        label_column=label_column,
        positive_label=positive_label,
        excluded_columns=excluded_columns or {},
        limitations=[reason],
    )


def _choose_positive_class(class_counts: pd.Series) -> Any:
    classes = list(class_counts.index)
    token_matches = [value for value in classes if str(value).strip().lower() in POSITIVE_TOKENS]
    if len(token_matches) == 1:
        return token_matches[0]
    minimum = int(class_counts.min())
    minority_classes = [value for value in classes if int(class_counts[value]) == minimum]
    return sorted(minority_classes, key=lambda value: str(value))[-1]


def _select_features(
    dataframe: pd.DataFrame,
    target: pd.Series,
    profile: DataProfile,
    schema_mapping: SchemaMapping,
    label_column: str,
) -> tuple[list[str], dict[str, str]]:
    excluded: dict[str, str] = {label_column: "confirmed label"}
    usable_types = set(
        profile.numeric_columns + profile.categorical_columns + profile.boolean_columns
    )
    mapped_fields = schema_mapping.mapped_fields()
    identifiers = set(profile.possible_id_columns)
    identifiers.difference_update(
        mapped_fields[field]
        for field in ("amount", "merchant", "location")
        if field in mapped_fields
    )
    customer_identifier = mapped_fields.get("customer_id")
    if customer_identifier is not None:
        identifiers.add(customer_identifier)
    dates = set(profile.date_columns)
    selected: list[str] = []

    for column in dataframe.columns:
        if column == label_column:
            continue
        if column in identifiers:
            excluded[column] = "identifier-like column"
        elif column in dates:
            excluded[column] = "date-like column"
        elif column not in usable_types:
            excluded[column] = "unsupported free-text or empty column"
        elif dataframe[column].dropna().nunique() <= 1:
            excluded[column] = "constant column"
        elif _is_deterministic_target_proxy(dataframe[column], target):
            excluded[column] = "deterministic target proxy"
        else:
            selected.append(column)
    return selected, excluded


def _is_deterministic_target_proxy(feature: pd.Series, target: pd.Series) -> bool:
    observed = feature.notna()
    if float(observed.mean()) < 0.9:
        return False
    normalized = feature[observed].astype(str)
    if normalized.nunique() > 20:
        return False
    grouped = pd.DataFrame({"feature": normalized, "target": target[observed]}).groupby("feature")
    return bool(grouped["target"].nunique().max() == 1)


def _prepare_features(dataframe: pd.DataFrame, profile: DataProfile) -> pd.DataFrame:
    prepared = dataframe.copy()
    numeric = set(profile.numeric_columns)
    for column in prepared.columns:
        if column in numeric:
            prepared[column] = pd.to_numeric(prepared[column], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            )
        else:
            prepared[column] = prepared[column].where(prepared[column].notna(), np.nan)
            prepared[column] = prepared[column].astype("object")
    return prepared


def _candidate_models(
    profile: DataProfile,
    feature_columns: list[str],
) -> dict[str, BaseEstimator]:
    numeric_columns = [column for column in feature_columns if column in profile.numeric_columns]
    categorical_columns = [column for column in feature_columns if column not in numeric_columns]

    logistic_transformers: list[tuple[str, Pipeline, list[str]]] = []
    boosted_transformers: list[tuple[str, Pipeline, list[str]]] = []
    if numeric_columns:
        logistic_transformers.append(
            (
                "numeric",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", StandardScaler()),
                    ]
                ),
                numeric_columns,
            )
        )
        boosted_transformers.append(
            ("numeric", Pipeline([("imputer", SimpleImputer(strategy="median"))]), numeric_columns)
        )
    if categorical_columns:
        logistic_transformers.append(
            (
                "categorical",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        (
                            "encoder",
                            OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                        ),
                    ]
                ),
                categorical_columns,
            )
        )
        boosted_transformers.append(
            (
                "categorical",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        (
                            "encoder",
                            OrdinalEncoder(
                                handle_unknown="use_encoded_value",
                                unknown_value=-1,
                            ),
                        ),
                    ]
                ),
                categorical_columns,
            )
        )

    return {
        "gradient_boosted_tree": Pipeline(
            [
                ("preprocessor", ColumnTransformer(boosted_transformers)),
                (
                    "classifier",
                    HistGradientBoostingClassifier(
                        class_weight="balanced",
                        learning_rate=0.08,
                        max_depth=5,
                        max_iter=100,
                        l2_regularization=1.0,
                        random_state=RANDOM_STATE,
                    ),
                ),
            ]
        ),
        "logistic_regression": Pipeline(
            [
                ("preprocessor", ColumnTransformer(logistic_transformers)),
                (
                    "classifier",
                    LogisticRegression(
                        class_weight="balanced",
                        max_iter=1_000,
                        random_state=RANDOM_STATE,
                    ),
                ),
            ]
        ),
    }


def _select_threshold(target: np.ndarray, probabilities: np.ndarray) -> float:
    precision, recall, thresholds = precision_recall_curve(target, probabilities)
    if not len(thresholds):
        return 0.5
    denominator = precision[:-1] + recall[:-1]
    scores = np.divide(
        2 * precision[:-1] * recall[:-1],
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > 0,
    )
    best_score = float(scores.max())
    best_thresholds = thresholds[np.isclose(scores, best_score)]
    return float(best_thresholds.max())
