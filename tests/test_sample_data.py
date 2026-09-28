"""Smoke tests for the bundled try-it-now assets."""

from pathlib import Path

from src.ingestion.pdf_loader import load_pdf_file
from src.ingestion.tabular_loader import load_tabular_file


def test_bundled_sample_assets_are_loadable() -> None:
    sample_dir = Path(__file__).resolve().parents[1] / "sample_data"
    with (sample_dir / "transactions.csv").open("rb") as csv_file:
        table = load_tabular_file(csv_file, "transactions.csv")
    with (sample_dir / "review_policy.pdf").open("rb") as pdf_file:
        document = load_pdf_file(pdf_file, "review_policy.pdf")

    assert table.row_count == 8
    assert "transaction_date" in table.dataframe.columns
    assert document.page_count == 1
    assert "manager review" in document.text
