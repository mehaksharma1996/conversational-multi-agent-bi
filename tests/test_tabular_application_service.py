"""Characterization tests for the framework-neutral tabular application service."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from packages.analytics import (
    AnalyzeTabularCommand,
    ConfirmSchemaCommand,
    ListWorkbookSheetsCommand,
    LoadTabularCommand,
    ProfileTabularCommand,
    TabularApplicationService,
    TabularRowLimitError,
    TabularUploadLimitError,
)

SERVICE = TabularApplicationService()


def _transactions_csv(row_count: int = 10) -> bytes:
    rows = ["transaction_date,amount,merchant,customer_id"]
    rows.extend(
        f"2026-01-{index + 1:02d},{999 if index == row_count - 1 else 10 + index},"
        f"Merchant {index % 2},customer-{index % 3}"
        for index in range(row_count)
    )
    return ("\n".join(rows) + "\n").encode()


def test_load_enforces_byte_limit_before_parsing() -> None:
    with pytest.raises(TabularUploadLimitError, match="limited"):
        SERVICE.load(
            LoadTabularCommand(
                payload=b"not-a-valid-csv",
                filename="transactions.csv",
                max_upload_bytes=4,
                max_rows=100,
            )
        )


def test_load_uses_bounded_read_to_reject_excess_rows() -> None:
    with pytest.raises(TabularRowLimitError, match="contains 3 rows"):
        SERVICE.load(
            LoadTabularCommand(
                payload=_transactions_csv(4),
                filename="transactions.csv",
                max_upload_bytes=10_000,
                max_rows=2,
            )
        )


def test_workbook_sheet_discovery_is_independent_of_streamlit() -> None:
    workbook = BytesIO()
    with pd.ExcelWriter(workbook) as writer:
        pd.DataFrame({"amount": [1]}).to_excel(writer, sheet_name="January", index=False)
        pd.DataFrame({"amount": [2]}).to_excel(writer, sheet_name="February", index=False)

    sheets = SERVICE.list_workbook_sheets(
        ListWorkbookSheetsCommand(payload=workbook.getvalue(), filename="ledger.xlsx")
    )

    assert sheets == ("January", "February")


def test_profile_schema_confirmation_and_analysis_preserve_current_behavior() -> None:
    loaded = SERVICE.load(
        LoadTabularCommand(
            payload=_transactions_csv(),
            filename="transactions.csv",
            max_upload_bytes=10_000,
            max_rows=100,
        )
    )
    profiled = SERVICE.profile(ProfileTabularCommand(dataframe=loaded.dataframe))

    assert profiled.profile.row_count == 10
    assert profiled.suggested_mapping.mapped_fields()["amount"] == "amount"
    assert "amount" in profiled.recommended_anomaly_features

    confirmed = SERVICE.confirm_schema(
        ConfirmSchemaCommand(
            profile=profiled.profile,
            overrides={
                "amount": "amount",
                "date": "transaction_date",
                "customer_id": "customer_id",
                "merchant": "merchant",
                "location": None,
                "label": None,
            },
        )
    )
    result = SERVICE.analyze(
        AnalyzeTabularCommand(
            dataframe=loaded.dataframe,
            profile=profiled.profile,
            schema_mapping=confirmed,
            anomaly_features=SERVICE.recommend_anomaly_features(
                profiled.profile,
                confirmed,
            ),
        )
    )

    assert result.analytics_report.trend is not None
    assert result.anomaly_report.enabled
    assert result.business_report.sections
