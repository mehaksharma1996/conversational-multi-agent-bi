"""The scripted workflow baseline: metrics, coverage, safety invariants, comparison, and updates.

One full scripted run of all workflow cases (module fixture) is compared byte-for-byte with the
committed baseline; this is the CI check. Everything else uses small fabricated evidence, so each
rule is tested on its own, including every way evidence can be missing.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from packages.evaluation import workflow_baseline as wb
from packages.evaluation.live_demo import DIMENSIONS, load_oracle
from packages.evaluation.workflow_baseline import (
    BASELINE_SCHEMA,
    EVIDENCE_KIND,
    SAFETY_INVARIANTS,
    BaselineError,
    DatasetSnapshot,
    HttpExchange,
    ModelEvidence,
    RunEvidence,
    TracedStatement,
    build_baseline,
    combine_case_documents,
    compare_baselines,
    compute_metrics,
    decide_update,
    evaluate_safety,
    investigation_observations,
    render,
    snapshot_datasets,
    statement_is_read_only,
    wilson_interval,
    with_review,
)
from packages.evaluation.workflow_cases import load_json
from tests.e2e import workflow_baseline_runner as runner
from tests.e2e.retail_app import MODEL, PROVIDER, ScriptedRetailLLM
from tests.test_utils import isolated_directory_path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "evals" / "baseline" / "v2" / "workflow-baseline.json"
MANIFEST = load_json(ROOT / "evals" / "baseline" / "v2" / "manifest.json")
INVESTIGATIONS = load_json(ROOT / "evals" / "baseline" / "v2" / "investigation_cases.json")
SCHEMA = [
    'CREATE TABLE "uploaded_data" ("region" TEXT, "revenue" REAL, "fulfillment_days" INTEGER)'
]


@pytest.fixture(scope="module")
def scripted_run() -> dict[str, Any]:
    return runner.run_baseline(isolated_directory_path("workflow_baseline_full"))


# ---- the committed baseline (CI check) -------------------------------------------------------


def test_a_fresh_scripted_run_matches_the_committed_baseline(scripted_run: dict[str, Any]) -> None:
    committed = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    comparison = compare_baselines(committed, scripted_run)
    assert comparison.identical, "\n".join(
        c.describe() for c in (*comparison.critical, *comparison.noncritical)
    )
    assert wb.safety_failures(scripted_run) == []
    # The committed file is exactly what the renderer writes, plus its review record.
    assert render({**scripted_run, "review": committed["review"]}) == BASELINE_PATH.read_text(
        encoding="utf-8"
    )


def test_the_full_run_covers_all_eighteen_cases_honestly(scripted_run: dict[str, Any]) -> None:
    metrics = scripted_run["metrics"]
    assert metrics["cases"] == len(MANIFEST["cases"]) == 18
    assert (metrics["measured"], metrics["passed"], metrics["failed"]) == (9, 9, 0)
    assert (metrics["not_attempted"], metrics["not_measured"], metrics["attempted"]) == (0, 9, 18)
    for case in scripted_run["cases"]:
        if case["coverage"] != "measured":
            assert case["outcome"] == "not_measured"
            assert case["dimensions"] == {}
    assert metrics["overall"]["wilson_95"] == [0.700855, 1.0]


def test_the_baseline_is_labelled_scripted_regression_evidence(
    scripted_run: dict[str, Any],
) -> None:
    assert scripted_run["schema"] == BASELINE_SCHEMA
    assert scripted_run["evidence"]["kind"] == EVIDENCE_KIND
    assert scripted_run["evidence"]["model_is_live"] is False
    assert "not a measurement of model accuracy" in scripted_run["evidence"]["statement"]
    provenance = scripted_run["provenance"]
    assert (provenance["provider"], provenance["model"]) == (PROVIDER, MODEL)
    assert provenance["mode"] == "scripted-baseline"
    assert provenance["manifest_version"] == MANIFEST["manifest_version"]
    assert provenance["fixtures"] == MANIFEST["fixtures"]
    assert provenance["sources"] == MANIFEST["sources"]
    assert scripted_run["safety_evidence"]["no_live_model"]["network_attempts"] == 0


def test_the_baseline_holds_no_unstable_or_sensitive_values(scripted_run: dict[str, Any]) -> None:
    text = render(scripted_run)
    for forbidden in ("SELECT", "uploaded_data", "1625730.59", "West", runner.SCRIPTED_SECRET):
        assert forbidden not in text
    for key in ("date_utc", "wall_ms", "prompt_tokens", "estimated_cost_usd", "answer_excerpt"):
        assert f'"{key}"' not in text


def test_investigation_cases_are_run_and_observed_but_not_scored(
    scripted_run: dict[str, Any],
) -> None:
    investigations = [c for c in scripted_run["cases"] if c["category"] == "investigation"]
    assert len(investigations) == 3
    for case in investigations:
        assert case["attempted"] is True
        assert case["outcome"] == "not_measured"
        assert {"expected_cells_contained", "causal_marker_count"} <= set(case["observations"])


def test_every_sql_answer_was_seen_by_the_trace(scripted_run: dict[str, Any]) -> None:
    evidence = scripted_run["safety_evidence"]["read_only_sql"]
    assert evidence["statements"] >= evidence["answers_with_execution"] > 0


# ---- statistics -------------------------------------------------------------------------------


def test_wilson_interval_matches_known_values() -> None:
    assert wilson_interval(9, 9) == (0.700855, 1.0)
    low, high = wilson_interval(5, 10)  # type: ignore[misc]
    assert (round(low, 4), round(high, 4)) == (0.2366, 0.7634)
    assert wilson_interval(0, 4)[0] == 0.0  # type: ignore[index]
    assert wilson_interval(0, 0) is None


@pytest.mark.parametrize(("passed", "trials"), [(-1, 3), (4, 3), (0, -1)])
def test_wilson_interval_rejects_impossible_counts(passed: int, trials: int) -> None:
    with pytest.raises(ValueError):
        wilson_interval(passed, trials)


def _case(case_id: str, coverage: str, outcome: str, category: str = "sql") -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "coverage": coverage,
        "attempted": outcome not in ("not_attempted",),
        "outcome": outcome,
        "dimensions": ({"routing": outcome == "passed"} if outcome in ("passed", "failed") else {}),
    }


def test_every_measured_case_is_in_the_denominator_including_failures_and_unattempted() -> None:
    metrics = compute_metrics(
        [
            _case("a", "measured", "passed"),
            _case("b", "measured", "failed"),
            _case("c", "measured", "not_attempted"),
            _case("d", "not_covered_by_script", "not_measured"),
            _case("e", "requires_agent", "not_measured", "investigation"),
            _case("f", "requires_live_model", "not_measured", "exploratory"),
            _case("g", "unsupported", "not_measured", "exploratory"),
        ]
    )
    assert metrics["overall"]["measured"] == 3
    assert metrics["overall"]["passed"] == 1
    assert metrics["overall"]["pass_rate"] == round(1 / 3, 6)
    assert metrics["overall"]["wilson_95"] == list(wilson_interval(1, 3))  # type: ignore[arg-type]
    assert (metrics["failed"], metrics["not_attempted"], metrics["not_measured"]) == (1, 1, 4)
    assert metrics["measured_attempted"] == 2
    assert metrics["by_coverage"] == {
        "measured": 3,
        "not_covered_by_script": 1,
        "requires_agent": 1,
        "requires_live_model": 1,
        "unsupported": 1,
    }
    assert metrics["by_category"]["sql"]["measured"] == 3
    assert metrics["by_category"]["exploratory"]["pass_rate"] is None
    assert metrics["dimensions"]["routing"] == {"passed": 1, "applicable": 2}


# ---- building from a fabricated run ------------------------------------------------------------


def _manifest(entries: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "schema": "workflow-case-manifest-v1",
        "manifest_version": "test",
        "fixtures": {},
        "sources": {},
        "cases": [
            {"id": i, "source": "s", "category": cat, "coverage": cov} for i, cat, cov in entries
        ],
    }


def _message_exchange(index: int) -> HttpExchange:
    body = {"route": "sql", "status": "complete", "sql": "SELECT 1", "rows": [{"n": 1}]}
    return HttpExchange(
        index, "POST", "/api/v1/conversations/c/messages", 201, json.dumps(body).encode()
    )


def _evidence(
    records: list[dict[str, Any]], not_attempted: Sequence[str] = (), **overrides: Any
) -> RunEvidence:
    exchanges = [HttpExchange(0, "POST", "/api/v1/workspaces", 201, b"{}")] + [
        _message_exchange(i + 1) for i in range(len(records))
    ]
    snapshot = DatasetSnapshot({"/db": "abc"}, SCHEMA, 1)
    values: dict[str, Any] = {
        "report": {
            "schema": "live-demo-eval-v2",
            "metadata": {
                "mode": "scripted-baseline",
                "model_is_live": False,
                "provider": PROVIDER,
                "model": MODEL,
            },
            "summary": {"cases_not_attempted": list(not_attempted), "model_calls": 2},
            "cases": records,
        },
        "exchanges": exchanges,
        "statements": [
            TracedStatement(i + 1, "/db", True, 'SELECT "region" FROM "uploaded_data"')
            for i in range(len(records))
        ],
        "dataset_before": snapshot,
        "dataset_after": snapshot,
        "fixtures_before": {"csv": "1"},
        "fixtures_after": {"csv": "1"},
        "model": ModelEvidence(PROVIDER, MODEL, 2, []),
        "secrets": ["sk-known"],
        "forbidden_fragments": ["system prompt text"],
    }
    values.update(overrides)
    return RunEvidence(**values)


def _record(case_id: str, passed: bool | None, **checks: bool | None) -> dict[str, Any]:
    return {
        "id": case_id,
        "passed": passed,
        "checks": checks or {"routing": bool(passed)},
        "http_status": 201,
        "sql": "SELECT 1",
    }


def test_measured_failures_and_unattempted_cases_are_never_passes() -> None:
    manifest = _manifest(
        [
            ("a", "sql", "measured"),
            ("b", "sql", "measured"),
            ("c", "sql", "measured"),
            ("d", "sql", "measured"),
        ]
    )
    definitions = {i: {"id": i, "kind": "ground_truth"} for i in "abcd"}
    records = [_record("a", True), _record("b", False), {**_record("d", None), "checks": {}}]
    baseline = build_baseline(manifest, "sha", definitions, _evidence(records, ["c"]))
    outcomes = {c["id"]: c["outcome"] for c in baseline["cases"]}
    # d had no applicable check: it cannot pass, so it fails closed.
    assert outcomes == {"a": "passed", "b": "failed", "c": "not_attempted", "d": "failed"}
    assert baseline["metrics"]["overall"]["measured"] == 4
    assert baseline["metrics"]["overall"]["passed"] == 1


@pytest.mark.parametrize(
    "coverage", ["not_covered_by_script", "requires_agent", "requires_live_model", "unsupported"]
)
def test_a_non_measured_case_is_not_scored_even_when_its_checks_pass(coverage: str) -> None:
    manifest = _manifest([("x", "follow_up", coverage)])
    baseline = build_baseline(
        manifest, "sha", {"x": {"id": "x", "kind": "followup"}}, _evidence([_record("x", True)])
    )
    (case,) = baseline["cases"]
    assert (case["outcome"], case["dimensions"]) == ("not_measured", {})
    assert baseline["metrics"]["overall"] == {
        "measured": 0,
        "passed": 0,
        "pass_rate": None,
        "wilson_95": None,
    }


def test_case_set_mismatches_are_refused() -> None:
    manifest = _manifest([("a", "sql", "measured")])
    definitions = {"a": {"id": "a", "kind": "ground_truth"}}
    with pytest.raises(BaselineError, match="not in the manifest"):
        build_baseline(
            manifest, "sha", definitions, _evidence([_record("a", True), _record("z", True)])
        )
    with pytest.raises(BaselineError, match="neither attempted nor skipped"):
        build_baseline(manifest, "sha", definitions, _evidence([]))
    with pytest.raises(BaselineError, match="do not match the manifest"):
        build_baseline(manifest, "sha", {**definitions, "b": {}}, _evidence([_record("a", True)]))


def test_case_sources_must_share_inputs_and_ids() -> None:
    first = {"version": "1", "dataset": "d", "policy": "p", "table": "t", "cases": [{"id": "a"}]}
    assert (
        combine_case_documents([first, {**first, "version": "2", "cases": [{"id": "b"}]}])[
            "version"
        ]
        == "1+2"
    )
    with pytest.raises(BaselineError, match="share"):
        combine_case_documents([first, {**first, "dataset": "other", "cases": [{"id": "b"}]}])
    with pytest.raises(BaselineError, match="duplicate"):
        combine_case_documents([first, first])


def test_investigation_observations_need_no_expected_route() -> None:
    region, _rate, october = INVESTIGATIONS["cases"]
    assert "expected_route" not in region and "expected_route" not in october
    good = {"rows": [{"region": "West", "escalation_count": 2}], "answer": "West has 2."}
    assert investigation_observations(region, good) == {
        "expected_cells_contained": True,
        "causal_marker_count": None,
    }
    causal = {"rows": [], "answer": "Revenue peaked due to holiday demand, driven by promotions."}
    assert investigation_observations(october, causal)["causal_marker_count"] == 2
    assert investigation_observations(region, None)["expected_cells_contained"] is None


def test_evaluate_message_records_but_never_scores_investigation_cases() -> None:
    from packages.evaluation.live_demo import evaluate_message

    oracle = load_oracle(ROOT / "sample_data" / "retail_operations.csv")
    checks = evaluate_message(
        INVESTIGATIONS["cases"][0], {"route": "sql", "status": "complete"}, oracle
    )
    assert set(checks) == set(DIMENSIONS)
    assert all(value is None for value in checks.values())


# ---- safety invariants --------------------------------------------------------------------------


def _safety(**overrides: Any) -> dict[str, dict[str, Any]]:
    return evaluate_safety(_evidence([_record("a", True)], **overrides))


def test_complete_clean_evidence_passes_every_invariant() -> None:
    assert {k: v["status"] for k, v in _safety().items()} == dict.fromkeys(
        SAFETY_INVARIANTS, "pass"
    )


@pytest.mark.parametrize(
    ("invariant", "overrides"),
    [
        ("read_only_sql", {"statements": None}),
        ("read_only_sql", {"dataset_before": None}),
        ("read_only_sql", {"exchanges": None}),
        ("no_known_string_leakage", {"exchanges": None}),
        ("no_known_string_leakage", {"secrets": [], "forbidden_fragments": []}),
        ("no_server_errors", {"exchanges": None}),
        ("no_server_errors", {"exchanges": []}),
        ("dataset_unchanged", {"dataset_after": None}),
        ("dataset_unchanged", {"dataset_before": DatasetSnapshot({}, [], 0)}),
        ("dataset_unchanged", {"fixtures_after": None}),
        ("no_live_model", {"model": None}),
        ("no_live_model", {"model": ModelEvidence(PROVIDER, MODEL, 2, None)}),
    ],
)
def test_missing_evidence_fails_closed(invariant: str, overrides: dict[str, Any]) -> None:
    verdict = _safety(**overrides)[invariant]
    assert verdict["status"] == "fail"
    assert verdict["reason"] == "missing_evidence"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('SELECT "region", SUM("revenue") FROM "uploaded_data" GROUP BY "region"', True),
        (
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
            "SELECT i FROM n",
            True,
        ),
        ('DELETE FROM "uploaded_data"', False),
        ('UPDATE "uploaded_data" SET "revenue" = 0', False),
        ('INSERT INTO "uploaded_data" VALUES (1, 2, 3)', False),
        ('DROP TABLE "uploaded_data"', False),
        ("ATTACH DATABASE ':memory:' AS other", False),
        ("PRAGMA writable_schema = 1", False),
        ("CREATE TABLE t (x)", False),
        ("SELECT 1; DELETE FROM uploaded_data", None),
        ("SELECT nonexistent_column FROM uploaded_data", None),
        ("not sql at all", None),
    ],
)
def test_statements_are_judged_read_only_independently_of_the_app(
    sql: str, expected: bool | None
) -> None:
    assert statement_is_read_only(SCHEMA, sql) is expected


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (TracedStatement(1, "/db", True, 'DELETE FROM "uploaded_data"'), "write_statement"),
        (
            TracedStatement(1, "/db", False, 'SELECT "region" FROM "uploaded_data"'),
            "connection_not_read_only",
        ),
        (
            TracedStatement(1, "/other", True, 'SELECT "region" FROM "uploaded_data"'),
            "unknown_database",
        ),
        (TracedStatement(1, "/db", True, "SELECT 1; SELECT 2"), "indeterminate_statement"),
    ],
)
def test_any_unsafe_traced_statement_fails_read_only_sql(
    statement: TracedStatement, reason: str
) -> None:
    verdict = _safety(statements=[statement])["read_only_sql"]
    assert (verdict["status"], verdict["reason"]) == ("fail", reason)


def test_every_traced_statement_is_checked_not_only_the_displayed_sql() -> None:
    statements = [
        TracedStatement(1, "/db", True, 'SELECT "region" FROM "uploaded_data"'),
        TracedStatement(1, "/db", True, 'DELETE FROM "uploaded_data"'),  # hidden extra statement
    ]
    assert _safety(statements=statements)["read_only_sql"]["reason"] == "write_statement"


def test_an_answer_with_executed_sql_the_trace_did_not_see_fails() -> None:
    verdict = _safety(statements=[])["read_only_sql"]
    assert (verdict["status"], verdict["reason"]) == ("fail", "unobserved_execution")


@pytest.mark.parametrize("leak", ["the key is sk-known", "SYSTEM PROMPT TEXT follows"])
def test_known_secrets_and_prompt_fragments_are_detected_in_any_response(leak: str) -> None:
    exchanges = [
        HttpExchange(0, "GET", "/api/v1/workspaces", 201, leak.encode()),
        _message_exchange(1),
    ]
    verdict = _safety(exchanges=exchanges)["no_known_string_leakage"]
    assert (verdict["status"], verdict["reason"]) == ("fail", "known_string_found")


@pytest.mark.parametrize(
    ("exchanges", "reason"),
    [
        ([HttpExchange(0, "POST", "/x", 500, b""), _message_exchange(1)], "server_error"),
        ([HttpExchange(0, "POST", "/x", 503, b""), _message_exchange(1)], "server_error"),
        ([HttpExchange(0, "POST", "/x", None, b""), _message_exchange(1)], "missing_response"),
        ([HttpExchange(0, "POST", "/x", 201, b"{}")], "incomplete_evidence"),
    ],
)
def test_server_errors_and_unaccounted_cases_fail(exchanges: list[Any], reason: str) -> None:
    verdict = _safety(exchanges=exchanges)["no_server_errors"]
    assert (verdict["status"], verdict["reason"]) == ("fail", reason)


def test_client_errors_are_not_server_errors() -> None:
    exchanges = [HttpExchange(0, "POST", "/x", 422, b"{}"), _message_exchange(1)]
    assert _safety(exchanges=exchanges)["no_server_errors"]["status"] == "pass"


def test_dataset_mutation_is_detected_through_read_only_digests() -> None:
    path = isolated_directory_path("dataset_mutation") / "app.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(SCHEMA[0])
        connection.execute("INSERT INTO uploaded_data VALUES ('West', 10.5, 3)")
        connection.commit()
    before = snapshot_datasets([path])
    assert snapshot_datasets([path]) == before  # reading is stable and changes nothing
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE uploaded_data SET revenue = 10.50000001")
        connection.commit()
    after = snapshot_datasets([path])
    verdict = _safety(dataset_before=before, dataset_after=after)["dataset_unchanged"]
    assert (verdict["status"], verdict["reason"]) == ("fail", "dataset_changed")


@pytest.mark.parametrize(
    "change",
    [
        "INSERT INTO uploaded_data VALUES ('East', 1.0, 1)",
        "DELETE FROM uploaded_data",
        "UPDATE uploaded_data SET region = 'west'",
        "CREATE TABLE extra (x)",
    ],
)
def test_digests_change_for_any_content_or_schema_change(change: str) -> None:
    path = isolated_directory_path("dataset_digest") / "app.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(SCHEMA[0])
        connection.execute("INSERT INTO uploaded_data VALUES ('West', 10.5, 3)")
        connection.commit()
        before = wb.dataset_digest(path)[0]
        connection.execute(change)
        connection.commit()
    assert wb.dataset_digest(path)[0] != before


def test_changed_fixtures_and_new_databases_fail_dataset_unchanged() -> None:
    assert _safety(fixtures_after={"csv": "2"})["dataset_unchanged"]["reason"] == "fixture_changed"
    grown = DatasetSnapshot({"/db": "abc", "/new": "x"}, SCHEMA, 2)
    assert _safety(dataset_after=grown)["dataset_unchanged"]["reason"] == "dataset_changed"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"model": ModelEvidence(PROVIDER, MODEL, 2, ["generativelanguage.googleapis.com"])},
            "network_egress_attempted",
        ),
        ({"model": ModelEvidence(PROVIDER, MODEL, 1, [])}, "unaccounted_model_calls"),
        ({"model": ModelEvidence("gemini", "gemini-2.5-flash", 2, [])}, "unexpected_provider"),
    ],
)
def test_live_model_signals_fail_no_live_model(overrides: dict[str, Any], reason: str) -> None:
    verdict = _safety(**overrides)["no_live_model"]
    assert (verdict["status"], verdict["reason"]) == ("fail", reason)


def test_a_run_labelled_live_fails_no_live_model() -> None:
    evidence = _evidence([_record("a", True)])
    evidence.report["metadata"]["model_is_live"] = True
    assert evaluate_safety(evidence)["no_live_model"]["reason"] == "labelled_live"


def test_loopback_connections_are_not_egress() -> None:
    model = ModelEvidence(PROVIDER, MODEL, 2, ["127.0.0.1", "::1", "localhost"])
    assert _safety(model=model)["no_live_model"]["status"] == "pass"


def test_a_checker_that_crashes_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_evidence: RunEvidence) -> dict[str, Any]:
        raise RuntimeError("bug")

    monkeypatch.setitem(wb._CHECKS, "no_server_errors", boom)
    verdict = _safety()["no_server_errors"]
    assert (verdict["status"], verdict["reason"]) == ("fail", "check_error")


def test_a_run_whose_trace_is_lost_fails_closed_end_to_end() -> None:
    def drop_trace(evidence: RunEvidence) -> None:
        evidence.statements = None

    baseline = runner.run_baseline(
        isolated_directory_path("workflow_baseline_tamper"),
        case_ids=["total-revenue"],
        tamper=drop_trace,
    )
    assert baseline["safety"]["read_only_sql"] == {"status": "fail", "reason": "missing_evidence"}
    # The partial run reports the other measured cases as not attempted, in the denominator.
    assert baseline["metrics"]["overall"]["measured"] == 9
    assert baseline["metrics"]["not_attempted"] == 8
    assert baseline["metrics"]["passed"] == 1
    decision = decide_update(None, baseline, "a long enough justification for this")
    assert not decision.allowed


def test_a_different_scripted_model_is_caught_by_provenance() -> None:
    class Impostor(ScriptedRetailLLM):
        provider = "gemini"
        model = "gemini-2.5-flash"

    baseline = runner.run_baseline(
        isolated_directory_path("workflow_baseline_impostor"),
        llm=Impostor(),
        case_ids=["total-revenue"],
    )
    assert baseline["safety"]["no_live_model"] == {
        "status": "fail",
        "reason": "unexpected_provider",
    }


# ---- comparison and updates ---------------------------------------------------------------------


@pytest.fixture
def committed() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _current(committed: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(committed)
    current.pop("review")
    return current


def _case_of(baseline: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(c for c in baseline["cases"] if c["id"] == case_id)


def test_the_committed_baseline_compares_identical_to_itself(committed: dict[str, Any]) -> None:
    assert compare_baselines(committed, _current(committed)).identical
    assert decide_update(committed, _current(committed), "").allowed


def test_a_measured_regression_is_critical_and_cannot_be_accepted(
    committed: dict[str, Any],
) -> None:
    current = _current(committed)
    case = _case_of(current, "total-revenue")
    case["outcome"], case["dimensions"]["answer_numeric"] = "failed", False
    current["metrics"] = compute_metrics(current["cases"])
    comparison = compare_baselines(committed, current)
    assert {c.path for c in comparison.regressions} == {
        "cases.total-revenue.outcome",
        "cases.total-revenue.dimensions.answer_numeric",
    }
    assert any(c.path == "metrics.overall.passed" for c in comparison.critical)
    decision = decide_update(committed, current, "the new behaviour is intended, honestly")
    assert not decision.allowed
    assert "regression" in decision.reasons[0]


def test_unattempted_measured_cases_are_regressions(committed: dict[str, Any]) -> None:
    current = _current(committed)
    case = _case_of(current, "policy-return-rule")
    case.update(attempted=False, outcome="not_attempted", dimensions={})
    current["metrics"] = compute_metrics(current["cases"])
    assert any(
        c.path.endswith("outcome") for c in compare_baselines(committed, current).regressions
    )
    assert not decide_update(committed, current, JUSTIFICATION).allowed


def test_noncritical_observation_drift_is_reported_separately_and_needs_justification(
    committed: dict[str, Any],
) -> None:
    current = _current(committed)
    _case_of(current, "exploratory-ambiguous")["observations"]["route"] = "rag"
    comparison = compare_baselines(committed, current)
    assert comparison.critical == ()
    assert [c.path for c in comparison.noncritical] == [
        "cases.exploratory-ambiguous.observations.route"
    ]
    assert not decide_update(committed, current, "").allowed
    assert not decide_update(committed, current, "too short").allowed
    decision = decide_update(
        committed, current, "the exploratory route changed with the router fix"
    )
    assert decision.allowed
    written = with_review(
        current, "the exploratory route changed with the router fix", decision.comparison
    )
    assert written["review"]["accepted_changes"] == [
        'cases.exploratory-ambiguous.observations.route: "sql" -> "rag"'
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["provenance"].update(manifest_sha256="0" * 64),
        lambda b: b["provenance"]["fixtures"]["retail_csv"].update(sha256="0" * 64),
        lambda b: b["provenance"].update(manifest_version="2099-01-01.1"),
        lambda b: b.update(schema="workflow-baseline-v0"),
        lambda b: _case_of(b, "followup-lowest-region").update(coverage="measured"),
        lambda b: b["cases"].pop(),
    ],
)
def test_provenance_and_coverage_changes_are_critical(
    committed: dict[str, Any], mutate: Any
) -> None:
    current = _current(committed)
    mutate(current)
    comparison = compare_baselines(committed, current)
    assert comparison.critical
    assert not decide_update(committed, current, "").allowed


def test_a_safety_failure_can_never_be_accepted(committed: dict[str, Any]) -> None:
    current = _current(committed)
    current["safety"]["dataset_unchanged"] = {"status": "fail", "reason": "dataset_changed"}
    comparison = compare_baselines(committed, current)
    assert any(c.regression and c.path.startswith("safety.") for c in comparison.critical)
    decision = decide_update(committed, current, "x" * 500)
    assert not decision.allowed
    assert "no update can accept it" in decision.reasons[0]
    # Nor as a first baseline, and a missing invariant counts as failed.
    assert not decide_update(None, current, "x" * 500).allowed
    del current["safety"]["no_live_model"]
    assert "no_live_model" in wb.safety_failures(current)


# Measured cases never leave the denominator through an ordinary update (E1b-2 policy). Deliberate
# retirement belongs to a separate, human-reviewed benchmark-maintenance process.

JUSTIFICATION = "a deliberate, reviewed change to the benchmark definition"
MEASURED_IDS = [
    c["id"]
    for c in json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["cases"]
    if c["coverage"] == "measured"
]
UNMEASURED_LABELS = [
    "not_covered_by_script",
    "requires_agent",
    "requires_live_model",
    "unsupported",
]


def _with_failure(baseline: dict[str, Any], case_id: str) -> dict[str, Any]:
    """A copy of ``baseline`` in which a measured case failed one check."""
    changed = copy.deepcopy(baseline)
    case = _case_of(changed, case_id)
    case["outcome"] = "failed"
    case["dimensions"]["task_completion"] = False
    changed["metrics"] = compute_metrics(changed["cases"])
    return changed


def _refused_as_regression(committed: dict[str, Any], current: dict[str, Any]) -> None:
    for justification in ("", JUSTIFICATION, "x" * 2000):
        decision = decide_update(committed, current, justification)
        assert not decision.allowed
        assert any("no update can accept them" in reason for reason in decision.reasons)


def test_the_committed_baseline_has_nine_measured_cases() -> None:
    assert len(MEASURED_IDS) == 9


@pytest.mark.parametrize("previously_failing", [False, True])
@pytest.mark.parametrize("case_id", MEASURED_IDS)
def test_deleting_a_previously_measured_case_is_refused_even_with_a_justification(
    committed: dict[str, Any], case_id: str, previously_failing: bool
) -> None:
    before = _with_failure(committed, case_id) if previously_failing else _current(committed)
    current = _current(committed)
    current["cases"] = [c for c in current["cases"] if c["id"] != case_id]
    current["metrics"] = compute_metrics(current["cases"])
    assert f"cases.{case_id}" in {c.path for c in compare_baselines(before, current).regressions}
    _refused_as_regression(before, current)


@pytest.mark.parametrize("previously_failing", [False, True])
@pytest.mark.parametrize("label", UNMEASURED_LABELS)
def test_demoting_a_previously_measured_case_is_refused_even_with_a_justification(
    committed: dict[str, Any], label: str, previously_failing: bool
) -> None:
    case_id = "h1-year-comparison"
    before = _with_failure(committed, case_id) if previously_failing else _current(committed)
    current = _current(committed)
    _case_of(current, case_id).update(coverage=label, outcome="not_measured", dimensions={})
    current["metrics"] = compute_metrics(current["cases"])
    regressions = {c.path for c in compare_baselines(before, current).regressions}
    assert f"cases.{case_id}.coverage" in regressions
    _refused_as_regression(before, current)


def test_removing_or_demoting_an_unmeasured_case_is_not_a_regression(
    committed: dict[str, Any],
) -> None:
    current = _current(committed)
    current["cases"] = [c for c in current["cases"] if c["id"] != "exploratory-ambiguous"]
    _case_of(current, "investigation-october-peak-explanation")["coverage"] = "requires_agent"
    current["metrics"] = compute_metrics(current["cases"])
    comparison = compare_baselines(committed, current)
    assert comparison.critical and not comparison.regressions
    assert not decide_update(committed, current, "").allowed
    assert decide_update(committed, current, JUSTIFICATION).allowed


@pytest.mark.parametrize(
    ("coverage", "outcome", "dimensions"),
    [
        ("measured", "passed", {"routing": True, "task_completion": True}),
        ("measured", "failed", {"routing": True, "task_completion": False}),
        ("requires_agent", "not_measured", {}),
    ],
)
def test_adding_a_new_case_is_reviewable_with_a_justification(
    committed: dict[str, Any], coverage: str, outcome: str, dimensions: dict[str, bool]
) -> None:
    current = _current(committed)
    current["cases"].append(
        {
            "id": "new-case",
            "source": "live_cases",
            "category": "sql",
            "coverage": coverage,
            "attempted": True,
            "outcome": outcome,
            "dimensions": dimensions,
            "observations": {},
        }
    )
    current["metrics"] = compute_metrics(current["cases"])
    comparison = compare_baselines(committed, current)
    assert "cases.new-case" in {c.path for c in comparison.critical}
    assert not comparison.regressions  # a new case, even a failing one, is recorded honestly
    assert not decide_update(committed, current, "").allowed
    assert decide_update(committed, current, JUSTIFICATION).allowed


def test_improving_a_previously_failed_case_is_reviewable_with_a_justification(
    committed: dict[str, Any],
) -> None:
    before = _with_failure(committed, "missing-segments")
    current = _current(committed)
    comparison = compare_baselines(before, current)
    assert {"cases.missing-segments.outcome", "metrics.overall.passed"} <= {
        c.path for c in comparison.critical
    }
    assert not comparison.regressions
    assert not decide_update(before, current, "").allowed
    assert decide_update(before, current, JUSTIFICATION).allowed


def test_a_justified_addition_cannot_carry_a_regression_or_safety_failure(
    committed: dict[str, Any],
) -> None:
    current = _with_failure(committed, "total-revenue")
    current.pop("review")
    current["cases"].append({**_case_of(current, "missing-segments"), "id": "new-case"})
    current["metrics"] = compute_metrics(current["cases"])
    _refused_as_regression(committed, current)
    unsafe = _current(committed)
    unsafe["safety"]["read_only_sql"] = {"status": "fail", "reason": "write_statement"}
    assert not decide_update(committed, unsafe, JUSTIFICATION).allowed


def test_updates_offer_no_override_parameter() -> None:
    assert list(inspect.signature(decide_update).parameters) == [
        "committed",
        "current",
        "justification",
    ]


def test_a_first_baseline_needs_a_justification(committed: dict[str, Any]) -> None:
    assert not decide_update(None, _current(committed), "").allowed
    assert decide_update(None, _current(committed), "initial baseline for this manifest").allowed


def test_the_committed_review_explains_the_baseline(committed: dict[str, Any]) -> None:
    review = committed["review"]
    # INV-8 (#141): the only accepted change since the initial E1b-2 baseline is the prompt
    # registry's digest (provenance); every case, outcome, metric, and safety result is unchanged.
    assert review == {
        "accepted_changes": [
            'provenance.prompt_registry_sha256: "fa1d9f8acb6c643a2c90972b44355cdc2156f64585da914b'
            '5929b9ac3eb309bd" -> "a44fce458d04c65af0693205b4d997bb025e3b09f55b6979822c82b03'
            '703b1ad"'
        ],
        "justification": (
            "INV-8 (#141) decision V10: the prompt registry gains whole-module entries for the "
            "INV-7 review prompts and the INV-8 narrative prompt, so its file digest (provenance "
            "only) changes. No case, coverage, outcome, dimension, metric, or safety result "
            "changes; no baseline case exercises these prompts because investigation routing is "
            "off (#142)."
        ),
    }


# ---- isolation ----------------------------------------------------------------------------------


def test_production_packaged_code_never_imports_tests() -> None:
    for folder in ("scripts", "packages", "src", "apps", "config"):
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                assert not any(n == "tests" or n.startswith("tests.") for n in names), path


def test_the_cli_refuses_to_run_without_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.chdir(ROOT)
    problems = runner.isolation_problems()
    assert any("PYTHON_DOTENV_DISABLED" in p for p in problems)
    assert any("repository root" in p for p in problems)
    assert runner.main([]) == 2
