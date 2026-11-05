"""The offline investigation demonstration (HIRE-1, #204). SCRIPTED: no real model is contacted.

    python -m tests.e2e.investigation_demo                # run, print, compare; exit 1 on drift
    python -m tests.e2e.investigation_demo --update       # record a reviewed change to the result

It drives the subscription-churn investigation over HTTP through the production FastAPI application:
routing, planning admission, plan and SQL-wave approval, the SQL and document gateways, the
independent check, the Critic, the report verifier, and durable storage are all the real code. Only
what a model would have said is scripted, by the same test-only adapters the browser journeys mount
(``tests/e2e/investigation_app.py``). So a run shows how the application handles these model
outputs. It says nothing about what any real model would produce.

Results go to ``evals/results/investigation-demo.json`` (git-ignored). The committed expected result
is ``evals/investigation/v1/demo-expected.json``, and ``tests/test_investigation_demo.py`` runs the
same comparison, so neither the documented output nor the behaviour can drift silently.

The module entry point re-execs from a temporary working directory with dotenv loading disabled.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from scripts.offline_execution import ensure_offline_module, invocation_path

if __name__ == "__main__":
    ensure_offline_module("tests.e2e.investigation_demo")

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PATH = ROOT / "evals" / "investigation" / "v1" / "demo-expected.json"
RESULTS_PATH = ROOT / "evals" / "results" / "investigation-demo.json"
QUESTION = "Why did customer churn increase last quarter?"
SCRIPTED_PARTS = (
    "planner: the churn plan (tests/investigation_critique_fakes.py)",
    "SQL analyst model: the SQL text of each task (tests/investigation_hypothesis_fakes.py)",
    "document answers: one cited answer per research task",
    "findings draft and Critic review: one seeded contradiction and one stratification gap",
    "narrative model: an empty answer, so the verifier falls back to the deterministic report",
    "embedder: a term-matching test embedder instead of SentenceTransformers",
    "human approver: this script, calling the same HTTP routes the browser calls",
)
REAL_PARTS = (
    "FastAPI routes, identity, consent, rate limits, and jobs",
    "the routing gate, plan validation, and the approval service",
    "the SQL and document gateways, the read-only SQL guards, and SQLite execution",
    "the independent check, verdicts, Critic checks, and report verifier",
    "ChromaDB retrieval and durable storage, under a temporary directory",
)
STATEMENT = (
    "Scripted demonstration, not real-model performance. Scripted adapters supply every model "
    "output, and the production application handles them. It says nothing about how any real "
    "model would plan, write SQL, cite, critique, or report."
)
VERIFIED_MEANS = (
    "Verified means agreement with the authorized source table under the approved definitions. "
    "It is not proof that the source data is accurate or that the definitions are the right ones."
)


# --- one scripted deployment ---------------------------------------------------------------------


def _providers(sql: Mapping[str, str] | None = None) -> Any:
    """The browser journeys' scripted churn adapters, recording what they were asked for."""
    from tests.e2e.investigation_app import ScriptedChurnReportAdapters
    from tests.investigation_execution_fakes import RoutedAdapter

    class Recording(ScriptedChurnReportAdapters):
        def __init__(self) -> None:
            self.planners: list[Any] = []
            self.models: list[Any] = []

        def planning(self) -> Any:
            provider = super().planning()
            self.planners.append(provider.adapter)
            return provider

        def execution(self) -> Any:
            provider = super().execution()
            model = provider.llm_client
            assert isinstance(model, RoutedAdapter)
            model.sql.update(sql or {})
            self.models.append(model)
            return provider

    return Recording()


@contextmanager
def _deployment(root: Path, adapters: Any) -> Iterator[Any]:
    """A test client for the production application. ``adapters=None`` is production's selection."""
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from config.settings import Settings
    from tests.e2e import investigation_app as stack
    from tests.test_subscription_churn_cases import _TermEmbedder

    settings_type = Settings if adapters is None else stack._ScriptedRecipientSettings
    extra = {} if adapters is None else {"investigation_adapters": adapters}
    app = create_app(
        settings=stack._settings(root, settings_type),
        embedder_factory=lambda _model: _TermEmbedder(),
        llm_client_factory=lambda _settings: stack.RecordingLLM(root),
        **extra,
    )
    with TestClient(app) as client:
        yield client


def _start(client: Any) -> dict[str, Any]:
    """Upload the sample data, ask the question, and start the proposed investigation."""
    from tests.test_api_investigation_evidence import _churn_conversation
    from tests.test_api_investigations import _create

    conversation_id, message_id = _churn_conversation(client)
    created = _create(client, conversation_id, message_id)
    if created.status_code != 202:
        raise RuntimeError(f"investigation was not created: {created.status_code}")
    return dict(created.json())


