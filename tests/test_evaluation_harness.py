"""The evaluation harness must be able to fail: negative controls, gates, and baselines.

A suite that always passes proves nothing, so these tests deliberately break each
guard (SQL validation, citation/quote checks, criteria provenance, redaction,
retrieval rejection, tenant ownership) and assert the matching *critical* check
fails.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from packages.evaluation import (
    CRITICAL_CHECKS,
    FixtureError,
    FixtureSet,
    build_baseline,
    evaluate_gates,
    load_fixtures,
    plan_baseline_update,
    run_suite,
)
from packages.evaluation.fakes import HashingEmbedder, ScriptedLLM, UnscriptedPromptError
from packages.evaluation.fixtures import validate_fixtures
from scripts import run_evaluations
from scripts.api_isolation_eval import run_api_isolation_suite

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "evals" / "v1"


@pytest.fixture(scope="module")
def fixtures() -> FixtureSet:
    return load_fixtures(FIXTURE_ROOT)


@pytest.fixture(scope="module")
def full_report(fixtures: FixtureSet) -> dict[str, Any]:
    return run_suite(fixtures, extra_results=run_api_isolation_suite())


def _critical_failures(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {(item["case"], item["check"]) for item in report["aggregate"]["critical_failures"]}


def _only(fixtures: FixtureSet, *case_ids: str) -> dict[str, Any]:
    return run_suite(fixtures, case_filter=lambda case: case["id"] in case_ids)


def test_committed_suite_passes_every_case_and_gate(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = full_report
    baseline = json.loads((FIXTURE_ROOT / "baseline.json").read_text(encoding="utf-8"))

    failed = [case["id"] for case in report["cases"] if not case["passed"]]
    gates = evaluate_gates(report, fixtures.thresholds, baseline)

    assert failed == []
    assert report["aggregate"]["critical_failures"] == []
    assert gates.failures == []


def test_report_is_machine_readable_and_records_required_metadata(fixtures: FixtureSet) -> None:
    report = _only(fixtures, "sql-flagged-count-filter")

    json.loads(json.dumps(report))  # serializable as-is
    metadata = report["metadata"]
    assert metadata["dataset_version"] == "v1"
    assert metadata["evaluator_version"]
    assert metadata["provider"] == "scripted"
    assert metadata["embedding"]["product_model_revision"]
    assert metadata["retrieval_settings"]["top_k"] == 4
    assert set(metadata["prompt_fingerprints"]) == {
        "sql_generation",
        "sql_correction",
        "rag_answer",
        "route_classification",
        "criteria_extraction",
    }
    (case,) = report["cases"]
    assert case["latency_ms"] >= 0
    assert {"crash.none", "route.expected", "safety.sql_valid_read_only"} <= {
        check["name"] for check in case["checks"]
    }
    assert report["aggregate"]["by_capability"]["text_to_sql"]["pass_rate"] == 1.0


def test_report_never_contains_questions_sql_rows_or_document_text(
    fixtures: FixtureSet,
) -> None:
    report = run_suite(fixtures)
    rendered = json.dumps(report)

    for case in fixtures.cases:
        assert case["question"] not in rendered
    for forbidden in ("Epsilon Electronics", "SELECT", "senior analyst", "jane.roe@example.com"):
        assert forbidden not in rendered


def test_every_critical_check_is_exercised_by_the_committed_suite(
    full_report: dict[str, Any],
) -> None:
    exercised = set(full_report["aggregate"]["by_check"])

    assert CRITICAL_CHECKS <= exercised, sorted(CRITICAL_CHECKS - exercised)


# --- negative controls -----------------------------------------------------------------


def test_permissive_sql_guard_is_caught_as_a_critical_failure(fixtures: FixtureSet) -> None:
    with (
        patch("src.agents.sql_agent.validate_read_query", lambda sql: sql),
        patch(
            "src.agents.sql_agent.execute_read_query",
            lambda *args, **kwargs: pd.DataFrame({"unused": [1]}),
        ),
    ):
        report = _only(fixtures, "sql-refuses-drop-table", "sql-refuses-pragma")

    failures = _critical_failures(report)
    assert ("sql-refuses-drop-table", "safety.unsafe_request_refused") in failures
    assert ("sql-refuses-drop-table", "safety.sql_valid_read_only") in failures
    assert ("sql-refuses-pragma", "safety.unsafe_request_refused") in failures


def test_destructive_execution_is_caught_by_the_dataset_integrity_check(
    fixtures: FixtureSet,
) -> None:
    import sqlite3
    from contextlib import closing

    def destructive(database_path: Path, sql: str, **_: Any) -> pd.DataFrame:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute('DELETE FROM "uploaded_data"')
            connection.commit()
        return pd.DataFrame({"unused": [1]})

    with (
        patch("src.agents.sql_agent.validate_read_query", lambda sql: sql),
        patch("src.agents.sql_agent.execute_read_query", destructive),
    ):
        report = _only(fixtures, "sql-refuses-drop-table")

    assert ("sql-refuses-drop-table", "safety.dataset_intact") in _critical_failures(report)


def test_disabled_quote_verification_is_caught(fixtures: FixtureSet) -> None:
    with patch("src.agents.rag_agent._find_unverified_quotes", lambda *args: []):
        report = _only(fixtures, "rag-flags-fabricated-quote")

    assert ("rag-flags-fabricated-quote", "grounding.status") in _critical_failures(report)


def test_disabled_citation_validation_is_caught(fixtures: FixtureSet) -> None:
    import re

    with patch("src.agents.rag_agent._CITATION_PATTERN", re.compile(r"NEVER-MATCHES")):
        report = _only(
            fixtures, "rag-flags-invalid-citation-number", "rag-escalation-threshold-grounded"
        )

    failures = _critical_failures(report)
    assert ("rag-flags-invalid-citation-number", "grounding.status") in failures
    assert ("rag-flags-invalid-citation-number", "grounding.warning_surfaced") in failures


def test_disabled_criteria_sanitization_is_caught(fixtures: FixtureSet) -> None:
    with patch(
        "src.orchestration.langgraph_orchestrator._sanitize_hybrid_criteria",
        lambda criteria, excerpts: criteria,
    ):
        report = _only(
            fixtures,
            "hybrid-untraceable-criterion-is-dropped",
            "hybrid-non-allowlisted-criteria-keys-are-rejected",
        )

    failures = _critical_failures(report)
    assert (
        "hybrid-untraceable-criterion-is-dropped",
        "provenance.untraceable_criteria_excluded",
    ) in failures
    assert (
        "hybrid-non-allowlisted-criteria-keys-are-rejected",
        "provenance.untraceable_criteria_excluded",
    ) in failures


def test_disabled_pii_redaction_is_caught(fixtures: FixtureSet) -> None:
    with (
        patch("src.agents.rag_agent.redact_pii", lambda text: text),
        patch("src.agents.sql_agent.redact_pii", lambda text: text),
        patch("src.orchestration.langgraph_orchestrator.redact_pii", lambda text: text),
    ):
        report = run_suite(
            fixtures, case_filter=lambda case: case["capability"] == "privacy_redaction"
        )

    assert {
        case for case, check in _critical_failures(report) if check == "privacy.prompts_redacted"
    } == {
        "privacy-document-pii-is-redacted-before-the-model",
        "privacy-table-sample-values-are-redacted-before-the-model",
        "privacy-hybrid-excerpts-are-redacted-for-criteria-extraction",
    }


def test_loose_retrieval_distance_lets_off_topic_questions_reach_the_model(
    fixtures: FixtureSet,
) -> None:
    loose = replace(
        fixtures,
        manifest={**fixtures.manifest, "retrieval": {**fixtures.retrieval, "max_distance": 2.0}},
    )

    report = run_suite(loose, case_filter=lambda case: case["id"].startswith("rag-off-topic"))

    assert (
        "rag-off-topic-question-is-refused-before-generation",
        "grounding.no_generation_without_evidence",
    ) in _critical_failures(report)


def test_broken_tenant_ownership_check_is_caught_by_the_isolation_suite() -> None:
    def no_tenant_check(self: Any, records: dict[str, Any], resource_id: str, *_: Any) -> Any:
        from apps.api.errors import ResourceNotFoundError

        if resource_id not in records:
            raise ResourceNotFoundError("Resource")
        return records[resource_id]

    with patch("apps.api.repository.LocalResourceRepository._owned", no_tenant_check):
        results = run_api_isolation_suite()

    failed = {
        check.name
        for result in results
        for check in result.checks
        if check.critical and not check.passed
    }
    assert "isolation.cross_tenant_denied" in failed


def test_unscripted_model_call_is_a_visible_crash_not_a_silent_fallback(
    fixtures: FixtureSet,
) -> None:
    broken = copy.deepcopy(fixtures.cases[0])
    broken["id"] = "broken-fixture"
    broken["script"] = {}
    modified = replace(fixtures, cases=[broken])

    report = run_suite(modified)

    assert ("broken-fixture", "crash.none") in _critical_failures(report)
    with pytest.raises(UnscriptedPromptError):
        ScriptedLLM({}).generate("careful SQLite analyst prompt")


# --- gates and baselines -----------------------------------------------------------------


def test_a_critical_failure_fails_the_gate_even_at_a_perfect_pass_rate(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = copy.deepcopy(full_report)
    baseline = build_baseline(report)
    tampered = copy.deepcopy(report)
    tampered["aggregate"]["critical_failures"] = [
        {"case": "x", "check": "safety.sql_valid_read_only", "detail": ""}
    ]

    gates = evaluate_gates(tampered, fixtures.thresholds, baseline)

    assert not gates.passed
    assert any(failure["gate"] == "critical_check" for failure in gates.failures)


def test_regression_missing_and_unbaselined_cases_fail_the_gate(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = copy.deepcopy(full_report)
    baseline = build_baseline(report)

    regressed = copy.deepcopy(report)
    regressed["cases"][0]["checks"][0]["passed"] = False
    removed = copy.deepcopy(report)
    removed["cases"] = removed["cases"][1:]
    added = copy.deepcopy(report)
    added["cases"].append({**copy.deepcopy(report["cases"][0]), "id": "brand-new-case"})

    def gates_of(candidate: dict[str, Any]) -> set[str]:
        return {
            f["gate"] for f in evaluate_gates(candidate, fixtures.thresholds, baseline).failures
        }

    assert "regression" in gates_of(regressed)
    assert "case_removed" in gates_of(removed)
    assert "case_unbaselined" in gates_of(added)


def test_prompt_dataset_and_retrieval_changes_require_a_baseline_update(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = copy.deepcopy(full_report)
    baseline = build_baseline(report)
    changed = copy.deepcopy(report)
    changed["metadata"]["prompt_fingerprints"]["sql_generation"] = "0" * 16
    changed["metadata"]["retrieval_settings"]["top_k"] = 9
    changed["metadata"]["dataset_revision"] = "other"

    gates = evaluate_gates(changed, fixtures.thresholds, baseline)

    details = [f["detail"] for f in gates.failures if f["gate"] == "baseline_metadata_changed"]
    assert len(details) == 3
    assert any("prompt_fingerprints" in detail for detail in details)


def test_threshold_below_one_needs_a_recorded_waiver(fixtures: FixtureSet) -> None:
    thresholds = {
        **fixtures.thresholds,
        "capability_min_pass_rate": {
            **fixtures.thresholds["capability_min_pass_rate"],
            "memory": 0.9,
        },
    }
    with pytest.raises(FixtureError, match="waiver"):
        validate_fixtures(replace(fixtures, thresholds=thresholds))

    waived = {
        **thresholds,
        "waivers": {
            "memory": {
                "reason": "Known flaky memory wording",
                "owner": "maintainers",
                "issue": "#123",
                "expires": "2026-12-31",
            }
        },
    }
    validate_fixtures(replace(fixtures, thresholds=waived))


def test_critical_failures_must_stay_at_zero_in_thresholds(fixtures: FixtureSet) -> None:
    with pytest.raises(FixtureError, match="critical_failures_allowed"):
        validate_fixtures(
            replace(fixtures, thresholds={**fixtures.thresholds, "critical_failures_allowed": 1})
        )


def test_baseline_update_refuses_critical_failures_and_unexplained_regressions(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = copy.deepcopy(full_report)
    baseline = build_baseline(report)

    critical = copy.deepcopy(report)
    critical["aggregate"]["critical_failures"] = [
        {"case": "c", "check": "grounding.status", "detail": ""}
    ]
    assert not plan_baseline_update(critical, baseline, "because I said so").allowed

    regressed = copy.deepcopy(report)
    regressed["cases"][0]["checks"][0]["passed"] = False
    assert not plan_baseline_update(regressed, baseline).allowed
    assert not plan_baseline_update(regressed, baseline, "   ").allowed
    plan = plan_baseline_update(regressed, baseline, "Intentional policy change, see review")
    assert plan.allowed
    assert plan.regressions[0]["id"] == report["cases"][0]["id"]


def test_accepted_regressions_are_recorded_in_the_baseline_and_tolerated(
    fixtures: FixtureSet, full_report: dict[str, Any]
) -> None:
    report = copy.deepcopy(full_report)
    regressed = copy.deepcopy(report)
    case_id = regressed["cases"][0]["id"]
    regressed["cases"][0]["checks"][0]["passed"] = False
    regressed["cases"][0]["passed"] = False
    regressed["aggregate"]["by_capability"][regressed["cases"][0]["capability"]]["pass_rate"] = 0.5

    accepted = [{"id": case_id, "checks": ["x"], "reason": "documented"}]
    baseline = build_baseline(regressed, accepted_regressions=accepted)

    assert baseline["accepted_regressions"] == accepted
    gates = evaluate_gates(regressed, fixtures.thresholds, baseline)
    assert {f["gate"] for f in gates.failures} == {"capability_threshold"}


# --- fixtures ---------------------------------------------------------------------------


def test_unknown_expectation_keys_are_rejected_instead_of_silently_ignored(
    fixtures: FixtureSet,
) -> None:
    broken = copy.deepcopy(fixtures.cases[0])
    broken["expect"]["sql"]["required_colums"] = ["typo"]

    with pytest.raises(FixtureError, match="unknown keys"):
        validate_fixtures(replace(fixtures, cases=[broken]))


def test_duplicate_case_ids_and_unknown_references_are_rejected(fixtures: FixtureSet) -> None:
    with pytest.raises(FixtureError, match="Duplicate"):
        validate_fixtures(replace(fixtures, cases=[fixtures.cases[0], fixtures.cases[0]]))
    bad = copy.deepcopy(fixtures.cases[0])
    bad["id"] = "bad-reference"
    bad["context"] = {"dataset": "does-not-exist"}
    with pytest.raises(FixtureError, match="unknown dataset"):
        validate_fixtures(replace(fixtures, cases=[bad]))


def test_every_case_documents_its_rationale_and_covers_all_five_routes(
    fixtures: FixtureSet,
) -> None:
    routes = {case["expect"].get("route") for case in fixtures.cases}

    assert {"memory", "sql", "rag", "hybrid", "unsupported"} <= routes
    assert all(case["rationale"].strip() for case in fixtures.cases)
    assert len(fixtures.cases) >= 30


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder()
    first = embedder.embed_texts(["Escalation policy for refunds"])[0]
    second = embedder.embed_texts(["Escalation policy for refunds"])[0]

    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


# --- CLI -----------------------------------------------------------------------------------


def test_cli_writes_a_report_and_returns_zero_when_gates_pass(tmp_path: Path) -> None:
    output = tmp_path / "report.json"

    status = run_evaluations.main(["--output", str(output)])

    assert status == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["gates"]["passed"] is True
    assert payload["aggregate"]["cases"] >= 40


def test_cli_fails_without_a_baseline_and_can_create_one(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"

    assert run_evaluations.main(["--baseline", str(baseline)]) == 1
    assert run_evaluations.main(["--baseline", str(baseline), "--update-baseline"]) == 0
    assert run_evaluations.main(["--baseline", str(baseline)]) == 0


def test_cli_refuses_to_baseline_a_regression_without_a_reason(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    committed = json.loads((FIXTURE_ROOT / "baseline.json").read_text(encoding="utf-8"))
    first_case = next(iter(committed["cases"]))
    committed["cases"][first_case]["checks"]["crash.none"] = True
    committed["cases"][first_case]["checks"]["route.expected"] = True
    baseline_path.write_text(json.dumps(committed), encoding="utf-8")

    with patch(
        "src.orchestration.langgraph_orchestrator.route_question", lambda **_: "unsupported"
    ):
        status = run_evaluations.main(["--baseline", str(baseline_path), "--update-baseline"])

    assert status == 1
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == committed


def test_cli_reports_fixture_errors_with_status_two(tmp_path: Path, capsys: Any) -> None:
    assert run_evaluations.main(["--fixtures", str(tmp_path)]) == 2
    assert "Fixture error" in capsys.readouterr().err


def test_live_mode_is_opt_in_and_needs_the_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv("RUN_LIVE_EVALS", raising=False)

    status = run_evaluations.main(["--live"])

    assert status == 2
    assert "RUN_LIVE_EVALS=1" in capsys.readouterr().err
