"""Map arbitrary user columns to a small canonical business schema."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.profiling.data_profiler import ColumnProfile, DataProfile

CANONICAL_FIELDS = (
    "amount",
    "date",
    "customer_id",
    "merchant",
    "location",
    "label",
)

FIELD_ALIASES = {
    "amount": {
        "amount",
        "amt",
        "value",
        "price",
        "cost",
        "total",
        "transaction_amount",
        "payment_amount",
        "revenue",
        "sales",
    },
    "date": {
        "date",
        "timestamp",
        "time",
        "created_at",
        "transaction_date",
        "purchase_date",
        "order_date",
        "event_date",
    },
    "customer_id": {
        "customer_id",
        "customer",
        "client_id",
        "client",
        "user_id",
        "account_id",
        "member_id",
        "cardholder_id",
    },
    "merchant": {
        "merchant",
        "vendor",
        "seller",
        "store",
        "retailer",
        "business",
        "supplier",
    },
    "location": {
        "location",
        "city",
        "state",
        "country",
        "region",
        "address",
        "zip",
        "postal_code",
    },
    "label": {
        "label",
        "target",
        "class",
        "outcome",
        "status",
        "is_fraud",
        "fraud",
    },
}

REQUIRED_TYPES = {
    "amount": {"numeric"},
    "date": {"date"},
    "customer_id": {"categorical", "text", "numeric"},
    "merchant": {"categorical", "text"},
    "location": {"categorical", "text"},
    "label": {"categorical", "boolean", "numeric"},
}


@dataclass(frozen=True)
class FieldMapping:
    canonical_field: str
    source_column: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class SchemaMapping:
    mappings: dict[str, FieldMapping]

    def mapped_fields(self) -> dict[str, str]:
        return {
            field: mapping.source_column
            for field, mapping in self.mappings.items()
            if mapping.source_column is not None
        }

    def missing_fields(self) -> list[str]:
        return [field for field, mapping in self.mappings.items() if mapping.source_column is None]


def map_schema(
    profile: DataProfile,
    overrides: dict[str, str | None] | None = None,
) -> SchemaMapping:
    """Map profiled columns to canonical fields using conservative heuristics."""
    available_columns = {column.name for column in profile.columns}
    columns_by_name = {column.name: column for column in profile.columns}
    mappings: dict[str, FieldMapping] = {}

    if overrides is not None:
        unknown_fields = set(overrides).difference(CANONICAL_FIELDS)
        if unknown_fields:
            raise ValueError("Unknown canonical field(s): " + ", ".join(sorted(unknown_fields)))
        chosen_columns = [column for column in overrides.values() if column is not None]
        if len(chosen_columns) != len(set(chosen_columns)):
            raise ValueError("A source column can only map to one canonical field.")

    for field in CANONICAL_FIELDS:
        if overrides is not None and field in overrides:
            source_column = overrides[field]
            if source_column is None:
                mappings[field] = FieldMapping(
                    canonical_field=field,
                    source_column=None,
                    confidence=0.0,
                    reason="Not mapped by the user.",
                )
                continue
            if source_column not in available_columns:
                raise ValueError(f"Column '{source_column}' is unavailable or already mapped.")
            if columns_by_name[source_column].inferred_type not in REQUIRED_TYPES[field]:
                raise ValueError(f"Column '{source_column}' is not compatible with {field}.")
            mappings[field] = FieldMapping(
                canonical_field=field,
                source_column=source_column,
                confidence=1.0,
                reason="Confirmed by the user.",
            )
            available_columns.discard(source_column)
            continue

        mapping = _best_mapping_for_field(
            field=field,
            columns=[column for column in profile.columns if column.name in available_columns],
        )
        mappings[field] = mapping
        if mapping.source_column is not None:
            available_columns.discard(mapping.source_column)

    return SchemaMapping(mappings=mappings)


def schema_mapping_to_dataframe(mapping: SchemaMapping) -> pd.DataFrame:
    """Convert schema mapping to a dataframe for display."""
    return pd.DataFrame(
        [
            {
                "canonical_field": field_mapping.canonical_field,
                "source_column": field_mapping.source_column or "Not found",
                "confidence": field_mapping.confidence,
                "reason": field_mapping.reason,
            }
            for field_mapping in mapping.mappings.values()
        ]
    )


def _best_mapping_for_field(
    field: str,
    columns: list[ColumnProfile],
) -> FieldMapping:
    candidates = [
        _score_column_for_field(field=field, column=column)
        for column in columns
        if column.inferred_type in REQUIRED_TYPES[field]
    ]
    candidates = [candidate for candidate in candidates if candidate.confidence > 0]

    if not candidates:
        return FieldMapping(
            canonical_field=field,
            source_column=None,
            confidence=0.0,
            reason="No column matched the expected name and type.",
        )

    return max(candidates, key=lambda candidate: candidate.confidence)


def _score_column_for_field(field: str, column: ColumnProfile) -> FieldMapping:
    normalized = _normalize(column.name)
    aliases = FIELD_ALIASES[field]

    if normalized in aliases:
        return FieldMapping(
            canonical_field=field,
            source_column=column.name,
            confidence=0.95,
            reason=f"Column name directly matches a known alias for {field}.",
        )

    for alias in aliases:
        if alias in normalized or normalized in alias:
            return FieldMapping(
                canonical_field=field,
                source_column=column.name,
                confidence=0.75,
                reason=f"Column name partially matches '{alias}'.",
            )

    if field == "amount" and column.inferred_type == "numeric":
        return FieldMapping(
            canonical_field=field,
            source_column=column.name,
            confidence=0.4,
            reason="Numeric column could be used as an amount-like measure.",
        )

    return FieldMapping(
        canonical_field=field,
        source_column=None,
        confidence=0.0,
        reason="No useful match.",
    )


def _normalize(value: str) -> str:
    normalized = value.strip().lower()
    for character in (" ", "-", ".", "/", "\\"):
        normalized = normalized.replace(character, "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized
