"""Generate the deterministic retail-operations demonstration assets.

The generated data is entirely synthetic.  Keeping the generator, policy, and
ground truth together makes the primary demo reproducible and prevents the
document rules from drifting away from the tabular data.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

SEED = 20261011
ROW_COUNT = 2400
START_DATE = date(2025, 1, 1)
END_DATE = date(2026, 6, 30)
MONEY = Decimal("0.01")
RATE = Decimal("0.0001")

REGIONS = ("Central", "East", "North", "West")
CATEGORIES = ("Apparel", "Beauty", "Electronics", "Home", "Sports")
CHANNELS = ("Marketplace", "Retail", "Wholesale")
SEGMENTS = ("Enterprise", "Growth", "Small Business")

CATEGORY_PRICE = {
    "Apparel": Decimal("58.00"),
    "Beauty": Decimal("34.00"),
    "Electronics": Decimal("245.00"),
    "Home": Decimal("112.00"),
    "Sports": Decimal("86.00"),
}
CATEGORY_COST_RATE = {
    "Apparel": Decimal("0.54"),
    "Beauty": Decimal("0.48"),
    "Electronics": Decimal("0.71"),
    "Home": Decimal("0.61"),
    "Sports": Decimal("0.58"),
}
REGION_PRICE_FACTOR = {
    "Central": Decimal("0.98"),
    "East": Decimal("1.05"),
    "North": Decimal("1.00"),
    "West": Decimal("1.12"),
}

FIELDNAMES = (
    "transaction_id",
    "transaction_date",
    "region",
    "category",
    "channel",
    "customer_segment",
    "units",
    "unit_price",
    "discount_rate",
    "revenue",
    "cost",
    "gross_profit",
    "gross_margin_rate",
    "fulfillment_days",
    "returned",
    "status",
)

POLICY_LINES = (
    "Retail Operations Review Policy",
    "Effective date: 2025-01-01. Applies only to the synthetic demo dataset.",
    "Finance review: a discount_rate of 0.30 or greater requires review.",
    "Margin review: a gross_margin_rate below 0.10 requires senior finance review.",
    "Operations escalation: fulfillment_days of 10 or greater requires escalation.",
    "A blank fulfillment_days value is unknown and must not be treated as meeting the threshold.",
    "A returned transaction does not by itself require escalation.",
    (
        "status=review means at least one rule above matched; it is not a finding of fraud "
        "or misconduct."
    ),
    "gross_margin_rate is gross_profit divided by revenue when revenue is greater than zero.",
    "Reported patterns are observations. The data does not establish why a result occurred.",
)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    return value.quantize(RATE, rounding=ROUND_HALF_UP)


def _weighted_choice(rng: random.Random, values: tuple[str, ...], weights: tuple[int, ...]) -> str:
    return rng.choices(values, weights=weights, k=1)[0]


def build_rows(seed: int = SEED, row_count: int = ROW_COUNT) -> list[dict[str, str]]:
    """Return reproducible rows with trends, nulls, and policy-defined review cases."""
    rng = random.Random(seed)
    day_span = (END_DATE - START_DATE).days
    rows: list[dict[str, str]] = []

    for index in range(1, row_count + 1):
        transaction_date = START_DATE + timedelta(days=rng.randint(0, day_span))
        region = _weighted_choice(rng, REGIONS, (21, 29, 19, 31))
        category = _weighted_choice(rng, CATEGORIES, (22, 16, 24, 21, 17))
        channel = _weighted_choice(rng, CHANNELS, (31, 49, 20))
        segment = _weighted_choice(rng, SEGMENTS, (18, 34, 48))
        if index % 97 == 0:
            segment = ""

        units = rng.randint(1, 7)
        if channel == "Wholesale":
            units += rng.randint(4, 12)

        month_index = (transaction_date.year - START_DATE.year) * 12 + transaction_date.month - 1
        trend_factor = Decimal("1") + Decimal(month_index) * Decimal("0.006")
        seasonal_factor = Decimal("1.13") if transaction_date.month in {11, 12} else Decimal("1")
        price_jitter = Decimal(rng.randint(-700, 900)) / Decimal("10000")
        unit_price = _money(
            CATEGORY_PRICE[category]
            * REGION_PRICE_FACTOR[region]
            * trend_factor
            * seasonal_factor
            * (Decimal("1") + price_jitter)
        )

        discount_rate = Decimal(
            _weighted_choice(rng, ("0", "0.05", "0.10", "0.15", "0.20"), (28, 27, 24, 15, 6))
        )
        if index % 211 == 0:
            discount_rate = Decimal("0.35")

        revenue = _money(Decimal(units) * unit_price * (Decimal("1") - discount_rate))
        cost_rate = CATEGORY_COST_RATE[category] + Decimal(rng.randint(-250, 250)) / Decimal(
            "10000"
        )
        if index % 257 == 0:
            cost_rate = Decimal("0.94")
        cost = _money(Decimal(units) * unit_price * cost_rate)
        gross_profit = _money(revenue - cost)
        gross_margin_rate = _rate(gross_profit / revenue) if revenue else Decimal("0")

        fulfillment_days_value = rng.randint(2, 7) + (1 if region in {"Central", "North"} else 0)
        if index % 173 == 0:
            fulfillment_days_value = rng.randint(10, 16)
        fulfillment_days = "" if index % 389 == 0 else str(fulfillment_days_value)

        returned = index % 29 == 0 or (category == "Apparel" and index % 41 == 0)
        requires_review = (
            discount_rate >= Decimal("0.30")
            or gross_margin_rate < Decimal("0.10")
            or (bool(fulfillment_days) and int(fulfillment_days) >= 10)
        )

        rows.append(
            {
                "transaction_id": f"RO-{index:05d}",
                "transaction_date": transaction_date.isoformat(),
                "region": region,
                "category": category,
                "channel": channel,
                "customer_segment": segment,
                "units": str(units),
                "unit_price": f"{unit_price:.2f}",
                "discount_rate": f"{discount_rate:.2f}",
                "revenue": f"{revenue:.2f}",
                "cost": f"{cost:.2f}",
                "gross_profit": f"{gross_profit:.2f}",
                "gross_margin_rate": f"{gross_margin_rate:.4f}",
                "fulfillment_days": fulfillment_days,
                "returned": "true" if returned else "false",
                "status": "review" if requires_review else "approved",
            }
        )
    return rows


def _sum_money(rows: list[dict[str, str]], field: str) -> Decimal:
    return _money(sum((Decimal(row[field]) for row in rows), Decimal("0")))


def build_ground_truth(rows: list[dict[str, str]], seed: int = SEED) -> dict[str, Any]:
    """Calculate exact answers from the rows instead of hand-maintaining demo claims."""
    revenue_by_region: defaultdict[str, Decimal] = defaultdict(Decimal)
    profit_by_category: defaultdict[str, Decimal] = defaultdict(Decimal)
    revenue_by_month: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        revenue_by_region[row["region"]] += Decimal(row["revenue"])
        profit_by_category[row["category"]] += Decimal(row["gross_profit"])
        revenue_by_month[row["transaction_date"][:7]] += Decimal(row["revenue"])

    top_region, top_region_revenue = max(revenue_by_region.items(), key=lambda item: item[1])
    top_category, top_category_profit = max(profit_by_category.items(), key=lambda item: item[1])
    top_month, top_month_revenue = max(revenue_by_month.items(), key=lambda item: item[1])
    review_rows = [row for row in rows if row["status"] == "review"]
    late_rows = [
        row for row in rows if row["fulfillment_days"] and int(row["fulfillment_days"]) >= 10
    ]
    h1_2025 = [row for row in rows if "2025-01-01" <= row["transaction_date"] <= "2025-06-30"]
    h1_2026 = [row for row in rows if "2026-01-01" <= row["transaction_date"] <= "2026-06-30"]
    h1_2025_revenue = _sum_money(h1_2025, "revenue")
    h1_2026_revenue = _sum_money(h1_2026, "revenue")
    h1_change = _money(h1_2026_revenue - h1_2025_revenue)
    h1_change_rate = _rate(h1_change / h1_2025_revenue)

    return {
        "schema_version": 1,
        "scenario": "retail_operations",
        "generator": {
            "seed": seed,
            "row_count": len(rows),
            "date_start": START_DATE.isoformat(),
            "date_end": END_DATE.isoformat(),
        },
        "policy_rules": {
            "discount_review_minimum": "0.30",
            "gross_margin_review_below": "0.10",
            "fulfillment_escalation_minimum_days": 10,
            "return_alone_requires_escalation": False,
        },
        "questions": [
            {
                "id": "total-revenue",
                "question": "What is total revenue across the dataset?",
                "answer": {"total_revenue": f"{_sum_money(rows, 'revenue'):.2f}"},
            },
            {
                "id": "top-region-revenue",
                "question": "Which region has the highest total revenue?",
                "answer": {
                    "region": top_region,
                    "total_revenue": f"{_money(top_region_revenue):.2f}",
                },
            },
            {
                "id": "top-category-profit",
                "question": "Which category generated the most gross profit?",
                "answer": {
                    "category": top_category,
                    "gross_profit": f"{_money(top_category_profit):.2f}",
                },
            },
            {
                "id": "peak-month-revenue",
                "question": "Which month had the highest revenue?",
                "answer": {
                    "month": top_month,
                    "total_revenue": f"{_money(top_month_revenue):.2f}",
                },
            },
            {
                "id": "h1-year-comparison",
                "question": "How did first-half revenue change from 2025 to 2026?",
                "answer": {
                    "h1_2025_revenue": f"{h1_2025_revenue:.2f}",
                    "h1_2026_revenue": f"{h1_2026_revenue:.2f}",
                    "absolute_change": f"{h1_change:.2f}",
                    "change_rate": f"{h1_change_rate:.4f}",
                },
            },
            {
                "id": "policy-review-count",
                "question": "How many transactions meet at least one documented review rule?",
                "answer": {"review_transaction_count": len(review_rows)},
            },
            {
                "id": "fulfillment-escalation-count",
                "question": "How many transactions meet the policy fulfillment escalation rule?",
                "answer": {"transaction_count": len(late_rows), "minimum_days": 10},
            },
            {
                "id": "missing-segments",
                "question": "How many transactions have an unknown customer segment?",
                "answer": {
                    "missing_customer_segment_count": sum(
                        not row["customer_segment"] for row in rows
                    )
                },
            },
            {
                "id": "policy-return-rule",
                "question": "Does a return by itself require escalation?",
                "answer": {
                    "requires_escalation": False,
                    "source_text": "A returned transaction does not by itself require escalation.",
                },
            },
        ],
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_policy_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(path), pagesize=letter, invariant=1, pageCompression=0)
    document.setTitle("Retail Operations Review Policy")
    text = document.beginText(54, 738)
    text.setFont("Helvetica", 10)
    text.setLeading(18)
    for line in POLICY_LINES:
        text.textLine(line)
    document.drawText(text)
    document.save()


def generate_assets(output_dir: Path) -> dict[str, Path]:
    rows = build_rows()
    paths = {
        "csv": output_dir / "retail_operations.csv",
        "policy": output_dir / "retail_operations_policy.pdf",
        "ground_truth": output_dir / "retail_operations_ground_truth.json",
    }
    write_csv(paths["csv"], rows)
    write_policy_pdf(paths["policy"])
    paths["ground_truth"].write_text(
        json.dumps(build_ground_truth(rows), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "sample_data",
    )
    args = parser.parse_args()
    for name, path in generate_assets(args.output_dir).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
