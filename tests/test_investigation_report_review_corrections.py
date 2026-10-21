"""PR #184 maintainer review corrections (INV-8, #141).

1. The ``observational_data_only`` note states only what the trusted methodology guarantees. It
   no longer asserts that no controlled comparison was performed. Causation stays a separate,
   mandatory disclosure.
2. ``rerun_investigation`` never simply recommends running again when a documented security,
   authorization, policy, or resource restriction stopped the run. It then names the blocker and
   recommends running again only after the blocker is resolved. The verifier enforces that
   trigger.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation import InvestigationStatus
from packages.investigation.contracts import TaskOutcome
from packages.investigation.gateway import AuthorityStatus
from packages.investigation.report import (
    BLOCKED_RERUN_TEXT,
    NOTE_TEMPLATES,
    RECOMMENDATION_RULES,
    RULES_VERSION,
    Recommendation,
    RecommendationClaim,
    assemble_report,
    compose_body,
    fallback_narrative,
    report_inputs,
    run_blocker,
)
from packages.investigation.report_verifier import verify_report, violation_codes
from packages.investigation.state import TaskRecord, restore_state
from tests.investigation_execution_fakes import execution, reset_replay_registry
from tests.investigation_fixtures import churn_plan, running_state, task_record

S = InvestigationStatus
PLAIN_RERUN = RECOMMENDATION_RULES["rerun_investigation"][0]


# --- 1. the observational note --------------------------------------------------------------------


def test_the_observational_note_claims_nothing_about_controlled_comparisons() -> None:
    text = NOTE_TEMPLATES["observational_data_only"]
    assert "controlled" not in text.lower() and "was run" not in text.lower()
    assert "observational" in text and "association only" in text
    # Causation remains its own mandatory disclosure, in every report.
    assert NOTE_TEMPLATES["causation_not_established"].endswith("cannot establish causation.")


def test_every_report_still_states_that_causation_is_not_established() -> None:
    inputs = report_inputs(restore_state(running_state(status="cancelled", **_cancelled())), {})
    body = compose_body(inputs)
    report = assemble_report(
        body, fallback_narrative(body), fallback_reason="narrative_not_eligible"
    )
    assert report.notes[0].code == "causation_not_established"
    assert verify_report(report, inputs) == ()
    stripped = report.model_copy(update={"notes": report.notes[1:]})
    assert "limitation_missing" in violation_codes(verify_report(stripped, inputs))


# --- 2. reruns are conditional on resolving a documented blocker ---------------------------------


def _cancelled() -> dict[str, Any]:
    return {task.id: task_record(task.id, "cancelled") for task in churn_plan().tasks}


def _ended(status: str, category: str) -> Any:
    """A run whose first task ended unsuccessfully with ``category``; the rest were closed."""
    tasks: dict[str, Any] = _cancelled()
    first = churn_plan().tasks[0].id
    ended = "refused" if category == "policy_refused" else "failed"
    tasks[first] = TaskRecord(
        task_id=first,
        status=ended,
        attempts=1 if ended == "failed" else 0,
        outcome=TaskOutcome(
            task_id=first,
            status=ended,
            error_category=category,
            attempts=1 if ended == "failed" else 0,
        ),
    ).model_dump(mode="json")
    return report_inputs(restore_state(running_state(status=status, **tasks)), {})


def _rerun(inputs: Any) -> Recommendation:
    body = compose_body(inputs)
    return next(item for item in body.recommendations if item.rule_id == "rerun_investigation")


def _text(inputs: Any) -> str:
    body = compose_body(inputs)
    report = assemble_report(
        body, fallback_narrative(body), fallback_reason="narrative_not_eligible"
    )
    assert verify_report(report, inputs) == ()
    section = next(item for item in report.sections if item.name == "recommendations")
    return next(
        sentence.text
        for sentence in section.sentences
        if isinstance(sentence.claim, RecommendationClaim)
        and sentence.claim.rule_id == "rerun_investigation"
    )


@pytest.mark.parametrize(
    ("category", "phrase"),
    [
        ("policy_refused", "security, authorization, or policy restriction"),
        ("resource_unavailable", "resource restriction"),
        ("unsafe_query", "query-safety restriction"),
        ("provider_configuration", "model-provider configuration restriction"),
    ],
)
def test_a_documented_blocker_makes_the_rerun_conditional(category: str, phrase: str) -> None:
    inputs = _ended("failed", category)
    assert run_blocker(inputs) == category
    rerun = _rerun(inputs)
    assert rerun.blocker == category
    assert rerun.limitation_codes == ("investigation_failed", category)
    text = _text(inputs)
    assert phrase in text and "only after that blocker is resolved" in text
    assert PLAIN_RERUN not in text
    assert RULES_VERSION == "inv8-recommendation-rules-v2" == rerun.rule_version


@pytest.mark.parametrize(
    ("status", "category"),
    [
        ("cancelled", "cancelled"),
        ("budget_exhausted", "budget_exhausted"),
        ("failed", "timeout"),
        ("failed", "provider_failure"),
        ("failed", "internal"),
        # An earlier recoverable task failure is not the cause of an owner-cancelled or
        # exhausted run.
        ("cancelled", "resource_unavailable"),
        ("budget_exhausted", "policy_refused"),
    ],
)
def test_without_a_blocker_a_plain_rerun_is_recommended(status: str, category: str) -> None:
    inputs = _ended(status, category)
    assert run_blocker(inputs) is None
    rerun = _rerun(inputs)
    run_code = {"cancelled": "investigation_cancelled", "failed": "investigation_failed"}
    assert rerun.blocker is None
    assert rerun.limitation_codes == (run_code.get(status, status),)
    assert PLAIN_RERUN in _text(inputs)


def test_the_verifier_rejects_an_unconditional_rerun_for_a_blocked_run() -> None:
    inputs = _ended("failed", "policy_refused")
    body = compose_body(inputs)
    report = assemble_report(
        body, fallback_narrative(body), fallback_reason="narrative_not_eligible"
    )
    rerun = _rerun(inputs)
    plain = rerun.model_copy(
        update={"blocker": None, "limitation_codes": ("investigation_failed",)}
    )
    recommendations = tuple(
        plain if item.id == rerun.id else item for item in report.recommendations
    )
    tampered = report.model_copy(update={"recommendations": recommendations})
    assert "recommendation_unsupported" in violation_codes(verify_report(tampered, inputs))
    section = next(item for item in report.sections if item.name == "recommendations")
    sentences = tuple(
        sentence.model_copy(update={"text": f"Recommendation {rerun.id}: {PLAIN_RERUN}"})
        if isinstance(sentence.claim, RecommendationClaim)
        else sentence
        for sentence in section.sentences
    )
    retexted = report.model_copy(
        update={
            "sections": tuple(
                section.model_copy(update={"sentences": sentences})
                if item.name == "recommendations"
                else item
                for item in report.sections
            )
        }
    )
    assert "rendered_text_mismatch" in violation_codes(verify_report(retexted, inputs))


def test_only_a_rerun_names_a_blocker_and_it_must_cite_it() -> None:
    with pytest.raises(ValidationError):
        Recommendation(
            id="r1",
            rule_id="complete_test",
            rule_version=RULES_VERSION,
            limitation_codes=("policy_refused",),
            blocker="policy_refused",
        )
    with pytest.raises(ValidationError):
        Recommendation(
            id="r1",
            rule_id="rerun_investigation",
            rule_version=RULES_VERSION,
            limitation_codes=("investigation_failed",),
            blocker="policy_refused",
        )


def test_the_blocked_wording_passes_the_text_scan() -> None:
    from packages.investigation.report import BLOCKER_PHRASES
    from packages.investigation.report_verifier import (
        CAUSAL_PATTERN,
        COMPARATIVE_PATTERN,
        NUMBER_PATTERN,
        _mask,
    )

    for phrase in BLOCKER_PHRASES.values():
        residual = _mask(BLOCKED_RERUN_TEXT.format(blocker=phrase), ())
        assert not CAUSAL_PATTERN.search(residual)
        assert not NUMBER_PATTERN.search(residual)
        assert not COMPARATIVE_PATTERN.search(residual)


# --- through the real graph -----------------------------------------------------------------------


def test_a_revoked_authority_yields_a_conditional_rerun_in_the_real_graph() -> None:
    reset_replay_registry()
    e = execution()
    # An authorization blocker mid-run: revoked while the first query runs, so the gateway's
    # post-call check refuses and records it (investigation-terminal ``authority_revoked``).
    e.executor.after = lambda: setattr(e.live, "status", AuthorityStatus.REVOKED)
    result = e.run.run()
    view = result.state
    assert view is not None
    assert "authority_revoked" in {item.refusal_code for item in view.decisions}
    assert result.status is S.FAILED and result.report is not None
    rerun = next(
        item for item in result.report.recommendations if item.rule_id == "rerun_investigation"
    )
    assert rerun.blocker == "policy_refused"
    assert view.report_record is not None and view.report_record.verification == "passed"


def test_an_unadmitted_provider_yields_a_conditional_rerun_in_the_real_graph() -> None:
    from src.llm.gemini_client import GeminiClient

    reset_replay_registry()
    client = GeminiClient.__new__(GeminiClient)  # never configured, never called
    result = execution(llm=client).run.run()
    assert result.status is S.FAILED and result.report is not None
    rerun = next(
        item for item in result.report.recommendations if item.rule_id == "rerun_investigation"
    )
    assert rerun.blocker == "provider_configuration"
