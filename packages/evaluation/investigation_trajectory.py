"""Grading scripted runs of the real investigation graph: expected versus actual, per dimension.

``tests/e2e/investigation_trajectory_runner.py`` (development only) drives the real proposal,
approval service, execution owner, gateway, specialists, Critic, Supervisor, and report verifier
with scripted bounded adapters, and hands this module what the run left behind. Here that becomes:

- an *observation*: content-free blocks, one per dimension. Fixed codes, identifiers, counts, and
  booleans only. No question, SQL, row, passage, prompt, or model text;
- a *grade*: each dimension a case's ``expect`` names is compared with the observed block, and any
  difference is a named mismatch. A case with no applicable dimension fails closed;
- a *baseline document* in the workflow baseline's shape, so its comparison and update rules
  (``compare_baselines``, ``decide_update``) apply unchanged.

What a pass shows: the application path behaves as recorded when the model output is the scripted
output. It is **never** a measurement of model accuracy, and the document says so. Reference values
are passed in by the harness, after the run; this module names no fixture path, so it cannot put an
oracle in front of a model. It imports no investigation or application code.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from packages.evaluation.result_sets import compare_result_sets
from packages.evaluation.workflow_baseline import (
    TRAJECTORY_INVARIANTS,
    TRAJECTORY_SCHEMA,
    compute_metrics,
    statement_is_read_only,
)
from packages.evaluation.workflow_cases import TRAJECTORY_DIMENSIONS, causal_markers_in

TRAJECTORY_EVIDENCE_KIND = "scripted-investigation-trajectory-regression"
TRAJECTORY_EVIDENCE_STATEMENT = (
    "Scripted regression evidence, not real-model performance. Scripted adapters supply every "
    "model output, and the real investigation graph handles them. A pass shows that the "
    "application path still behaves as recorded. It says nothing about how any real model would "
    "plan, write SQL, cite, critique, or report."
)
# Every graph case states its end state and its exact calls, so a longer path cannot pass quietly.
REQUIRED_GRAPH_DIMENSIONS = ("failure_recovery", "unnecessary_calls")
# The report's fixed reason for an untested hypothesis. "Because" here explains why no test ran; it
# attributes no business outcome to anything.
UNTESTED_CLAUSE = "because the data it needs is not available"
# Strings that must never reach a results document (SQL text, fixture rows, scripted model prose).
FORBIDDEN_IN_RESULTS = ("SELECT ", "uploaded_data", "IGNORE ALL PRIOR", "Source 1", "ev_")


# --- observation -----------------------------------------------------------------------------


def sql_equivalence(
    payloads: Iterable[Any], references: Mapping[str, Sequence[Sequence[Any]]]
) -> dict[str, Any]:
    """For each referenced task, whether its query evidence equals the reference rows.

    The last succeeded attempt's rows are compared under ``compare_result_sets``. Only a boolean
    and counts are kept. A task with no query evidence is not equivalent: absence never passes.
    """
    rows_by_task: dict[str, Any] = {}
    for payload in payloads:
        if getattr(payload, "kind", None) == "query":
            rows_by_task[payload.task_id] = payload
    block: dict[str, Any] = {}
    for task_id, expected in sorted(references.items()):
        payload = rows_by_task.get(task_id)
        if payload is None:
            block[task_id] = {"equivalent": False, "reason": "no_evidence", "rows": 0}
            continue
        outcome = compare_result_sets(list(payload.rows), list(expected))
        block[task_id] = {
            "equivalent": outcome.equivalent and not payload.truncated,
            "reason": outcome.reason,
            "rows": outcome.actual_rows,
        }
    return block


def citation_block(payloads: Iterable[Any], report: Any) -> dict[str, Any]:
    """What the document evidence and the report cite. Locations only, never passages."""
    cited: set[tuple[str, int | None]] = set()
    invalid = uncited = documents = 0
    resolves = True
    for payload in payloads:
        if getattr(payload, "kind", None) != "document":
            continue
        documents += 1
        sources = {source.number: source for source in payload.sources}
        invalid += len(payload.invalid_citations)
        uncited += not payload.cited_source_numbers
        for number in payload.cited_source_numbers:
            source = sources.get(number)
            if source is None:
                resolves = False
            else:
                cited.add((source.filename, source.page_number))
    report_cited = {(c.filename, c.page_number) for c in report.citations} if report else set()
    return {
        "document_evidence": documents,
        "cited": sorted(f"{name}#{page}" for name, page in cited),
        "invalid_citations": invalid,
        "uncited_documents": uncited,
        "citations_resolve": resolves,
        "report_citations_are_verified": report_cited <= cited,
        "report_cited": sorted(f"{name}#{page}" for name, page in report_cited),
    }


def grounding_block(view: Any, payloads: Mapping[str, Any]) -> dict[str, Any]:
    """Verdicts, and whether each decisive one cites derived evidence that exists."""
    verdicts = dict(view.verdicts) if view is not None else {}
    evidence = dict(view.evidence) if view is not None else {}
    grounded = True
    for record in verdicts.values():
        if record.verdict == "untestable":
            continue
        kinds = {
            evidence[eid].kind if eid in evidence and eid in payloads else None
            for eid in record.evidence_ids
        }
        grounded = grounded and "derived" in kinds and None not in kinds
    return {
        "verdicts": {key: record.verdict for key, record in sorted(verdicts.items())},
        "reasons": {key: list(record.reasons) for key, record in sorted(verdicts.items())},
        "test_tasks": {key: list(record.test_task_ids) for key, record in sorted(verdicts.items())},
        "decisive_verdicts_cite_derived_evidence": grounded,
    }


def critic_block(view: Any, seeded: Sequence[str] = ()) -> dict[str, Any]:
    """Critique rounds, follow-ups, and recall over the defects the scenario seeded on purpose."""
    critiques = tuple(view.critiques) if view is not None else ()
    flagged = {item.code for record in critiques for item in record.findings}
    wanted = set(seeded)
    return {
        "rounds": [
            {
                "draft_status": record.draft_status,
                "review_status": record.review_status,
                "replan": record.replan,
                "codes": sorted(item.code for item in record.findings),
                "rejected_proposals": record.rejected_proposals,
            }
            for record in critiques
        ],
        "follow_up_rounds": len(view.follow_up_rounds) if view is not None else 0,
        "follow_up_tasks": [t.id for t in view.admitted_follow_ups] if view is not None else [],
        "seeded_defects": len(wanted),
        "seeded_defects_flagged": len(wanted & flagged),
        "seeded_defects_missed": sorted(wanted - flagged),
    }


def recovery_block(status: Any, category: str | None, view: Any) -> dict[str, Any]:
    """The end state and every task's outcome, with the corrections that recovery spent."""
    tasks: dict[str, Any] = {}
    corrections = 0
    if view is not None:
        for task_id, record in sorted(view.tasks.items()):
            outcome = record.outcome
            tasks[task_id] = {
                "status": record.status,
                "attempts": record.attempts,
                "error": outcome.error_category if outcome is not None else None,
            }
        for item in view.evidence.values():
            corrections += getattr(item.provenance, "corrections_used", 0) or 0
    return {
        "status": getattr(status, "value", status),
        "category": category,
        "tasks": tasks,
        "sql_corrections": corrections,
    }


