"""Detect which analyses are possible for an uploaded dataset."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    reason: str
    required_fields: list[str]
    missing_fields: list[str]


@dataclass(frozen=True)
class CapabilityReport:
    capabilities: list[Capability]

    def available_capabilities(self) -> list[str]:
        return [capability.name for capability in self.capabilities if capability.available]

    def unavailable_capabilities(self) -> list[str]:
        return [capability.name for capability in self.capabilities if not capability.available]


def detect_capabilities(
    profile: DataProfile,
    schema_mapping: SchemaMapping,
) -> CapabilityReport:
    """Detect available analysis paths from profile and canonical mappings."""
    mapped_fields = schema_mapping.mapped_fields()

    capabilities = [
        _build_capability(
            name="Fraud-analysis readiness",
            required_fields=["amount", "date", "customer_id", "merchant"],
            mapped_fields=mapped_fields,
            success_reason=("Amount, date, customer, and merchant fields are available."),
        ),
        _generic_anomaly_capability(profile),
        _build_capability(
            name="Trend analysis",
            required_fields=["date", "amount"],
            mapped_fields=mapped_fields,
            success_reason="A date field and numeric amount-like field are available.",
        ),
        _classification_capability(profile=profile, mapped_fields=mapped_fields),
        _build_capability(
            name="Location analysis",
            required_fields=["location", "amount"],
            mapped_fields=mapped_fields,
            success_reason="Location and amount fields are available.",
        ),
    ]

    return CapabilityReport(capabilities=capabilities)


def capability_report_to_dataframe(report: CapabilityReport) -> pd.DataFrame:
    """Convert a capability report to a dataframe for display."""
    return pd.DataFrame(
        [
            {
                "capability": capability.name,
                "available": "Yes" if capability.available else "No",
                "missing_fields": ", ".join(capability.missing_fields)
                if capability.missing_fields
                else "None",
                "reason": capability.reason,
            }
            for capability in report.capabilities
        ]
    )


def _build_capability(
    name: str,
    required_fields: list[str],
    mapped_fields: dict[str, str],
    success_reason: str,
) -> Capability:
    missing_fields = [field for field in required_fields if field not in mapped_fields]
    available = not missing_fields

    return Capability(
        name=name,
        available=available,
        required_fields=required_fields,
        missing_fields=missing_fields,
        reason=success_reason
        if available
        else f"Missing required field(s): {', '.join(missing_fields)}.",
    )


def _generic_anomaly_capability(profile: DataProfile) -> Capability:
    required_fields = ["at least one numeric column"]
    available = len(profile.numeric_columns) >= 1

    return Capability(
        name="Generic anomaly detection",
        available=available,
        required_fields=required_fields,
        missing_fields=[] if available else required_fields,
        reason="Numeric columns are available for unsupervised anomaly detection."
        if available
        else "No numeric columns were detected.",
    )


def _classification_capability(
    profile: DataProfile,
    mapped_fields: dict[str, str],
) -> Capability:
    required_fields = ["label", "at least one feature column"]
    has_label = "label" in mapped_fields or bool(profile.possible_label_columns)
    has_features = profile.column_count >= 2

    missing_fields = []
    if not has_label:
        missing_fields.append("label")
    if not has_features:
        missing_fields.append("at least one feature column")

    return Capability(
        name="Classification readiness",
        available=has_label and has_features,
        required_fields=required_fields,
        missing_fields=missing_fields,
        reason="A label-like column and feature columns are available."
        if has_label and has_features
        else f"Missing required field(s): {', '.join(missing_fields)}.",
    )
