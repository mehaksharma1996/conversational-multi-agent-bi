"""Run the investigation trajectory suite offline and compare it with the committed baseline.

DEVELOPMENT AND TEST ONLY. It runs the real investigation graph with scripted adapters
(``tests/e2e/investigation_trajectory_scenarios.py``); no model is contacted and no credential is
read. Its results are **scripted regression evidence, not real-model performance**.

    python -m tests.e2e.investigation_trajectory_runner                # compare; exit 1 on drift
    python -m tests.e2e.investigation_trajectory_runner --case <id>    # one case; no comparison
    python -m tests.e2e.investigation_trajectory_runner --update --justification "why this is right"

It writes the machine-readable results and the human-readable summary to ``evals/results/``
(git-ignored), or to ``--output`` and ``--summary``. The pytest suite runs the same comparison
(``tests/test_investigation_trajectory_suite.py``), so a worse trajectory fails the ordinary gate.

The module entry point re-execs from a temporary working directory with dotenv loading disabled.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.offline_execution import ensure_offline_module, invocation_path

if __name__ == "__main__":
    ensure_offline_module("tests.e2e.investigation_trajectory_runner")

ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = ROOT / "evals" / "investigation" / "v1"
CASES_PATH = SUITE_DIR / "trajectory_cases.json"
BASELINE_PATH = SUITE_DIR / "trajectory-baseline.json"
ORACLE_PATH = SUITE_DIR / "oracle.json"
PUBLIC_CASES_PATH = SUITE_DIR / "public_cases.json"
RETAIL_CASES_PATH = ROOT / "evals" / "baseline" / "v2" / "investigation_cases.json"
CAUSAL_MARKER_CASE = "investigation-october-peak-explanation"
RESULTS_DIR = ROOT / "evals" / "results"
RESULTS_JSON = "investigation-trajectory.json"
RESULTS_SUMMARY = "investigation-trajectory.md"
# Pinned into provenance: changing any of these is a reviewed baseline change.
_DIGESTED = {
    "cases_sha256": CASES_PATH,
    "public_cases_sha256": PUBLIC_CASES_PATH,
    "oracle_sha256": ORACLE_PATH,
    "retail_cases_sha256": RETAIL_CASES_PATH,
}


def load_cases() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return document


def build_resources(directory: Path) -> Any:
    from tests.e2e.investigation_trajectory_scenarios import Resources

    public = json.loads(PUBLIC_CASES_PATH.read_text(encoding="utf-8"))
    retail = json.loads(RETAIL_CASES_PATH.read_text(encoding="utf-8"))
    retail_cases = {case["id"]: case for case in retail["cases"]}
    return Resources(
        directory=directory,
        oracle=json.loads(ORACLE_PATH.read_text(encoding="utf-8")),
        retail_cases=retail_cases,
        causal_markers=tuple(retail_cases[CAUSAL_MARKER_CASE]["causal_markers"]),
        churn_questions={case["id"]: case["question"] for case in public["cases"]},
    )


def _question(case: Mapping[str, Any], resources: Any) -> str | None:
    source = case.get("source_case")
    if source in resources.churn_questions:
        return str(resources.churn_questions[source])
    if source in resources.retail_cases:
        return str(resources.retail_cases[source]["question"])
    return None


def _routing_block(case: Mapping[str, Any], resources: Any) -> dict[str, Any]:
    """The real deterministic gate on the case's own question, or on its table of questions."""
    from src.orchestration.investigation_routing import evaluate_investigation_eligibility as gate

    question = _question(case, resources)
    block: dict[str, Any] = {}
    if question is not None:
        decision = gate(question, True, True)
        block = {
            "eligible": decision.eligible,
            "primary_reason": decision.primary_reason,
            "reason_codes": list(decision.reason_codes),
        }
    table = case.get("routing_table")
    if table:
        results = [
            gate(row["question"], row.get("feature", True), row.get("table", True)) for row in table
        ]
        block["table"] = [
            {"eligible": item.eligible, "primary_reason": item.primary_reason} for item in results
        ]
    return block


def _scan(texts: Iterable[str], needles: Sequence[str]) -> bool:
    """Whether none of the needles appears, whole-text and case-insensitively."""
    folded = [text.casefold() for text in texts]
    return not any(needle.casefold() in text for needle in needles for text in folded)