def budget_block(usage: Any, limits: Any) -> dict[str, Any]:
    """Execution ledger counters. Tokens and time are left out: they follow prompt bytes."""
    if usage is None:
        return {"ledger": None}
    counters = (
        "model_calls",
        "sql_executions",
        "retrievals",
        "task_attempts",
        "replan_rounds",
        "critic_rounds",
    )
    within = all(getattr(usage, name) <= getattr(limits, name) for name in counters)
    return {
        **{name: getattr(usage, name) for name in counters},
        "closed_reason": usage.closed_reason,
        "exhausted_resource": usage.exhausted_resource,
        "outstanding_reservations": usage.outstanding_reservations,
        "unmetered_model_calls": usage.unmetered_model_calls,
        "within_limits": within,
    }


def report_block(view: Any, report: Any, reverified: bool | None) -> dict[str, Any]:
    """What the report verifier decided, and an independent re-check of the report returned."""
    record = view.report_record if view is not None else None
    if record is None:
        return {"record": None, "report_returned": report is not None}
    block: dict[str, Any] = {
        "record": record.verification,
        "mode": record.mode,
        "availability": record.availability,
        "fallback_reason": record.fallback_reason,
        "violations": list(record.violations),
        "report_returned": report is not None,
    }
    if report is not None:
        block.update(
            reverified=reverified,
            notes=sorted({note.code for note in report.notes}),
            limitations=sorted({str(item.code.value) for item in report.limitations}),
            facts=len(report.facts),
            recommendations=[
                f"{item.rule_id}:{item.hypothesis_id}:{'unproven' if item.unproven else 'direct'}"
                for item in report.recommendations
            ],
            hypotheses={row.hypothesis_id: row.verdict for row in report.hypotheses},
        )
    return block


