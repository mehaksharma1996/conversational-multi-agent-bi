"""The Docker-only provider must satisfy the same grounding contract as production models."""

from __future__ import annotations

from pathlib import Path

from src.ingestion.pdf_loader import load_pdf_file
from src.llm.structured import HybridCriteria
from src.orchestration.policy_grounding import sanitize_policy_predicates, verify_policy_sql
from tests.e2e.fake_app import BrowserTestLLM


def test_browser_provider_returns_a_source_grounded_structured_predicate() -> None:
    fixture = Path(__file__).parent / "e2e" / "review_policy.pdf"
    with fixture.open("rb") as source:
        document = load_pdf_file(source, fixture.name)
    excerpts = [document.pages[0].text]

    response = BrowserTestLLM().generate("Extract business criteria")
    criteria = HybridCriteria.model_validate_json(response.text).model_dump(exclude_none=True)
    criteria["predicates"] = sanitize_policy_predicates(
        criteria["predicates"],
        excerpts,
        allowed_fields={"amount", "merchant"},
    )

    assert criteria["predicates"] == [
        {
            "field": "amount",
            "operator": "gt",
            "value": 1000,
            "unit": "currency",
            "source": 1,
            "null_behavior": "exclude",
        }
    ]
    verification = verify_policy_sql(
        'SELECT "merchant", "amount" FROM "uploaded_data" WHERE "amount" > 1000',
        criteria,
        user_question="Which uploaded transactions violate the policy?",
    )
    assert verification.status == "structurally_verified"
    assert verification.predicate_count == 1


def test_browser_provider_document_quote_exists_in_its_pdf_fixture() -> None:
    fixture = Path(__file__).parent / "e2e" / "review_policy.pdf"
    with fixture.open("rb") as source:
        document = load_pdf_file(source, fixture.name)

    response = BrowserTestLLM().generate("You are a careful document analyst")

    assert "Amount paid over $1000 requires manager review." in document.pages[0].text
    assert '"Amount paid over $1000 requires manager review."' in response.text