def observe(case: Mapping[str, Any], run: Any, resources: Any) -> dict[str, Any]:
    """The content-free observation blocks for one scenario run."""
    from packages.evaluation.investigation_trajectory import (
        audit_counts,
        budget_block,
        causal_markers_found,
        citation_block,
        critic_block,
        delegation_block,
        grounding_block,
        read_only_verdict,
        recovery_block,
        report_block,
        sql_equivalence,
    )
    from packages.evaluation.workflow_baseline import dataset_digest
    from packages.investigation.report import report_digest, report_inputs
    from packages.investigation.report_verifier import verify_report
    from tests.e2e.investigation_trajectory_scenarios import model_call_kinds

    e, result = run.e, run.result
    view = result.state if result is not None else None
    payloads = result.payloads if result is not None else {}
    report = result.report if result is not None else None
    reverified: bool | None = None
    if report is not None and view is not None:
        reverified = verify_report(report, report_inputs(view, payloads)) == () and (
            view.report_record.report_sha256 == report_digest(report)
        )
    calls = model_call_kinds(list(e.llm.prompts)) if e is not None else {}
    planning_calls = len(run.planner.calls) if run.planner is not None else 0
    statements = [*(e.executor.calls if e is not None else ()), *run.other_statements]
    schema_sql: list[str] = []
    digest_after = None
    if run.table is not None:
        digest_after, schema_sql = dataset_digest(Path(run.table.database_path))
    refusals = sorted(
        {
            decision.refusal_code.value
            for decision in (view.decisions if view is not None else ())
            if decision.refusal_code is not None
        }
    )
    visible = [json.dumps(report.model_dump(mode="json"))] if report is not None else []
    sink = e.approvals.sink.events if e is not None and e.approvals is not None else ()
    security = {
        "statements_executed": len(statements),
        "statements_read_only": read_only_verdict(schema_sql, statements)
        if (statements or schema_sql)
        else True,
        "refusals": refusals,
        "audit_events": audit_counts(sink),
        "forbidden_text_absent": _scan(visible, run.forbidden_text),
        **run.extra_security,
    }
    status = result.status if result is not None else None
    usage = view.budget_usage if view is not None else None
    return {
        "routing_gate": _routing_block(case, resources),
        "planning_delegation": delegation_block(view, run.proposal_outcome),
        "sql_result_equivalence": sql_equivalence(payloads.values(), run.references),
        "citation_quality": citation_block(payloads.values(), report),
        "hypothesis_grounding": grounding_block(view, payloads),
        "critic_recall": critic_block(view, run.seeded),
        "failure_recovery": recovery_block(
            status, result.category if result is not None else None, view
        ),
        "budget_compliance": budget_block(usage, run.limits),
        "security_approval": security,
        "report_verification": {
            **report_block(view, report, reverified),
            "causal_markers_found": causal_markers_found(report, run.causal_markers),
        },
        "unnecessary_calls": {
            "planning_calls": planning_calls,
            "calls": calls,
            "model_calls": sum(calls.values()),
        },
        "_harness": {
            "digest_after": digest_after,
            "statements": len(statements),
            "read_only": security["statements_read_only"],
            "adapters_scripted": _adapters_scripted(run),
        },
    }


def _adapters_scripted(run: Any) -> bool:
    """Every model the run could reach is one of the scripted adapters."""
    from tests.investigation_execution_fakes import RoutedAdapter
    from tests.investigation_planning_fakes import ScriptedDeadlineAdapter

    planner = run.planner is None or isinstance(run.planner, ScriptedDeadlineAdapter)
    llm = run.e is None or isinstance(run.e.llm, RoutedAdapter)
    return planner and llm


def _digests() -> dict[str, str]:
    from packages.evaluation.workflow_cases import fixture_sha256

    return {name: fixture_sha256(path, "sha256-lf") for name, path in _DIGESTED.items()}


