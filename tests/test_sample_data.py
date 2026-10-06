"""Smoke tests for the bundled try-it-now assets."""

from pathlib import Path

from src.analytics.supervised_classification import run_supervised_classification
from src.ingestion.pdf_loader import load_pdf_file
from src.ingestion.tabular_loader import load_tabular_file
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema


def test_bundled_sample_assets_are_loadable() -> None:
    sample_dir = Path(__file__).resolve().parents[1] / "sample_data"
    with (sample_dir / "transactions.csv").open("rb") as csv_file:
        table = load_tabular_file(csv_file, "transactions.csv")
    with (sample_dir / "labelled_transactions.csv").open("rb") as csv_file:
        labelled_table = load_tabular_file(csv_file, "labelled_transactions.csv")
    with (sample_dir / "review_policy.pdf").open("rb") as pdf_file:
        document = load_pdf_file(pdf_file, "review_policy.pdf")

    assert table.row_count == 8
    assert "transaction_date" in table.dataframe.columns
    assert labelled_table.row_count == 160
    assert labelled_table.dataframe["label"].value_counts().to_dict() == {0: 93, 1: 67}
    labelled_profile = profile_dataframe(labelled_table.dataframe)
    labelled_mapping = map_schema(labelled_profile, overrides={"label": "label"})
    classification = run_supervised_classification(
        labelled_table.dataframe,
        labelled_profile,
        labelled_mapping,
    )
    assert classification.enabled
    assert classification.metrics is not None
    assert document.page_count == 1
    assert "manager review" in document.text