def _wait(client: Any, investigation_id: str, done: Callable[[dict[str, Any]], bool]) -> Any:
    from tests.test_api_investigations import _until

    return _until(client, investigation_id, done)


def _is(*statuses: str) -> Callable[[dict[str, Any]], bool]:
    return lambda body: body.get("status") in statuses


TERMINAL = ("completed", "completed_partial", "failed", "cancelled", "expired", "budget_exhausted")


def _decide_waves(
    client: Any, investigation_id: str, reject: frozenset[str]
) -> tuple[Any, list[dict[str, Any]]]:
    """Decide each SQL wave as a person would, statement by statement, until the run ends."""
    decided: set[str] = set()
    waves: list[dict[str, Any]] = []

    def settled(latest: dict[str, Any]) -> bool:
        wave = latest["sql_wave"]
        if latest["status"] == "awaiting_sql_approval":
            return wave is not None and wave["wave_id"] not in decided
        return latest["status"] in TERMINAL

    while True:
        body = _wait(client, investigation_id, settled)
        if body["status"] != "awaiting_sql_approval":
            return body, waves
        wave = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").json()
        decided.add(wave["wave_id"])
        choices = {
            item["task_id"]: "reject" if item["task_id"] in reject else "approve"
            for item in wave["items"]
        }
        waves.append(
            {
                "sequence": wave["sequence"],
                "statements": [
                    {"task": task, "decision": decision} for task, decision in choices.items()
                ],
            }
        )
        answer = client.post(
            f"/api/v1/investigations/{investigation_id}/sql-wave",
            json={
                "wave_id": wave["wave_id"],
                "wave_digest": wave["wave_digest"],
                "items": [
                    {
                        "task_id": item["task_id"],
                        "sql_fingerprint": item["sql_fingerprint"],
                        "decision": choices[item["task_id"]],
                    }
                    for item in wave["items"]
                ],
            },
        )
        if answer.status_code != 202:
            raise RuntimeError(f"wave decision refused: {answer.text}")


# --- the stable, inspectable result --------------------------------------------------------------


