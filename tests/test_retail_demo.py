"""Contract tests for the deterministic retail-operations demo assets."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from scripts.generate_retail_demo import (
    ROW_COUNT,
    build_ground_truth,
    build_rows,
    generate_assets,
)
from src.agents.sql_agent import answer_with_sql, summarize_sql_result
from src.ingestion.pdf_loader import load_pdf_file
from src.ingestion.tabular_loader import load_tabular_file
from src.llm.base import LLMResponse
from src.storage.sqlite_store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "sample_data"


@dataclass
class _FixedSQLClient:
    text: str
    provider: str = "test"
    model: str = "fixed-sql"

    def generate(self, prompt: str) -> LLMResponse:
        return LLMResponse(text=self.text, model=self.model, provider=self.provider)


def _committed_rows() -> list[dict[str, str]]:
    with (SAMPLE_DIR / "retail_operations.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_retail_demo_assets_are_byte_reproducible(tmp_path: Path) -> None:
    generated = generate_assets(tmp_path)

    assert generated["csv"].read_bytes() == (SAMPLE_DIR / "retail_operations.csv").read_bytes()
    assert (
        generated["policy"].read_bytes()
        == (SAMPLE_DIR / "retail_operations_policy.pdf").read_bytes()
    )
    assert (
        generated["ground_truth"].read_bytes()
        == (SAMPLE_DIR / "retail_operations_ground_truth.json").read_bytes()
    )


def test_retail_demo_rows_preserve_arithmetic_and_policy_rules() -> None:
    rows = _committed_rows()

    assert len(rows) == ROW_COUNT
    assert len({row["transaction_id"] for row in rows}) == ROW_COUNT
    assert {row["region"] for row in rows} == {"Central", "East", "North", "West"}
    assert {row["category"] for row in rows} == {
        "Apparel",
        "Beauty",
        "Electronics",
        "Home",
        "Sports",
    }
    assert any(not row["customer_segment"] for row in rows)
    assert any(not row["fulfillment_days"] for row in rows)

    for row in rows:
        revenue = Decimal(row["revenue"])
        cost = Decimal(row["cost"])
        profit = Decimal(row["gross_profit"])
        margin = Decimal(row["gross_margin_rate"])
        assert profit == revenue - cost
        assert abs(margin - profit / revenue) <= Decimal("0.00005")

        expected_review = (
            Decimal(row["discount_rate"]) >= Decimal("0.30")
            or margin < Decimal("0.10")
            or (bool(row["fulfillment_days"]) and int(row["fulfillment_days"]) >= 10)
        )
        assert (row["status"] == "review") is expected_review


def test_retail_ground_truth_is_derived_from_the_committed_csv() -> None:
    rows = _committed_rows()
    committed = json.loads(
        (SAMPLE_DIR / "retail_operations_ground_truth.json").read_text(encoding="utf-8")
    )

    assert committed == build_ground_truth(rows)
    assert committed["generator"]["row_count"] == ROW_COUNT
    assert {case["id"] for case in committed["questions"]} == {
        "fulfillment-escalation-count",
        "h1-year-comparison",
        "missing-segments",
        "peak-month-revenue",
        "policy-return-rule",
        "policy-review-count",
        "top-category-profit",
        "top-region-revenue",
        "total-revenue",
    }


def test_retail_policy_is_loadable_and_matches_the_ground_truth() -> None:
    with (SAMPLE_DIR / "retail_operations_policy.pdf").open("rb") as stream:
        document = load_pdf_file(stream, "retail_operations_policy.pdf")
    ground_truth = json.loads(
        (SAMPLE_DIR / "retail_operations_ground_truth.json").read_text(encoding="utf-8")
    )

    assert document.page_count == 1
    assert "discount_rate of 0.30 or greater" in document.text
    assert "gross_margin_rate below 0.10" in document.text
    assert "fulfillment_days of 10 or greater" in document.text
    assert ground_truth["policy_rules"] == {
        "discount_review_minimum": "0.30",
        "fulfillment_escalation_minimum_days": 10,
        "gross_margin_review_below": "0.10",
        "return_alone_requires_escalation": False,
    }


def test_retail_csv_uses_the_real_ingestion_path() -> None:
    with (SAMPLE_DIR / "retail_operations.csv").open("rb") as stream:
        loaded = load_tabular_file(stream, "retail_operations.csv")

    assert loaded.row_count == ROW_COUNT
    assert loaded.dataframe["revenue"].notna().all()
    assert loaded.dataframe["gross_margin_rate"].notna().all()
    assert loaded.dataframe["customer_segment"].isna().sum() > 0
    assert loaded.dataframe["fulfillment_days"].isna().sum() > 0


def test_generator_returns_fresh_deterministic_rows() -> None:
    first = build_rows()
    second = build_rows()

    assert first == second
    assert first is not second


def test_retail_ground_truth_flows_through_guarded_sql_and_insights(tmp_path: Path) -> None:
    with (SAMPLE_DIR / "retail_operations.csv").open("rb") as stream:
        loaded = load_tabular_file(stream, "retail_operations.csv")
    expected = json.loads(
        (SAMPLE_DIR / "retail_operations_ground_truth.json").read_text(encoding="utf-8")
    )
    expected_answer = next(
        item["answer"] for item in expected["questions"] if item["id"] == "top-region-revenue"
    )
    stored_table = SQLiteStore(tmp_path / "retail-demo.sqlite3").save_dataframe(loaded.dataframe)
    sql = (
        'SELECT "region", ROUND(SUM("revenue"), 2) AS "total_revenue" '
        'FROM "uploaded_data" GROUP BY "region" '
        'ORDER BY "total_revenue" DESC, "region" ASC'
    )

    query_result = answer_with_sql(
        "Which region had the highest total revenue?",
        stored_table,
        _FixedSQLClient(sql),
    )
    summary = summarize_sql_result(
        query_result.result,
        question="Which region had the highest total revenue?",
        sql=query_result.sql,
    )

    top_row = query_result.result.iloc[0]
    assert top_row["region"] == expected_answer["region"]
    assert Decimal(str(top_row["total_revenue"])) == Decimal(expected_answer["total_revenue"])
    assert "highest for region=West at 533,933.01" in summary
    assert "No currency or unit was inferred for total_revenue" in summary
