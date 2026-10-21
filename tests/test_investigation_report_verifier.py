"""INV-8 S3 (#141): the deterministic report verifier rejects every seeded violation.

Each test starts from a report of the real scripted churn investigation that passes verification.
It seeds one violation, in the structured report or in its rendered narrative, and asserts the
fixed reason code. The verifier recomputes everything from the validated state and the gateway's
payloads and never consults a model.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.investigation.report import (
    ComparisonClaim,
    FactClaim,
    LimitationClaim,
    Recommendation,
    RecommendationClaim,
    ReportSection,
    Sentence,
    TaskClaim,
    VerdictClaim,
    VerifiedReport,
    assemble_report,
    compose_body,
    fallback_narrative,
    render_claim,
    report_inputs,
)
from packages.investigation.report_verifier import verify_report, violation_codes
from packages.investigation.state import restore_state
from tests.investigation_fixtures import churn_plan, running_state, task_record
from tests.investigation_report_fakes import churn_execution, evidence_of


@pytest.fixture(scope="module")
def churn(tmp_path_factory: pytest.TempPathFactory) -> Any:
    e = churn_execution(tmp_path_factory.mktemp("inv8_verifier_index"))
    result = e.run.run()
    inputs = report_inputs(result.state, result.payloads)
    body = compose_body(inputs)
    report = assemble_report(body, fallback_narrative(body, fallback=False), fallback_reason=None)
    return result, inputs, body, report


def codes(report: object, inputs: Any) -> set[str]:
    return set(violation_codes(verify_report(report, inputs)))


def section(report: VerifiedReport, name: str) -> ReportSection:
    return next(item for item in report.sections if item.name == name)


def replace_section(report: VerifiedReport, new: ReportSection) -> VerifiedReport:
    sections = tuple(new if item.name == new.name else item for item in report.sections)
    return report.model_copy(update={"sections": sections})


def add_sentence(
    report: VerifiedReport, body: Any, name: str, claim: Any, text: str | None = None
) -> VerifiedReport:
    """Append a sentence; its text is the reviewed rendering unless ``text`` is given."""
    if text is None:
        rendered = render_claim(claim, body)
        assert rendered is not None
        text = rendered.text
    old = section(report, name)
    sentence = Sentence(claim=claim, text=text)
    return replace_section(report, old.model_copy(update={"sentences": (*old.sentences, sentence)}))


def fact_of(body: Any, task_id: str) -> Any:
    return next(item for item in body.facts if item.task_id == task_id)


# --- the baseline passes -------------------------------------------------------------------------


def test_the_model_assisted_and_fallback_reports_pass(churn: Any) -> None:
    _, inputs, body, report = churn
    assert verify_report(report, inputs) == ()
    fallback = assemble_report(body, fallback_narrative(body), fallback_reason="narrative_invalid")
    assert verify_report(fallback, inputs) == ()
    assert report.mode == "model_assisted" and fallback.mode == "deterministic_fallback"


# --- A. the twelve seeded violations -------------------------------------------------------------


def test_1_a_fabricated_numerical_claim_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    fabricated = add_sentence(
        report, body, "findings", FactClaim(fact_id="f42", direction="increase"), "Measurement f42."
    )
    assert "numeric_unsupported" in codes(fabricated, inputs)
    invented = fact_of(body, "h1_rate").model_copy(update={"id": "f9"})
    assert "numeric_unsupported" in codes(
        report.model_copy(update={"facts": (*report.facts, invented)}), inputs
    )


def test_2_a_numerically_incorrect_value_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    wrong = fact_of(body, "h1_rate")
    altered = wrong.model_copy(
        update={"comparison": wrong.comparison.model_copy(update={"display": "0.1800"})}
    )
    facts = tuple(altered if item.id == wrong.id else item for item in report.facts)
    assert "numeric_mismatch" in codes(report.model_copy(update={"facts": facts}), inputs)
    sign = wrong.model_copy(update={"direction": "decrease"})
    facts = tuple(sign if item.id == wrong.id else item for item in report.facts)
    assert "numeric_mismatch" in codes(report.model_copy(update={"facts": facts}), inputs)


def test_3_an_invalid_citation_id_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    fact = fact_of(body, "h1_rate")
    claim = FactClaim(fact_id=fact.id, direction=fact.direction, evidence_ids=("ev_fabricated1",))
    assert codes(add_sentence(report, body, "findings", claim), inputs) == {"citation_unknown"}


def test_4_a_citation_from_another_investigation_is_rejected(churn: Any) -> None:
    result, inputs, body, report = churn
    fact = fact_of(body, "h1_rate")
    other = restore_state(running_state(with_evidence=True))
    foreign = next(iter(other.evidence))  # real evidence of another investigation's state
    claim = FactClaim(fact_id=fact.id, direction=fact.direction, evidence_ids=(foreign,))
    assert codes(add_sentence(report, body, "findings", claim), inputs) == {"citation_unknown"}
    # The whole report checked against another investigation: its scope does not match.
    scope = result.state.scope.model_copy(update={"investigation_id": "inv_other"})
    elsewhere = report_inputs(result.state.model_copy(update={"scope": scope}), result.payloads)
    assert "scope_mismatch" in codes(report, elsewhere)


def test_5_incorrect_evidence_attribution_is_rejected(churn: Any) -> None:
    result, inputs, body, report = churn
    fact = fact_of(body, "h1_rate")
    h2 = evidence_of(result, "derived", "h2_rate")
    claim = FactClaim(fact_id=fact.id, direction=fact.direction, evidence_ids=(h2,))
    assert codes(add_sentence(report, body, "findings", claim), inputs) == {
        "citation_misattributed"
    }
    moved = fact.model_copy(update={"derived_evidence_id": h2})
    facts = tuple(moved if item.id == fact.id else item for item in report.facts)
    assert "citation_misattributed" in codes(report.model_copy(update={"facts": facts}), inputs)


def test_6_unsupported_causal_language_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    causal = VerdictClaim(
        hypothesis_id="h1", verdict="supported_association", relationship="causation"
    )
    assert codes(add_sentence(report, body, "hypotheses", causal), inputs) == {"causal_claim"}
    fact = fact_of(body, "h1_rate")
    claim = FactClaim(fact_id=fact.id, direction=fact.direction, relationship="causation")
    assert codes(add_sentence(report, body, "findings", claim), inputs) == {"causal_claim"}


def test_7_an_unsupported_recommendation_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    extra = Recommendation(
        id="r9",
        rule_id="complete_test",
        rule_version="inv8-recommendation-rules-v1",
        hypothesis_id="h1",
        limitation_codes=("test_not_completed",),
    )
    tampered = report.model_copy(update={"recommendations": (*report.recommendations, extra)})
    assert "recommendation_unsupported" in codes(tampered, inputs)
    claim = RecommendationClaim(recommendation_id="r9", rule_id="complete_test")
    assert codes(add_sentence(report, body, "recommendations", claim), inputs) == {
        "recommendation_unsupported"
    }


def test_8_a_fabricated_playbook_rule_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    claim = RecommendationClaim(recommendation_id="r1", rule_id="retention_discount_playbook")
    text = "Recommendation r1: offer retention discounts."
    assert "playbook_rule_unknown" in codes(
        add_sentence(report, body, "recommendations", claim, text), inputs
    )
    first = report.recommendations[0]
    invented = first.model_copy(update={"rule_id": "retention_discount_playbook"})
    tampered = report.model_copy(
        update={"recommendations": (invented, *report.recommendations[1:])}
    )
    assert "playbook_rule_unknown" in codes(tampered, inputs)


def test_9_a_modified_hypothesis_verdict_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    rows = {row.hypothesis_id: row for row in report.hypotheses}
    upgraded = rows["h4"].model_copy(
        update={"verdict": "supported_association", "reasons": ("effect_in_claimed_direction",)}
    )
    hypotheses = tuple(upgraded if row.hypothesis_id == "h4" else row for row in report.hypotheses)
    assert "verdict_mismatch" in codes(report.model_copy(update={"hypotheses": hypotheses}), inputs)
    for hypothesis_id in ("h3", "h4"):  # untestable or refuted, never silently upgraded
        claim = VerdictClaim(hypothesis_id=hypothesis_id, verdict="supported_association")
        assert codes(add_sentence(report, body, "hypotheses", claim), inputs) == {
            "verdict_mismatch"
        }


def test_10_an_omitted_mandatory_limitation_is_rejected(churn: Any) -> None:
    _, inputs, _, report = churn
    notes = tuple(item for item in report.notes if item.code != "causation_not_established")
    assert "limitation_missing" in codes(report.model_copy(update={"notes": notes}), inputs)
    for name in ("executive_summary", "limitations"):
        old = section(report, name)
        kept = tuple(
            sentence
            for sentence in old.sentences
            if not (
                isinstance(sentence.claim, LimitationClaim)
                and sentence.claim.code == "causation_not_established"
            )
        )
        stripped = replace_section(report, old.model_copy(update={"sentences": kept}))
        assert codes(stripped, inputs) == {"limitation_missing"}


def test_11_a_contradictory_claim_is_rejected(churn: Any) -> None:
    _, inputs, body, report = churn
    fact = fact_of(body, "h1_rate")
    claim = FactClaim(fact_id=fact.id, direction="decrease")
    assert codes(add_sentence(report, body, "findings", claim), inputs) == {"claim_contradiction"}
    h1, h4 = fact.id, fact_of(body, "h4_rate").id
    wrong = ComparisonClaim(fact_ids=(h1, h4), relation="smaller")  # 0.08 vs 0: larger
    assert codes(add_sentence(report, body, "findings", wrong), inputs) == {"claim_contradiction"}
    right = ComparisonClaim(fact_ids=(h1, h4), relation="larger")
    assert codes(add_sentence(report, body, "findings", right), inputs) == set()


def _cancelled() -> tuple[Any, VerifiedReport]:
    plan = churn_plan()
    tasks: dict[str, Any] = {task.id: task_record(task.id, "cancelled") for task in plan.tasks}
    inputs = report_inputs(restore_state(running_state(plan=plan, status="cancelled", **tasks)), {})
    body = compose_body(inputs)
    report = assemble_report(
        body, fallback_narrative(body), fallback_reason="narrative_not_eligible"
    )
    return inputs, report


def test_12_an_unexecuted_task_presented_as_completed_is_rejected() -> None:
    inputs, report = _cancelled()
    assert verify_report(report, inputs) == ()
    body = compose_body(inputs)
    task = report.tasks[0]
    claim = TaskClaim(task_id=task.task_id, status="succeeded")
    assert codes(add_sentence(report, body, "limitations", claim), inputs) == {
        "task_status_misrepresented"
    }
    done = task.model_copy(update={"status": "succeeded", "category": None})
    tasks = (done, *report.tasks[1:])
    assert "task_status_misrepresented" in codes(report.model_copy(update={"tasks": tasks}), inputs)
    completed = report.model_copy(
        update={"investigation_status": "completed", "availability": "complete"}
    )
    assert "status_misrepresented" in codes(completed, inputs)


# --- V4: adversarial text ------------------------------------------------------------------------


def _retext(report: VerifiedReport, name: str, text: str) -> VerifiedReport:
    old = section(report, name)
    first = old.sentences[0].model_copy(update={"text": text})
    return replace_section(
        report, old.model_copy(update={"sentences": (first, *old.sentences[1:])})
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("SMB Basic churn rose as a result of the pricing change.", "causal_claim"),
        ("The pricing change drove the churn increase.", "causal_claim"),
        ("Churn rose after prices changed, which explains the loss.", "causal_claim"),
        ("The retention playbook was effective.", "causal_claim"),
        ("SMB Basic churn doubled.", "numeric_unsupported"),
        ("SMB Basic churn rose by eighteen percent.", "numeric_unsupported"),
        ("SMB Basic churn rose to 18%.", "numeric_unsupported"),
        ("SMB Basic churn is higher than Enterprise churn.", "unsupported_comparison"),
    ],
)
def test_free_text_cannot_carry_causal_numeric_or_comparative_claims(
    churn: Any, text: str, code: str
) -> None:
    _, inputs, _, report = churn
    found = codes(_retext(report, "findings", text), inputs)
    assert {"rendered_text_mismatch", code} <= found


def test_comparisons_need_two_facts_in_one_unit(churn: Any) -> None:
    _, inputs, body, report = churn
    fact = fact_of(body, "h1_rate").id
    same = ComparisonClaim(fact_ids=(fact, fact), relation="equal")
    assert "unsupported_comparison" in codes(add_sentence(report, body, "findings", same), inputs)
    absent = ComparisonClaim(fact_ids=(fact, "f77"), relation="larger")
    assert "numeric_unsupported" in codes(
        add_sentence(report, body, "findings", absent, "The difference is larger."), inputs
    )


# --- completeness and shape ----------------------------------------------------------------------


def _without(report: VerifiedReport, name: str, kind: type) -> VerifiedReport:
    old = section(report, name)
    kept = tuple(item for item in old.sentences if not isinstance(item.claim, kind))
    return replace_section(report, old.model_copy(update={"sentences": kept}))


@pytest.mark.parametrize(
    ("name", "kind", "code"),
    [
        ("findings", FactClaim, "evidence_omitted"),
        ("hypotheses", VerdictClaim, "hypothesis_omitted"),
        ("recommendations", RecommendationClaim, "recommendation_omitted"),
    ],
)
def test_a_narrative_must_state_what_the_evidence_holds(
    churn: Any, name: str, kind: type, code: str
) -> None:
    _, inputs, _, report = churn
    assert code in codes(_without(report, name, kind), inputs)


def test_material_critic_findings_must_be_stated(churn: Any) -> None:
    _, inputs, _, report = churn
    critique = section(report, "critique")
    assert critique.sentences  # the memo contradiction and the tenure gap
    emptied = replace_section(report, critique.model_copy(update={"sentences": ()}))
    assert codes(emptied, inputs) == {"finding_omitted"}


def test_missing_sections_and_non_reports_are_rejected(churn: Any) -> None:
    _, inputs, _, report = churn
    dropped = report.model_copy(
        update={"sections": tuple(item for item in report.sections if item.name != "critique")}
    )
    assert "section_missing" in codes(dropped, inputs)
    assert codes({"label": "verified"}, inputs) == {"schema_invalid"}
    broken = report.model_copy(update={"fallback_reason": "narrative_invalid"})
    assert codes(broken, inputs) == {"schema_invalid"}


def test_the_verifier_reports_codes_only(churn: Any) -> None:
    _, inputs, _, report = churn
    found = verify_report(_retext(report, "findings", "Churn doubled."), inputs)
    assert all(set(vars(item)) == {"code", "section", "index"} for item in found)
    assert all(isinstance(item.index, int | None) for item in found)


def test_every_reviewed_wording_passes_the_text_scan() -> None:
    from packages.investigation import report as module
    from packages.investigation.interim_report import LIMITATION_TEMPLATES
    from packages.investigation.report_verifier import (
        CAUSAL_PATTERN,
        COMPARATIVE_PATTERN,
        NUMBER_PATTERN,
        _mask,
    )

    wording = [
        *module.NOTE_TEMPLATES.values(),
        *LIMITATION_TEMPLATES.values(),
        *(text for text, _ in module.RECOMMENDATION_RULES.values()),
        *module._STATUS_PHRASES.values(),
        *module._VERDICT_PHRASES.values(),
        *module._FINDING_PHRASES.values(),
        *module._TASK_PHRASES.values(),
        *module._DIRECTION_PHRASES.values(),
        module.UNPROVEN_SUFFIX,
    ]
    for text in wording:
        residual = _mask(text, ())
        assert not CAUSAL_PATTERN.search(residual), text
        assert not NUMBER_PATTERN.search(residual), text
        assert not COMPARATIVE_PATTERN.search(residual), text