def run_suite(
    directory: Path,
    *,
    case_ids: Sequence[str] | None = None,
    scenarios: Mapping[str, Callable[[Any], Any]] | None = None,
    after_observe: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """One scripted run of the suite, returning the results document.

    ``scenarios`` and ``after_observe`` exist for tests: a worse scripted model, or a change to what
    was observed, must make the grade fail.
    """
    from packages.evaluation.investigation_trajectory import (
        build_document,
        case_problems,
        evaluate_invariants,
        grade_case,
    )
    from packages.evaluation.workflow_baseline import BaselineError, dataset_digest
    from tests.e2e.investigation_trajectory_scenarios import SCENARIOS
    from tests.e2e.workflow_baseline_runner import _record_network
    from tests.investigation_execution_fakes import reset_replay_registry

    table = scenarios if scenarios is not None else SCENARIOS
    definitions = load_cases()["cases"]
    problems = case_problems(definitions, table)
    if problems:
        raise BaselineError("invalid trajectory cases: " + "; ".join(problems))
    selected = [c for c in definitions if case_ids is None or c["id"] in set(case_ids)]
    resources = build_resources(directory)
    before = {
        "churn": dataset_digest(Path(resources.churn_table().database_path))[0],
        "retail": dataset_digest(Path(resources.retail_table().database_path))[0],
    }
    graded: list[dict[str, Any]] = []
    harness: list[dict[str, Any]] = []
    with _record_network() as network:
        for case in selected:
            reset_replay_registry()
            run = table[case["scenario"]](resources)
            observed = observe(case, run, resources)
            facts = observed.pop("_harness")
            if after_observe is not None:
                after_observe(case["id"], observed)
            harness.append(
                {
                    "statements": facts["statements"],
                    "read_only": facts["read_only"],
                    "adapters_scripted": facts["adapters_scripted"],
                    "dataset_unchanged": facts["digest_after"] is None
                    or facts["digest_after"] in before.values(),
                }
            )
            status = observed["failure_recovery"]["status"]
            usage = observed["unnecessary_calls"]
            graded.append(
                {
                    "id": case["id"],
                    "group": case["group"],
                    "source_case": case.get("source_case"),
                    "grade": grade_case(case, observed),
                    "trajectory": {
                        "status": status,
                        "model_calls": usage["model_calls"] + usage["planning_calls"],
                        "tool_steps": len(
                            observed["planning_delegation"].get("tool_sequence") or ()
                        ),
                    },
                    "observed": observed,
                }
            )
        attempts = list(network)
    invariants = evaluate_invariants(harness, [host for host in attempts if not _is_local(host)])
    return build_document(_digests(), graded, invariants)


def _is_local(host: str) -> bool:
    from packages.evaluation.workflow_baseline import _is_local as local

    return local(host)


def graph_grades(directory: Path, case_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Grades for the workflow baseline's ``graded_by: investigation_graph`` cases.

    The suite's cases name their source case; a source case with no trajectory case has no grade,
    so the workflow baseline reports it ``not_attempted``.
    """
    from packages.evaluation.workflow_baseline import safety_failures

    wanted = set(case_ids)
    definitions = load_cases()["cases"]
    ids = [c["id"] for c in definitions if c.get("source_case") in wanted]
    if not ids:
        return {}
    document = run_suite(directory, case_ids=ids)
    source_of = {c["id"]: c["source_case"] for c in definitions if c["id"] in ids}
    clean = not safety_failures(document)
    return {
        source_of[case["id"]]: {
            "passed": clean and case["outcome"] == "passed",
            "dimensions": case["dimensions"],
        }
        for case in document["cases"]
    }


def _print_summary(document: Mapping[str, Any]) -> None:
    metrics = document["metrics"]
    print(f"[{document['evidence']['kind']}] {document['evidence']['statement']}")
    print(f"  cases {metrics['cases']}: passed {metrics['passed']}, failed {metrics['failed']}")
    for name, verdict in document["safety"].items():
        print(f"  invariant {name:<24} {verdict['status']} ({verdict['reason']})")
    for case in document["cases"]:
        if case["outcome"] != "passed":
            print(f"  FAILED {case['id']}")
            for item in case["mismatches"]:
                print(f"    {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted investigation trajectory suite.")
    parser.add_argument("--update", action="store_true", help="Write the baseline if allowed.")
    parser.add_argument("--justification", default="", help="Required for any accepted change.")
    parser.add_argument("--case", action="append", help="Run only these case ids (no comparison).")
    parser.add_argument(
        "--output",
        type=invocation_path,
        default=RESULTS_DIR / RESULTS_JSON,
        help="Machine-readable results.",
    )
    parser.add_argument(
        "--summary",
        type=invocation_path,
        default=RESULTS_DIR / RESULTS_SUMMARY,
        help="Human-readable summary.",
    )
    args = parser.parse_args(argv)
    if args.update and args.case:
        parser.error("--update needs a full run")

    from packages.evaluation.investigation_trajectory import (
        render_summary,
        without_details,
    )
    from packages.evaluation.workflow_baseline import (
        compare_baselines,
        decide_update,
        render,
        safety_failures,
        with_review,
    )

    with tempfile.TemporaryDirectory(prefix="trajectory-", ignore_cleanup_errors=True) as tmp:
        current = run_suite(Path(tmp), case_ids=args.case)
    _print_summary(current)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(current), encoding="utf-8", newline="\n")
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(render_summary(current), encoding="utf-8", newline="\n")
    print(f"Results: {args.output}\nSummary: {args.summary}")
    failures = safety_failures(current)
    if args.case:
        if failures:
            print(f"INVARIANTS FAILED: {', '.join(failures)}", file=sys.stderr)
        return 0 if not failures and current["metrics"]["failed"] == 0 else 1

    committed = (
        json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else None
    )
    if args.update:
        decision = decide_update(committed, without_details(current), args.justification)
        if decision.comparison is not None:
            _print_comparison(decision.comparison)
        if not decision.allowed:
            for reason in decision.reasons:
                print(f"UPDATE REFUSED: {reason}", file=sys.stderr)
            return 1
        BASELINE_PATH.write_text(
            render(with_review(without_details(current), args.justification, decision.comparison)),
            encoding="utf-8",
            newline="\n",
        )
        print(f"Baseline written to {BASELINE_PATH}; review the diff before committing.")
        return 0
    if committed is None:
        print("No committed baseline. Create one with --update --justification.", file=sys.stderr)
        return 1
    comparison = compare_baselines(committed, current)
    _print_comparison(comparison)
    if failures:
        print(f"INVARIANTS FAILED: {', '.join(failures)}", file=sys.stderr)
    return 0 if comparison.identical and not failures else 1


def _print_comparison(comparison: Any) -> None:
    if comparison.identical:
        print("Matches the committed baseline.")
        return
    for title, changes in (
        ("CRITICAL", comparison.critical),
        ("NONCRITICAL", comparison.noncritical),
    ):
        for change in changes:
            print(f"  {title} {change.describe()}")


if __name__ == "__main__":
    raise SystemExit(main())