def report_text(report: Any) -> str:
    """Every sentence the report shows a reader (for in-process scans; never recorded)."""
    return " ".join(sentence.text for section in report.sections for sentence in section.sentences)


def causal_markers_found(report: Any, markers: Sequence[str]) -> list[str]:
    """Causal phrases in what the report says, apart from its one fixed non-causal clause."""
    if report is None:
        return []
    return causal_markers_in(report_text(report).replace(UNTESTED_CLAUSE, ""), markers)


def tool_sequence(view: Any) -> list[str]:
    """The gateway's decisions in order: ``task:tool:result`` plus any fixed refusal code."""
    if view is None:
        return []
    sequence = []
    for decision in view.decisions:
        code = decision.refusal_code
        suffix = f":{code.value}" if code is not None else ""
        sequence.append(f"{decision.task_id or '-'}:{decision.tool}:{decision.result}{suffix}")
    return sequence


def delegation_block(view: Any, proposal_outcome: str | None) -> dict[str, Any]:
    """Who the plan assigned each task to, who actually ran it, and the decision sequence."""
    if view is None or view.plan is None:
        return {"proposal": proposal_outcome, "plan_version": None, "assignees": {}}
    roles: dict[str, set[str]] = {}
    for decision in view.decisions:
        if decision.task_id is not None and decision.role is not None:
            roles.setdefault(decision.task_id, set()).add(str(decision.role.value))
    return {
        "proposal": proposal_outcome,
        "plan_version": view.plan.schema_version,
        "assignees": {task.id: str(task.assignee.value) for task in view.plan.tasks},
        "ran_as": {task_id: sorted(names) for task_id, names in sorted(roles.items())},
        "tool_sequence": tool_sequence(view),
    }


def read_only_verdict(schema_sql: Sequence[str], statements: Sequence[str]) -> bool:
    """Every executed statement prepares as read-only on an empty schema copy; fails closed."""
    if not schema_sql:
        return False
    return all(statement_is_read_only(schema_sql, sql) is True for sql in statements)


def audit_counts(events: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(event.name for event in events).items()))


# --- grading ---------------------------------------------------------------------------------


