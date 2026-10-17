"""Integrity and arithmetic contracts for the synthetic churn fixture."""

from __future__ import annotations

import copy
import csv
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.generate_subscription_churn import (
    ASSET_RELATIVE_PATHS,
    MISSING_REASON_COUNT,
    MODEL_ACCESSIBLE_RELATIVE_PATHS,
    REACTIVATIONS_PER_QUARTER,
    REFERENCE_QUERIES,
    ROW_COUNT,
    build_oracle,
    generate_assets,
)
from src.ingestion.tabular_loader import load_tabular_file
from src.storage.query_executor import execute_read_query
from src.storage.sqlite_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "sample_data" / "subscription_churn.csv"
ORACLE_PATH = ROOT / "evals" / "investigation" / "v1" / "oracle.json"


def _rows() -> list[dict[str, str]]:
    with CSV_PATH.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _oracle() -> dict:
    return json.loads(ORACLE_PATH.read_text(encoding="utf-8"))


def test_every_generated_asset_is_byte_reproducible(tmp_path: Path) -> None:
    generated = generate_assets(tmp_path)

    assert set(generated) == {path.as_posix() for path in ASSET_RELATIVE_PATHS}
    for relative, generated_path in generated.items():
        assert generated_path.read_bytes() == (ROOT / relative).read_bytes(), relative


def test_rows_are_synthetic_unique_and_within_supported_limits() -> None:
    rows = _rows()

    assert len(rows) == ROW_COUNT == 8_200
    assert CSV_PATH.stat().st_size < 50 * 1024 * 1024
    assert len({row["record_id"] for row in rows}) == ROW_COUNT
    assert len({(row["customer_id"], row["quarter"]) for row in rows}) == ROW_COUNT
    assert len({row["customer_id"] for row in rows}) < ROW_COUNT
    assert all(row["record_id"].startswith("CQ-") for row in rows)
    assert all(row["customer_id"].startswith("CUS-") for row in rows)
    assert {row["quarter"] for row in rows} == {
        "2025-Q3",
        "2025-Q4",
        "2026-Q1",
        "2026-Q2",
    }
    assert not any("@" in value for row in rows for value in row.values())

    for quarter in {row["quarter"] for row in rows}:
        quarter_rows = [row for row in rows if row["quarter"] == quarter]
        assert len(quarter_rows) == 2_050
        assert sum(row["active_at_start"] == "true" for row in quarter_rows) == 2_000
        assert sum(row["active_at_start"] == "false" for row in quarter_rows) == (
            REACTIVATIONS_PER_QUARTER
        )


def test_churn_is_recomputed_from_lifecycle_fields() -> None:
    for row in _rows():
        start = date.fromisoformat(row["quarter_start"])
        end = date.fromisoformat(row["quarter_end"])
        cancelled = date.fromisoformat(row["cancelled_at"]) if row["cancelled_at"] else None
        reactivated = date.fromisoformat(row["reactivated_at"]) if row["reactivated_at"] else None
        recomputed = (
            row["active_at_start"] == "true"
            and cancelled is not None
            and start <= cancelled <= end
            and (reactivated is None or reactivated > end)
        )
        assert (row["churned"] == "true") is recomputed
        assert (row["active_at_end"] == "false") is recomputed
        if reactivated is not None:
            assert start <= reactivated <= end
            assert not recomputed