def _references(evidence: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Evidence IDs are random per run; a reference names the kind and task instead."""
    return {item["evidence_id"]: f"{item['kind']}:{item['task_id']}" for item in evidence}


def _measure(side: Mapping[str, Any]) -> str:
    return f"{side['numerator']:g}/{side['denominator']} = {side['display']}"


def _report_summary(evidence_body: Mapping[str, Any]) -> dict[str, Any]:
    evidence = evidence_body["evidence"]
    refs = _references(evidence)
    report = evidence_body["report"]

    def named(ids: Sequence[str]) -> list[str]:
        return sorted(refs[item] for item in ids)

    tables = {table["task_id"]: table for table in report["query_tables"]}
    return {
        "report": {
            "label": report["label"],
            "verification": report["verification"],
            "availability": report["availability"],
            "mode": report["mode"],
            "fallback_reason": report["fallback_reason"],
        },
        "hypotheses": {
            row["hypothesis_id"]: {
                "verdict": row["verdict"],
                "reasons": row["reasons"],
                "tests": row["test_task_ids"],
                "follow_ups": row["follow_up_task_ids"],
                "facts": row["fact_ids"],
                "evidence": named(row["evidence_ids"]),
            }
            for row in report["hypotheses"]
        },
        "facts": {
            fact["id"]: {
                "hypothesis": fact["hypothesis_id"],
                "task": fact["task_id"],
                "baseline": _measure(fact["baseline"]),
                "comparison": _measure(fact["comparison"]),
                "difference": fact["difference"],
                "outcome": fact["outcome"],
                "standardized_by": fact["standardized_by"],
                "evidence": named([fact["derived_evidence_id"], fact["query_evidence_id"]]),
            }
            for fact in report["facts"]
        },
        "independent_check": {
            item["task_id"]: {
                "independently_confirmed": item["independently_confirmed"],
                "rows_in_report": item["task_id"] in tables,
            }
            for item in sorted(evidence, key=lambda item: item["task_id"])
            if item["kind"] == "query"
        },
        "citations": sorted(
            {f"{item['filename']} p.{item['page_number']}" for item in report["citations"]}
        ),
        "critic": [
            {
                "round": round_["round"],
                "replan": round_["replan"],
                "findings": [
                    {
                        "code": finding["code"],
                        "severity": finding["severity"],
                        "disposition": finding["disposition"],
                        "hypothesis": finding["hypothesis_id"],
                    }
                    for finding in round_["findings"]
                ],
            }
            for round_ in report["critiques"]
        ],
        "limitations": sorted({item["code"] for item in report["limitations"]}),
    }


def _calls(adapters: Any) -> dict[str, int]:
    from tests.e2e.investigation_trajectory_scenarios import model_call_kinds

    kinds = model_call_kinds([prompt for model in adapters.models for prompt in model.prompts])
    return {"planning": sum(len(p.calls) for p in adapters.planners), **kinds}


def _approved_run(
    root: Path,
    *,
    sql: Mapping[str, str] | None = None,
    reject: frozenset[str] = frozenset(),
    follow_up_rounds: int = 0,
    refusals: bool = False,
    statement_rows_of: str | None = None,
) -> dict[str, Any]:
    """One investigation that a person approves, over HTTP, from question to report."""
    from apps.api.investigation_report_export import VERIFICATION_SCOPE
    from tests.test_api_investigations import _approve

    adapters = _providers(sql)
    result: dict[str, Any] = {}
    with _deployment(root, adapters) as client:
        started = _start(client)
        investigation_id = started["id"]
        plan = _wait(client, investigation_id, _is("awaiting_plan_approval", "failed"))
        if plan["status"] != "awaiting_plan_approval":
            raise RuntimeError(f"planning did not reach approval: {plan['explanation_code']}")
        if refusals:
            result["before_approval"] = {
                "status": plan["status"],
                "plan_digest": plan["plan_digest"],
                "execution_providers_built": len(adapters.models),
                "evidence_status_code": client.get(
                    f"/api/v1/investigations/{investigation_id}/evidence"
                ).status_code,
                "tasks": [
                    {"id": task["id"], "kind": task["kind"], "assignee": task["assignee"]}
                    for task in plan["plan"]["tasks"]
                ],
                "hypotheses": {
                    item["id"]: item["statement"] for item in plan["plan"]["hypotheses"]
                },
                "approval_requirements": plan["proposal"]["approval_requirements"],
            }
            stale = _approve(client, plan, plan_digest="0" * 64)
            result["altered_plan_digest_refused"] = {
                "status_code": stale.status_code,
                "code": stale.json()["error"]["code"],
            }
        approved = _approve(client, plan, follow_up_rounds=follow_up_rounds)
        if approved.status_code != 200:
            raise RuntimeError(f"plan approval refused: {approved.text}")
        body, waves = _decide_waves(client, investigation_id, reject)
        evidence = client.get(f"/api/v1/investigations/{investigation_id}/evidence").json()
        export = client.get(
            f"/api/v1/investigations/{investigation_id}/report/export",
            params={"format": "markdown"},
        )
    result |= {
        "status": body["status"],
        "follow_up_rounds_delegated": follow_up_rounds,
        "sql_waves": waves,
        **_report_summary(evidence),
        "export_states_verification_limit": VERIFICATION_SCOPE in export.text,
        "scripted_model_calls": _calls(adapters),
    }
    if statement_rows_of is not None:
        item = next(
            entry
            for entry in evidence["evidence"]
            if entry["kind"] == "query" and entry["task_id"] == statement_rows_of
        )
        columns = item["columns"]
        result["forged_statement_returned"] = [
            f"{row[columns.index('quarter_start')]}: "
            f"{row[columns.index('numerator')]}/{row[columns.index('denominator')]}"
            for row in item["rows"]
        ]
        # The statement stays reviewable in the evidence inspector and never leaves in an export.
        result["forged_statement_kept_for_review"] = bool(item["sql"])
        result["forged_statement_in_export"] = item["sql"] in export.text
    return result


def _rejected_plan(root: Path) -> dict[str, Any]:
    from tests.test_api_investigations import _approve

    adapters = _providers()
    with _deployment(root, adapters) as client:
        investigation_id = _start(client)["id"]
        plan = _wait(client, investigation_id, _is("awaiting_plan_approval", "failed"))
        rejected = _approve(client, plan, decision="reject")
        if rejected.status_code != 200:
            raise RuntimeError(f"plan rejection refused: {rejected.text}")
        body = _wait(client, investigation_id, _is("plan_rejected", *TERMINAL))
        evidence = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
    return {
        "status": body["status"],
        "execution_providers_built": len(adapters.models),
        "evidence_status_code": evidence.status_code,
    }


def _production_configuration(root: Path) -> dict[str, Any]:
    """Routing on, with no injected adapters: exactly how production selects a provider today."""
    with _deployment(root, None) as client:
        investigation_id = _start(client)["id"]
        body = _wait(client, investigation_id, _is("failed", "awaiting_plan_approval"))
    return {
        "status": body["status"],
        "explanation_code": body["explanation_code"],
        "plan_produced": body["plan"] is not None,
    }


def _routing_table() -> list[dict[str, Any]]:
    from src.orchestration.investigation_routing import evaluate_investigation_eligibility as gate

    rows = [
        ("Feature flag on, table ready", QUESTION, True, True),
        ("Feature flag off (the default)", QUESTION, False, True),
        ("No table in the conversation", QUESTION, True, False),
        ("A single-fact question", "What is the churn rate in Q2?", True, True),
    ]
    table: list[dict[str, Any]] = []
    for name, question, feature, ready in rows:
        decision = gate(question, feature, ready)
        table.append(
            {
                "case": name,
                "eligible": decision.eligible,
                "reason": decision.primary_reason,
            }
        )
    return table


def run_demo(directory: Path) -> dict[str, Any]:
    """Every scenario, in fresh deployments, returning the stable result document."""
    from packages.evaluation.workflow_baseline import _is_local
    from tests.e2e.workflow_baseline_runner import _record_network
    from tests.investigation_execution_fakes import reset_replay_registry
    from tests.test_investigation_red_team import FABRICATED_COUNTS, RELABELLED_POPULATION

    forged = "hypothesis h4 ("
    scenarios: dict[str, Callable[[Path], dict[str, Any]]] = {
        "approved": lambda root: _approved_run(root, refusals=True, follow_up_rounds=1),
        "forged_literal_counts": lambda root: _approved_run(
            root, sql={forged: FABRICATED_COUNTS}, statement_rows_of="h4_rate"
        ),
        "forged_relabelled_population": lambda root: _approved_run(
            root, sql={forged: RELABELLED_POPULATION}, statement_rows_of="h4_rate"
        ),
        "statement_rejected": lambda root: _approved_run(root, reject=frozenset({"h4_rate"})),
        "plan_rejected": _rejected_plan,
        "production_configuration": _production_configuration,
    }
    runs: dict[str, Any] = {}
    roots: list[Path] = []
    with _record_network() as network:
        for name, scenario in scenarios.items():
            reset_replay_registry()
            root = directory / name
            root.mkdir(parents=True)
            roots.append(root)
            runs[name] = scenario(root)
        attempts = [host for host in network if not _is_local(host)]
    from tests.e2e.investigation_app import MODEL_CALL_LOG

    return {
        "schema": "investigation-demo/v1",
        "evidence": {
            "kind": "scripted-investigation-demo",
            "model_is_live": False,
            "statement": STATEMENT,
            "verified_means": VERIFIED_MEANS,
            "scripted": list(SCRIPTED_PARTS),
            "real": list(REAL_PARTS),
        },
        "question": QUESTION,
        "routing_gate": _routing_table(),
        "runs": runs,
        "safety": {
            "non_loopback_network_attempts": len(attempts),
            "ordinary_model_calls": sum((root / MODEL_CALL_LOG).exists() for root in roots),
        },
    }


# --- what a reader sees --------------------------------------------------------------------------


def render_summary(document: Mapping[str, Any]) -> str:
    """The stable text that the demo prints and the guide quotes. Built only from the document."""
    runs = document["runs"]
    approved = runs["approved"]
    lines = [
        "SCRIPTED DEMONSTRATION: no real model was contacted.",
        f"Question: {document['question']}",
        "",
        "Routing gate",
    ]
    lines += [
        f"  {row['case']}: {'eligible' if row['eligible'] else 'not eligible'} ({row['reason']})"
        for row in document["routing_gate"]
    ]
    before = approved["before_approval"]
    stale = approved["altered_plan_digest_refused"]
    lines += [
        "",
        "Before approval",
        f"  status {before['status']}; execution providers built "
        f"{before['execution_providers_built']}; evidence HTTP {before['evidence_status_code']}",
        f"  altered plan digest: HTTP {stale['status_code']} {stale['code']}",
        "",
        "Approved run",
        f"  status {approved['status']}; label '{approved['report']['label']}'; "
        f"follow-up rounds delegated {approved['follow_up_rounds_delegated']}",
    ]
    for wave in approved["sql_waves"]:
        tasks = ", ".join(item["task"] for item in wave["statements"])
        lines.append(f"  SQL wave {wave['sequence']} approved: {tasks}")
    lines += ["", "Hypothesis verdicts"]
    for name, row in approved["hypotheses"].items():
        lines.append(f"  {name}: {row['verdict']} ({', '.join(row['reasons'])})")
        for fact_id in row["facts"]:
            fact = approved["facts"][fact_id]
            strata = fact["standardized_by"]
            standard = f", standardized by {strata['field']}" if strata else ""
            lines.append(
                f"    {fact_id} {fact['task']}: {fact['baseline']} -> {fact['comparison']} "
                f"({fact['difference']}){standard}"
            )
        lines.append(f"    evidence: {', '.join(row['evidence'])}")
    confirmed = approved["independent_check"]
    lines += [
        "",
        "Independent check",
        "  confirmed: "
        + ", ".join(k for k, v in confirmed.items() if v["independently_confirmed"]),
        "  no specification to check: "
        + ", ".join(k for k, v in confirmed.items() if not v["independently_confirmed"]),
    ]
    for name, title in (
        ("forged_literal_counts", "Form A, literal counts"),
        ("forged_relabelled_population", "Form B, relabelled population"),
    ):
        run = runs[name]
        row = run["hypotheses"]["h4"]
        lines.append(
            f"  {title}: statement returned {'; '.join(run['forged_statement_returned'])}; "
            f"h4 {row['verdict']} ({', '.join(row['reasons'])}); "
            f"rows in report {run['independent_check']['h4_rate']['rows_in_report']}"
        )
    critic = approved["critic"]
    lines += ["", "Critic and bounded replanning"]
    for round_ in critic:
        codes = sorted({finding["code"] for finding in round_["findings"]})
        lines.append(f"  round {round_['round']}: replan {round_['replan']}; {', '.join(codes)}")
    rejected = runs["statement_rejected"]
    plan_rejected = runs["plan_rejected"]
    production = runs["production_configuration"]
    h4 = rejected["hypotheses"]["h4"]
    lines += [
        "",
        "Human decisions and fail-closed paths",
        f"  rejected statement h4_rate: status {rejected['status']}; "
        f"h4 {h4['verdict']} ({', '.join(h4['reasons'])})",
        f"  rejected plan: status {plan_rejected['status']}; execution providers built "
        f"{plan_rejected['execution_providers_built']}; "
        f"evidence HTTP {plan_rejected['evidence_status_code']}",
        f"  production configuration: status {production['status']}; "
        f"{production['explanation_code']}; plan produced {production['plan_produced']}",
        "",
        "Offline",
        "  non-loopback network attempts: "
        f"{document['safety']['non_loopback_network_attempts']}; "
        f"ordinary model calls: {document['safety']['ordinary_model_calls']}",
        f"  {document['evidence']['verified_means']}",
    ]
    return "\n".join(lines) + "\n"


def dumps(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def differences(expected: Any, found: Any, path: str = "") -> list[str]:
    """Every difference, by path, so a drift names what changed."""
    if isinstance(expected, dict) and isinstance(found, dict):
        changes: list[str] = []
        for key in sorted(expected.keys() | found.keys()):
            where = f"{path}.{key}" if path else key
            if key not in found:
                changes.append(f"{where}: missing")
            elif key not in expected:
                changes.append(f"{where}: unexpected")
            else:
                changes += differences(expected[key], found[key], where)
        return changes
    return [] if expected == found else [f"{path}: expected {expected!r}, found {found!r}"]


@contextmanager
def _quiet_telemetry() -> Iterator[None]:
    """The application's structured log lines stay out of the demonstration's output."""
    from packages.observability.telemetry import TELEMETRY_LOGGER_NAME

    logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
    previous = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted offline investigation demonstration.")
    parser.add_argument("--update", action="store_true", help="Record the result as expected.")
    parser.add_argument(
        "--output", type=invocation_path, default=RESULTS_PATH, help="Where the result is written."
    )
    args = parser.parse_args(argv)

    began = time.monotonic()
    with (
        tempfile.TemporaryDirectory(
            prefix="investigation-demo-", ignore_cleanup_errors=True
        ) as tmp,
        _quiet_telemetry(),
    ):
        document = run_demo(Path(tmp))
    print(render_summary(document))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps(document), encoding="utf-8", newline="\n")
    print(f"Full result: {args.output}")
    print(f"Finished in {time.monotonic() - began:.0f} seconds.")
    if args.update:
        EXPECTED_PATH.write_text(dumps(document), encoding="utf-8", newline="\n")
        print(f"Expected result written to {EXPECTED_PATH}; review the diff before committing.")
        return 0
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    changes = differences(expected, document)
    if changes:
        print("DRIFT from the committed expected result:", file=sys.stderr)
        for change in changes:
            print(f"  {change}", file=sys.stderr)
        return 1
    print("Matches the committed expected result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