def mismatches(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Where ``actual`` differs from ``expected``. Mappings are subsets; all else must be equal.

    A mapping in ``expected`` constrains only the keys it names, so a case states what it is about.
    Lists compare exactly and in order: a longer tool sequence is a difference. Values come only
    from content-free observation blocks, so a mismatch is safe to print and record.
    """
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{path}: expected a mapping, found {json.dumps(actual, default=str)}"]
        found: list[str] = []
        for key, value in expected.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in actual:
                found.append(f"{child}: missing (expected {json.dumps(value, default=str)})")
            else:
                found.extend(mismatches(value, actual[key], child))
        return found
    if expected != actual:
        wanted, seen = json.dumps(expected, default=str), json.dumps(actual, default=str)
        return [f"{path}: expected {wanted}, found {seen}"]
    return []


def grade_case(case: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    """One case's outcome: every dimension it names, compared with what the run left behind."""
    dimensions: dict[str, bool] = {}
    differences: list[str] = []
    for name, expected in case["expect"].items():
        found = mismatches(expected, observed.get(name), name)
        dimensions[name] = not found
        differences.extend(found)
    return {
        "passed": bool(dimensions) and all(dimensions.values()),
        "dimensions": dimensions,
        "mismatches": differences,
    }


def case_problems(cases: Sequence[Mapping[str, Any]], scenarios: Iterable[str]) -> list[str]:
    """Every problem with the case definitions; an empty list means they are valid."""
    known = set(scenarios)
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id"))
        if case_id in seen:
            problems.append(f"{case_id}: listed more than once")
        seen.add(case_id)
        if case.get("scenario") not in known:
            problems.append(f"{case_id}: unknown scenario {case.get('scenario')!r}")
        if not str(case.get("rationale", "")).strip():
            problems.append(f"{case_id}: a rationale is required")
        expect = case.get("expect")
        if not isinstance(expect, Mapping) or not expect:
            problems.append(f"{case_id}: expect must name at least one dimension")
            continue
        problems.extend(
            f"{case_id}: unknown dimension {name!r}"
            for name in expect
            if name not in TRAJECTORY_DIMENSIONS
        )
        if case.get("graph", True):
            problems.extend(
                f"{case_id}: a graph case must state {name}"
                for name in REQUIRED_GRAPH_DIMENSIONS
                if name not in expect
            )
    return problems


# --- the document ----------------------------------------------------------------------------


def _verdict(reason: str | None, **evidence: Any) -> dict[str, Any]:
    return {"status": "fail" if reason else "pass", "reason": reason or "ok", "evidence": evidence}


def evaluate_invariants(
    runs: Sequence[Mapping[str, Any]], network_attempts: Sequence[str] | None
) -> dict[str, dict[str, Any]]:
    """Suite-wide invariants from harness evidence. Missing evidence is a failure."""
    results: dict[str, dict[str, Any]] = {}
    if not runs:
        return {name: _verdict("missing_evidence") for name in TRAJECTORY_INVARIANTS}
    statements = sum(int(run.get("statements", 0)) for run in runs)
    results["read_only_sql"] = _verdict(
        None if all(run.get("read_only") is True for run in runs) else "write_statement",
        statements=statements,
    )
    results["dataset_unchanged"] = _verdict(
        None if all(run.get("dataset_unchanged") is True for run in runs) else "dataset_changed",
        runs=len(runs),
    )
    if network_attempts is None:
        results["no_live_model"] = _verdict("missing_evidence")
    elif any(run.get("adapters_scripted") is not True for run in runs):
        results["no_live_model"] = _verdict("unscripted_adapter")
    elif network_attempts:
        results["no_live_model"] = _verdict("network_egress_attempted")
    else:
        results["no_live_model"] = _verdict(None, network_attempts=0)
    results["no_content_in_results"] = _verdict(None)  # replaced once the document exists
    return results


def build_document(
    manifest_digests: Mapping[str, str],
    graded: Sequence[Mapping[str, Any]],
    invariants: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The deterministic results document, in the workflow baseline's shape."""
    cases = [
        {
            "id": item["id"],
            "group": item["group"],
            "source": item.get("source_case") or "trajectory",
            "category": "investigation",
            "coverage": "measured",
            "attempted": True,
            "outcome": "passed" if item["grade"]["passed"] else "failed",
            "dimensions": item["grade"]["dimensions"],
            "mismatches": item["grade"]["mismatches"],
            "trajectory": item["trajectory"],
            "observations": {"routing_gate": item["observed"]["routing_gate"]},
        }
        for item in graded
    ]
    document: dict[str, Any] = {
        "schema": TRAJECTORY_SCHEMA,
        "evidence": {
            "kind": TRAJECTORY_EVIDENCE_KIND,
            "statement": TRAJECTORY_EVIDENCE_STATEMENT,
            "model_is_live": False,
        },
        "provenance": {"mode": "scripted-trajectory", **dict(sorted(manifest_digests.items()))},
        "metrics": compute_metrics(cases, dimensions=TRAJECTORY_DIMENSIONS),
        "safety": {},
        "safety_evidence": {},
        "cases": cases,
    }
    verdicts = {name: dict(value) for name, value in invariants.items()}
    # The full observed blocks, for the results file. The committed baseline leaves them out.
    document["details"] = {item["id"]: item["observed"] for item in graded}
    leaked = [text for text in FORBIDDEN_IN_RESULTS if text in json.dumps(document)]
    verdicts["no_content_in_results"] = _verdict(
        "content_found" if leaked else None, patterns_checked=len(FORBIDDEN_IN_RESULTS)
    )
    document["safety"] = {
        name: {"status": verdicts[name]["status"], "reason": verdicts[name]["reason"]}
        for name in TRAJECTORY_INVARIANTS
    }
    document["safety_evidence"] = {
        name: verdicts[name]["evidence"] for name in TRAJECTORY_INVARIANTS
    }
    return document


def without_details(document: Mapping[str, Any]) -> dict[str, Any]:
    """The document as it is committed: outcomes and trajectories, not every observed block."""
    return {key: value for key, value in document.items() if key != "details"}


def render_summary(document: Mapping[str, Any]) -> str:
    """The human-readable summary: the label first, then outcomes, dimensions, and failures."""
    metrics = document["metrics"]
    overall = metrics["overall"]
    lines = [
        "# Investigation trajectory results",
        "",
        f"**{document['evidence']['kind']}.** {document['evidence']['statement']}",
        "",
        f"- Cases: {metrics['cases']}; passed {metrics['passed']}, failed {metrics['failed']}.",
        f"- Pass rate: {overall['pass_rate']} (Wilson 95% {overall['wilson_95']}), over scripted "
        "scenarios chosen by the authors. It is not an accuracy estimate.",
        "",
        "## Invariants",
        "",
        *(
            f"- {name}: {verdict['status']} ({verdict['reason']})"
            for name, verdict in document["safety"].items()
        ),
        "",
        "## Dimensions",
        "",
        "| Dimension | Passed | Applicable |",
        "|---|---:|---:|",
        *(
            f"| {name} | {value['passed']} | {value['applicable']} |"
            for name, value in metrics["dimensions"].items()
        ),
        "",
        "## Cases",
        "",
        "| Case | Group | Outcome | Status | Model calls | Tool steps |",
        "|---|---|---|---|---:|---:|",
    ]
    for case in document["cases"]:
        trajectory = case["trajectory"]
        lines.append(
            f"| {case['id']} | {case['group']} | {case['outcome']} | "
            f"{trajectory['status'] or '-'} | {trajectory['model_calls']} | "
            f"{trajectory['tool_steps']} |"
        )
    unrouted = [
        case["id"]
        for case in document["cases"]
        if case["source"] != "trajectory"
        and case["observations"]["routing_gate"].get("eligible") is False
    ]
    if unrouted:
        lines.extend(
            [
                "",
                "## Graded on the graph, not reached through the routing gate",
                "",
                "The deterministic gate does not send these questions to the investigation route "
                "today. Each is graded from an approved plan, so a pass says what the graph does "
                "with that plan, not that the product routes the question to it.",
                "",
                *(f"- {case_id}" for case_id in unrouted),
            ]
        )
    failed = [case for case in document["cases"] if case["outcome"] != "passed"]
    if failed:
        lines.extend(["", "## Expected versus actual differences", ""])
        for case in failed:
            lines.append(f"### {case['id']}")
            lines.extend(f"- {item}" for item in case["mismatches"])
    return "\n".join(lines) + "\n"