def test_customer_histories_are_stable_and_next_quarter_eligible() -> None:
    histories: dict[str, list[dict[str, str]]] = defaultdict(list)
    quarter_order = {
        quarter: index for index, quarter in enumerate(("2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"))
    }
    for row in _rows():
        histories[row["customer_id"]].append(row)

    for history in histories.values():
        history.sort(key=lambda row: quarter_order[row["quarter"]])
        assert len({row["quarter"] for row in history}) == len(history)
        stable = {
            (row["region"], row["customer_segment"], row["plan_tier"], row["customer_start_date"])
            for row in history
        }
        assert len(stable) == 1
        customer_start = date.fromisoformat(history[0]["customer_start_date"])
        for row in history:
            quarter_start = date.fromisoformat(row["quarter_start"])
            expected_tenure = (quarter_start.year - customer_start.year) * 12 + (
                quarter_start.month - customer_start.month
            )
            assert int(row["tenure_months_start"]) == expected_tenure

        first = history[0]
        if first["active_at_start"] == "false":
            assert first["reactivated_at"]
            assert date.fromisoformat(first["cancelled_at"]) < date.fromisoformat(
                first["quarter_start"]
            )
        elif first["quarter"] != "2025-Q3":
            assert first["customer_start_date"] == first["quarter_start"]

        for previous, current in zip(history, history[1:], strict=False):
            assert quarter_order[current["quarter"]] == quarter_order[previous["quarter"]] + 1
            if previous["active_at_end"] == "true":
                assert current["active_at_start"] == "true"
            else:
                assert current["active_at_start"] == "false"
                assert current["reactivated_at"]


def test_reactivations_and_replacements_enter_the_at_risk_population_explicitly() -> None:
    rows = _rows()
    by_customer_quarter = {(row["customer_id"], row["quarter"]): row for row in rows}
    quarters = ("2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2")

    for previous_quarter, current_quarter in zip(quarters, quarters[1:], strict=False):
        current_eligible = [
            row
            for row in rows
            if row["quarter"] == current_quarter and row["active_at_start"] == "true"
        ]
        for row in current_eligible:
            previous = by_customer_quarter.get((row["customer_id"], previous_quarter))
            if previous is None:
                assert row["customer_start_date"] == row["quarter_start"]
            else:
                assert previous["active_at_end"] == "true"


def test_python_oracle_is_derived_from_the_committed_rows() -> None:
    rows = _rows()
    committed = _oracle()

    assert committed == build_oracle(rows)
    facts = committed["facts"]
    assert facts["overall"] == {
        "2026-Q1": {"denominator": 2000, "churned": 96, "rate": "0.0480"},
        "2026-Q2": {"denominator": 2000, "churned": 142, "rate": "0.0710"},
    }
    assert facts["smb_basic"]["2026-Q1"]["rate"] == "0.0600"
    assert facts["smb_basic"]["2026-Q2"]["rate"] == "0.1400"
    assert facts["apac"]["2026-Q1"]["rate"] == "0.0500"
    assert facts["apac"]["2026-Q2"]["rate"] == "0.0900"
    assert facts["enterprise"]["2026-Q1"] == facts["enterprise"]["2026-Q2"]
    assert facts["smb_basic_incremental_churn_share"] == "0.8696"


def test_tenure_adjustment_currency_and_missing_reasons_are_exact() -> None:
    facts = _oracle()["facts"]

    assert facts["smb_basic_tenure"] == {
        "2026-Q1": {
            "new": {"denominator": 57, "churned": 15, "rate": "0.2632"},
            "established": {"denominator": 443, "churned": 15, "rate": "0.0339"},
        },
        "2026-Q2": {
            "new": {"denominator": 52, "churned": 12, "rate": "0.2308"},
            "established": {"denominator": 448, "churned": 58, "rate": "0.1295"},
        },
    }
    assert facts["q2_tenure_standardized_rate_using_q1_weights"] == "0.1410"
    assert facts["smb_basic_observed_rate_change"] == "0.0800"
    assert facts["tenure_standardized_rate_change"] == "0.0810"
    assert facts["tenure_composition_associated_difference"] == "-0.0010"
    assert facts["mrr_lost"] == {"2026-Q1": "28124.00", "2026-Q2": "32578.00"}
    assert facts["mrr_lost_change"] == "4454.00"
    assert facts["missing_churn_reasons"] == {
        "churned": 420,
        "missing": MISSING_REASON_COUNT,
        "rate": "0.4000",
    }


def test_reference_sql_matches_oracle_through_real_ingestion(tmp_path: Path) -> None:
    with CSV_PATH.open("rb") as stream:
        loaded = load_tabular_file(stream, CSV_PATH.name)
    stored = SQLiteStore(tmp_path / "subscription-churn.sqlite3").save_dataframe(loaded.dataframe)
    expected = _oracle()["reference_sql_results"]

    assert loaded.row_count == ROW_COUNT
    for name, sql in REFERENCE_QUERIES.items():
        actual = execute_read_query(
            stored.database_path,
            sql,
            allowed_tables={stored.table_name},
            allowed_columns={stored.table_name: set(stored.columns)},
            encryption_key=stored.encryption_key,
        )
        rows = json.loads(actual.to_json(orient="values"))
        assert rows == expected[name], name


def test_oracle_reacts_to_mutated_source_counts_instead_of_fixed_figures() -> None:
    source = _rows()
    original = build_oracle(source)
    mutated = copy.deepcopy(source)
    row = next(
        item
        for item in mutated
        if item["quarter"] == "2026-Q2"
        and item["customer_segment"] == "SMB"
        and item["plan_tier"] == "Basic"
        and item["churned"] == "false"
        and not item["reactivated_at"]
    )
    row["cancelled_at"] = "2026-06-30"
    row["active_at_end"] = "false"
    row["churned"] = "true"
    row["churn_reason_code"] = "price"

    changed = build_oracle(mutated)

    assert changed["facts"]["overall"]["2026-Q2"]["churned"] == 143
    assert changed["facts"]["smb_basic"]["2026-Q2"]["churned"] == 71
    assert changed["facts"]["smb_basic_incremental_churn_share"] == "0.8723"
    assert changed["reference_sql_results"] != original["reference_sql_results"]


def test_oracle_reacts_to_mutated_at_risk_denominators() -> None:
    source = _rows()
    removed = next(
        item
        for item in source
        if item["quarter"] == "2026-Q2"
        and item["customer_segment"] == "SMB"
        and item["plan_tier"] == "Basic"
        and item["churned"] == "false"
        and not item["reactivated_at"]
    )

    changed = build_oracle([row for row in source if row is not removed])

    assert changed["facts"]["overall"]["2026-Q2"]["denominator"] == 1_999
    assert changed["facts"]["smb_basic"]["2026-Q2"]["denominator"] == 499
    assert changed["facts"]["smb_basic"]["2026-Q2"]["rate"] == "0.1403"
    assert changed["reference_sql_results"]["overall_churn"][1][1] == 1_999


def test_reference_arithmetic_fails_closed_on_a_zero_denominator(tmp_path: Path) -> None:
    with CSV_PATH.open("rb") as stream:
        loaded = load_tabular_file(stream, CSV_PATH.name)
    stored = SQLiteStore(tmp_path / "zero-denominator.sqlite3").save_dataframe(loaded.dataframe)
    result = execute_read_query(
        stored.database_path,
        "SELECT 1.0 * COUNT(*) / NULLIF(COUNT(*), 0) AS rate "
        "FROM uploaded_data WHERE quarter = '2099-Q1'",
        allowed_tables={stored.table_name},
        allowed_columns={stored.table_name: set(stored.columns)},
    )

    assert result.iloc[0]["rate"] is None or result.iloc[0]["rate"] != result.iloc[0]["rate"]


def test_model_accessible_assets_are_only_the_csv_and_four_pdfs() -> None:
    accessible = {path.as_posix() for path in MODEL_ACCESSIBLE_RELATIVE_PATHS}

    assert accessible == {
        "sample_data/subscription_churn.csv",
        "sample_data/subscription_churn_pricing_change_notice.pdf",
        "sample_data/subscription_churn_support_operations_update.pdf",
        "sample_data/subscription_churn_retention_playbook.pdf",
        "sample_data/subscription_churn_draft_board_memo.pdf",
    }
    assert all(not path.startswith("evals/") for path in accessible)
    assert Decimal(_oracle()["facts"]["mrr_lost"]["2026-Q2"]) == Decimal("32578.00")
