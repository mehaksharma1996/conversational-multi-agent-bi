"""EVAL-2 (#147) S1b: the scripted comparison of both workflows through the real API.

DEVELOPMENT AND TEST ONLY. NOT a live model: the scripted adapters in
``tests/e2e/workflow_comparison_adapters.py`` supply every model output, and the production FastAPI
application handles them. Its results are **scripted evidence, not real-model performance**.

    python -m tests.e2e.workflow_comparison_runner                 # 5 repeats of the 4 cases
    python -m tests.e2e.workflow_comparison_runner --repeats 1 \\
        --case investigation-october-peak-explanation

Each run is a fresh deployment of the production application under a temporary directory: it uploads
the case's sample data and documents, records consent, and asks **only the case's question**.

- Arm A: the bounded workflow, with investigation routing **off**.
- Arm B: the investigation workflow, with routing **on**, through production routing unchanged. When
  the routing gate proposes an investigation, the declared evaluation approver approves exactly the
  plan and the SQL waves it is shown, through the real approval endpoints. It delegates no follow-up
  rounds and never sends SQL. When the gate proposes none, the run is recorded as ``not_routed``;
  the question is never rephrased and the gate is never bypassed.

Evaluator-only inputs stay out of every run. Before the runs the harness reads only the public
questions and data paths. While both arms run, opening any evaluator-only file raises,
constructing a live provider client raises, and every network lookup or connection is recorded; any
non-loopback host stops the comparison before grading. Only after every run has finished does the
harness read the oracle and the case expectations and grade the runs with
``packages.evaluation.workflow_comparison`` (S1a).

Results go to ``evals/results/`` (git-ignored): ``workflow-comparison.json``,
``workflow-comparison.md``, and the content-free per-run grades ``workflow-comparison-grades.json``.
``--compare-with <earlier grades>`` also writes ``workflow-comparison-delta.md``: every outcome,
reason, failure mode, and count that changed, per case and arm. The module entry point re-execs
from a temporary working directory with dotenv loading disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest import mock

from scripts.offline_execution import ensure_offline_module, invocation_path

if __name__ == "__main__":
    ensure_offline_module("tests.e2e.workflow_comparison_runner")

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "evals" / "results"
RESULTS_JSON = "workflow-comparison.json"
RESULTS_SUMMARY = "workflow-comparison.md"
RESULTS_GRADES = "workflow-comparison-grades.json"
RESULTS_DELTA = "workflow-comparison-delta.md"

CHURN_PUBLIC_PATH = ROOT / "evals" / "investigation" / "v1" / "public_cases.json"
RETAIL_CASES_PATH = ROOT / "evals" / "baseline" / "v2" / "investigation_cases.json"
ORACLE_PATH = ROOT / "evals" / "investigation" / "v1" / "oracle.json"
# Files that hold expected values, reference SQL, or graded results. None may be opened while an
# arm runs. The retail case file is read once before the runs, for its questions only.
EVALUATOR_ONLY_PATHS = (
    ORACLE_PATH,
    ROOT / "evals" / "investigation" / "v1" / "reference.sql",
    ROOT / "evals" / "investigation" / "v1" / "trajectory_cases.json",
    ROOT / "evals" / "investigation" / "v1" / "trajectory-baseline.json",
    ROOT / "evals" / "investigation" / "v1" / "demo-expected.json",
    ROOT / "evals" / "baseline" / "v2" / "workflow-baseline.json",
    RETAIL_CASES_PATH,
)

DEFAULT_REPEATS = 5
FOLLOW_UP_ROUNDS = 0  # the approver delegates nothing it has not been shown (E4)
APPROVER = "evaluation-approver"
TERMINAL = (
    "completed",
    "completed_partial",
    "budget_exhausted",
    "plan_invalid",
    "plan_rejected",
    "cancelled",
    "failed",
    "interrupted",
    "expired",
)


# --- public inputs --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicCase:
    """What a run may see: the question and where the case's data and documents are."""

    case_id: str
    kind: str  # "churn" | "retail"
    question: str
    dataset: Path
    documents: tuple[Path, ...]


def load_public_cases(case_ids: Sequence[str] | None = None) -> dict[str, PublicCase]:
    """The compared cases' questions and data paths. Nothing else is kept from the case files."""
    from tests.e2e.workflow_comparison_adapters import CHURN_CASE, COMPARED_CASES

    wanted = tuple(case_ids or COMPARED_CASES)
    unknown = set(wanted) - set(COMPARED_CASES)
    if unknown:
        raise ValueError(f"not a compared case: {sorted(unknown)}")
    churn = json.loads(CHURN_PUBLIC_PATH.read_text(encoding="utf-8"))
    retail = json.loads(RETAIL_CASES_PATH.read_text(encoding="utf-8"))
    cases: dict[str, PublicCase] = {}
    for case in churn["cases"]:
        if case["id"] == CHURN_CASE and CHURN_CASE in wanted:
            cases[CHURN_CASE] = PublicCase(
                CHURN_CASE,
                "churn",
                case["question"],
                ROOT / churn["dataset"],
                tuple(ROOT / "sample_data" / name for name in churn["documents"]),
            )
    for case in retail["cases"]:
        if case["id"] in wanted:
            cases[case["id"]] = PublicCase(
                case["id"],
                "retail",
                case["question"],
                ROOT / retail["dataset"],
                (ROOT / retail["policy"],),
            )
    del retail  # expected values, steps, and reference SQL are never kept for the runs
    return {case_id: cases[case_id] for case_id in wanted}


# --- guards while the arms run --------------------------------------------------------------------


class EvaluatorOnlyFileOpened(PermissionError):
    """An evaluator-only file was opened while an arm was running."""


class LiveClientRefused(RuntimeError):
    """Something tried to construct a live provider client during a scripted run."""


_ACTIVE_GUARDS: list[EvaluatorOnlyGuard] = []
_HOOK_INSTALLED = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "open" or not _ACTIVE_GUARDS or not args:
        return
    target = args[0]
    if not isinstance(target, str | bytes | os.PathLike):
        return
    name = os.path.basename(os.fsdecode(target)).lower()
    for guard in _ACTIVE_GUARDS:
        guard.check(name, target)


class EvaluatorOnlyGuard:
    """While active, opening any of ``paths`` raises and is recorded (a process audit hook)."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._resolved = {str(path.resolve()).lower() for path in paths}
        self._names = {path.name.lower() for path in paths}
        self.blocked: list[str] = []

    def check(self, name: str, target: Any) -> None:
        if name not in self._names:
            return
        resolved = str(Path(os.fsdecode(target)).resolve()).lower()
        if resolved in self._resolved:
            self.blocked.append(name)
            raise EvaluatorOnlyFileOpened(f"evaluator-only file opened during a run: {name}")

    def __enter__(self) -> EvaluatorOnlyGuard:
        global _HOOK_INSTALLED
        if not _HOOK_INSTALLED:
            sys.addaudithook(_audit)  # audit hooks cannot be removed; it idles when inactive
            _HOOK_INSTALLED = True
        _ACTIVE_GUARDS.append(self)
        return self

    def __exit__(self, *exc: object) -> None:
        _ACTIVE_GUARDS.remove(self)


@contextmanager
def no_live_clients() -> Iterator[list[str]]:
    """Constructing a live provider client raises; every attempt is recorded by class name."""
    from src.llm.gemini_client import GeminiClient
    from src.llm.openai_responses_bounded import OpenAIResponsesBoundedAdapter
    from src.llm.responses_transport import ResponsesTransport

    attempts: list[str] = []

    def refusing(name: str) -> Any:
        def init(self: Any, *args: Any, **kwargs: Any) -> None:
            attempts.append(name)
            raise LiveClientRefused(f"{name} must not be constructed in a scripted run")

        return init

    with (
        mock.patch.object(GeminiClient, "__init__", refusing("GeminiClient")),
        mock.patch.object(
            OpenAIResponsesBoundedAdapter, "__init__", refusing("OpenAIResponsesBoundedAdapter")
        ),
        mock.patch.object(ResponsesTransport, "__init__", refusing("ResponsesTransport")),
    ):
        yield attempts


# --- the evaluation approver ----------------------------------------------------------------------


@dataclass
class EvaluationApprover:
    """A declared evaluation-only operator (E4). It approves exactly the plan and the SQL waves it
    is shown, through the real approval endpoints, and records every decision. It never edits SQL:
    a wave decision carries only each item's task, fingerprint, and decision."""

    decisions: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def approve_plan(self, client: Any, body: Mapping[str, Any]) -> Any:
        request = {
            "decision": "approve",
            "plan_digest": body["plan_digest"],
            "authority_epoch": body["authority_epoch"],
            "follow_up_rounds": FOLLOW_UP_ROUNDS,
        }
        self.requests.append(request)
        response = client.post(f"/api/v1/investigations/{body['id']}/plan-decision", json=request)
        self.decisions.append(
            {"kind": "plan", "decision": "approve", "status_code": response.status_code}
        )
        return response

    def decide_waves(self, client: Any, investigation_id: str) -> dict[str, Any]:
        """Approve each SQL wave as it is shown, until the investigation ends."""
        from tests.test_api_investigations import _until

        decided: set[str] = set()

        def settled(latest: dict[str, Any]) -> bool:
            wave = latest["sql_wave"]
            if latest["status"] == "awaiting_sql_approval":
                return wave is not None and wave["wave_id"] not in decided
            return latest["status"] in TERMINAL

        while True:
            body: dict[str, Any] = _until(client, investigation_id, settled)
            if body["status"] != "awaiting_sql_approval":
                return body
            wave = client.get(f"/api/v1/investigations/{investigation_id}/sql-wave").json()
            decided.add(wave["wave_id"])
            request = {
                "wave_id": wave["wave_id"],
                "wave_digest": wave["wave_digest"],
                "items": [
                    {
                        "task_id": item["task_id"],
                        "sql_fingerprint": item["sql_fingerprint"],
                        "decision": "approve",
                    }
                    for item in wave["items"]
                ],
            }
            self.requests.append(request)
            response = client.post(
                f"/api/v1/investigations/{investigation_id}/sql-wave", json=request
            )
            self.decisions.append(
                {
                    "kind": "sql_wave",
                    "sequence": wave["sequence"],
                    "items": len(wave["items"]),
                    "decision": "approve",
                    "status_code": response.status_code,
                }
            )
            if response.status_code != 202:
                raise RuntimeError(f"wave decision refused: {response.status_code}")


# --- one run --------------------------------------------------------------------------------------


@dataclass
class ArmRun:
    """One arm's run of one case. Holds response content in memory only; never written."""

    case_id: str
    arm: str
    repeat: int
    question_sent: str
    route: str
    gate_reason: str
    message: dict[str, Any] | None = None
    investigation: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    model_calls: int = 0
    latency_ms: float = 0.0
    prompts: list[str] = field(default_factory=list)
    planner_outputs: list[str] = field(default_factory=list)
    approver: EvaluationApprover | None = None


@contextmanager
def _deployment(root: Path, case: PublicCase, arm: str, llm: Any, adapters: Any) -> Iterator[Any]:
    """A test client for the production application, configured for one arm."""
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from config.settings import Settings
    from tests.e2e import investigation_app as stack
    from tests.e2e.retail_app import ScriptedRetailEmbedder
    from tests.test_subscription_churn_cases import _TermEmbedder

    settings_type = stack._ScriptedRecipientSettings if arm == "B" else Settings
    settings = replace(
        stack._settings(root, settings_type), investigation_routing_enabled=arm == "B"
    )
    embedder = _TermEmbedder() if case.kind == "churn" else ScriptedRetailEmbedder()
    extra = {"investigation_adapters": adapters} if arm == "B" else {}
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: embedder,
        llm_client_factory=lambda _settings: llm,
        **extra,
    )
    with TestClient(app) as client:
        yield client


def _open_conversation(client: Any, case: PublicCase) -> str:
    """Upload the case's data and documents, record consent, and open a conversation."""
    from tests.consent_helpers import accept_current_notice
    from tests.test_api_investigation_evidence import CHURN_MAPPING

    workspace = client.post("/api/v1/workspaces").json()["id"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        files={"file": (case.dataset.name, case.dataset.read_bytes(), "text/csv")},
    )
    upload.raise_for_status()
    dataset = client.post(f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={})
    dataset.raise_for_status()
    body = dataset.json()
    if case.kind == "churn":
        mapping: dict[str, Any] = dict(CHURN_MAPPING)
    else:
        mapping = {
            f: item["source_column"] for f, item in body["schema_mapping"]["mappings"].items()
        }
    client.put(f"/api/v1/datasets/{body['id']}/schema-mapping", json=mapping).raise_for_status()
    accept_current_notice(client, workspace).raise_for_status()
    documents = client.post(
        f"/api/v1/workspaces/{workspace}/document-collections",
        files=[
            ("files", (path.name, path.read_bytes(), "application/pdf")) for path in case.documents
        ],
    )
    documents.raise_for_status()
    conversation = client.post(
        f"/api/v1/workspaces/{workspace}/conversations",
        json={"dataset_id": body["id"], "document_collection_id": documents.json()["id"]},
    )
    conversation.raise_for_status()
    return str(conversation.json()["id"])


def run_arm(root: Path, case: PublicCase, arm: str, repeat: int) -> ArmRun:
    """One fresh deployment, one question, through the API, to a terminal outcome."""
    from src.orchestration.investigation_routing import evaluate_investigation_eligibility
    from tests.e2e.workflow_comparison_adapters import (
        ComparisonInvestigationAdapters,
        ScriptedBoundedComparisonLLM,
        bounded_scripts,
    )
    from tests.test_api_investigations import _create, _until

    llm = ScriptedBoundedComparisonLLM({case.question: bounded_scripts()[case.case_id]})
    adapters = ComparisonInvestigationAdapters(case.case_id) if arm == "B" else None
    gate = evaluate_investigation_eligibility(case.question, arm == "B", True)
    with _deployment(root, case, arm, llm, adapters) as client:
        conversation_id = _open_conversation(client, case)
        started = time.perf_counter()
        response = client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"question": case.question}
        )
        response.raise_for_status()
        message = response.json()
        run = ArmRun(
            case.case_id,
            arm,
            repeat,
            question_sent=case.question,
            route=str(message["route"]),
            gate_reason=str(gate.primary_reason),
        )
        if arm == "A":
            run.message = message
        elif message["route"] == "investigation":
            approver = EvaluationApprover()
            run.approver = approver
            created = _create(client, conversation_id, message["id"], key=f"eval2-{repeat}")
            if created.status_code != 202:
                raise RuntimeError(f"investigation not created: {created.status_code}")
            investigation_id = created.json()["id"]
            planned = _until(
                client,
                investigation_id,
                lambda body: (
                    body["status"] == "awaiting_plan_approval" or body["status"] in TERMINAL
                ),
            )
            final = planned
            if planned["status"] == "awaiting_plan_approval":
                approved = approver.approve_plan(client, planned)
                if approved.status_code != 200:
                    raise RuntimeError(f"plan approval refused: {approved.status_code}")
                final = approver.decide_waves(client, investigation_id)
            run.investigation = final
            evidence = client.get(f"/api/v1/investigations/{investigation_id}/evidence")
            run.evidence = evidence.json() if evidence.status_code == 200 else None
        run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
    run.model_calls = llm.calls + (adapters.calls if adapters is not None else 0)
    run.prompts = [*llm.prompts, *(adapters.prompts() if adapters is not None else [])]
    run.planner_outputs = adapters.planner_outputs() if adapters is not None else []
    return run


# --- the whole comparison -------------------------------------------------------------------------


@dataclass
class RunPhase:
    """Everything the runs left, before any evaluator-only file has been read."""

    runs: list[ArmRun]
    repeats: int
    blocked_opens: list[str]
    network_hosts: list[str]
    live_client_attempts: list[str]


def run_arms(
    directory: Path, *, repeats: int = DEFAULT_REPEATS, case_ids: Sequence[str] | None = None
) -> RunPhase:
    """Every case, every repeat, both arms, under the evaluator-only, network, and client guards."""
    from tests.e2e.investigation_demo import _quiet_telemetry
    from tests.e2e.workflow_baseline_runner import _record_network

    if not 1 <= repeats <= DEFAULT_REPEATS:
        raise ValueError(f"repeats must be between 1 and {DEFAULT_REPEATS}")
    cases = load_public_cases(case_ids)  # the last read of a case file before grading
    runs: list[ArmRun] = []
    with _quiet_telemetry(), EvaluatorOnlyGuard(EVALUATOR_ONLY_PATHS) as guard:
        _preload()  # guarded: a helper that reads an evaluator-only file at import fails here
        with _record_network() as network, no_live_clients() as attempts:
            for case in cases.values():
                for repeat in range(1, repeats + 1):
                    for arm in ("A", "B"):
                        root = directory / f"{case.case_id}-{arm}-{repeat}"
                        runs.append(run_arm(root, case, arm, repeat))
    remote = [host for host in network if not _is_loopback(host)]
    return RunPhase(runs, repeats, list(guard.blocked), remote, list(attempts))


PRELOADED_MODULES = (
    "apps.api.main",
    "tests.e2e.investigation_demo",
    "src.orchestration.investigation_routing",
    "tests.consent_helpers",
    "tests.e2e.investigation_app",
    "tests.e2e.investigation_trajectory_scenarios",
    "tests.e2e.retail_app",
    "tests.e2e.workflow_comparison_adapters",
    "tests.investigation_hypothesis_fakes",
    "tests.investigation_planning_fakes",
    "tests.investigation_report_fakes",
    "tests.test_api_investigation_evidence",
    "tests.test_api_investigations",
    "tests.test_investigation_tool_gateway",
    "tests.test_subscription_churn_cases",
)


def _preload() -> None:
    """Import the application and the test helpers before the guards are switched on.

    Some test modules build offline test objects at import time (for example a ``GeminiClient``
    with a placeholder key as parametrization data; it is never called). Importing them first keeps
    the guards precise: nothing may construct a live client *while an arm runs*.
    """
    import importlib

    for name in PRELOADED_MODULES:
        importlib.import_module(name)


def _is_loopback(host: str) -> bool:
    import ipaddress

    if host.lower() in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _evaluator_texts(oracle: Mapping[str, Any], retail: Mapping[str, Any]) -> list[str]:
    """Evaluator-only prose and SQL that no model may see.

    Single identifiers (a verdict code such as ``supported_association``, a public document
    filename) are shared vocabulary, not evaluator content, so only text with whitespace and at
    least 20 characters is kept.
    """
    texts: list[str] = [str(oracle.get("visibility", ""))]
    texts += [str(value) for value in oracle.get("definitions", {}).values()]
    texts += [str(item.get("claim", "")) for item in oracle.get("hypotheses", {}).values()]
    for expectation in oracle.get("case_expectations", {}).values():
        texts += [str(value) for value in expectation.values() if isinstance(value, str)]
    for case in retail.get("cases", []):
        texts += list(case.get("steps", []))
        texts += list(case.get("safety_expectations", []))
        texts += [str(case.get(key, "")) for key in ("reference_sql", "ranking_sql")]
        texts += [str(case.get(key, "")) for key in ("expected_behavior", "unsupported_reason")]
        if isinstance(case.get("cross_check"), Mapping):
            texts.append(str(case["cross_check"].get("sql", "")))
        texts += [str(item.get("sql", "")) for item in case.get("descriptive_facts", [])]
    return sorted({text for text in texts if len(text) >= 20 and any(c.isspace() for c in text)})


@dataclass
class Graded:
    document: dict[str, Any]
    grades: list[dict[str, Any]]
    evaluator_texts: list[str]


def grade_runs(phase: RunPhase) -> Graded:
    """Read the evaluator-only files now that every run has finished, and grade with S1a."""
    from packages.evaluation import workflow_comparison as wc
    from tests.e2e.workflow_comparison_adapters import ARM_A_MODEL, ARM_B_MODEL, CHURN_CASE

    if phase.blocked_opens or phase.live_client_attempts or phase.network_hosts:
        raise RuntimeError("a guard tripped during the runs; the results are not graded")
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    churn_public = json.loads(CHURN_PUBLIC_PATH.read_text(encoding="utf-8"))
    retail = json.loads(RETAIL_CASES_PATH.read_text(encoding="utf-8"))
    tolerances = wc.Tolerances.from_oracle(oracle["tolerances"])
    specs = {
        CHURN_CASE: wc.churn_case_spec(
            oracle, next(c for c in churn_public["cases"] if c["id"] == CHURN_CASE)
        ),
        **{case["id"]: wc.retail_case_spec(case) for case in retail["cases"]},
    }
    grades: list[dict[str, Any]] = []
    routing: Counter[tuple[str, str, str, str]] = Counter()
    for run in phase.runs:
        if run.arm == "A":
            view = wc.observe_arm_a(run.message)
        elif run.route != "investigation":
            view = wc.not_routed_arm_b()
        else:
            view = wc.observe_arm_b(run.investigation, run.evidence)
        metering = wc.RunMetering(model_calls=run.model_calls, latency_ms=run.latency_ms)
        grades.append(
            wc.grade_run(specs[run.case_id], view, tolerances, repeat=run.repeat, metering=metering)
        )
        routing[(run.case_id, run.arm, run.route, run.gate_reason)] += 1
    texts = _evaluator_texts(oracle, retail)
    questions = sorted({run.question_sent for run in phase.runs})
    document = wc.build_document(
        grades,
        evidence_kind="scripted",
        provenance={
            "arm_a_investigation_routing": False,
            "arm_b_investigation_routing": True,
            "arm_a_model": ARM_A_MODEL,
            "arm_b_model": ARM_B_MODEL,
            "harness": "tests.e2e.workflow_comparison_runner",
            "approver": APPROVER,
            "follow_up_rounds": FOLLOW_UP_ROUNDS,
            "evaluator_only_opens_during_runs": len(phase.blocked_opens),
            "network_hosts_during_runs": len(phase.network_hosts),
            "live_client_attempts": len(phase.live_client_attempts),
        },
        planned_repeats=phase.repeats,
        sensitive=[*texts, *questions],
        routing=[
            {"case_id": c, "arm": a, "route": r, "gate_reason": g, "runs": n}
            for (c, a, r, g), n in sorted(routing.items())
        ],
    )
    return Graded(document, grades, texts)


def stable_grades(grades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-run grades without timing, so two scripted runs of the same code compare equal."""
    return [{key: value for key, value in grade.items() if key != "metering"} for grade in grades]


def write_results(
    document: Mapping[str, Any],
    results_dir: Path | None = None,
    grades: Sequence[Mapping[str, Any]] | None = None,
) -> list[Path]:
    """Write the document, its summary, and (optionally) the per-run grades: results dir only."""
    from packages.evaluation.workflow_comparison import content_problems, render_summary

    target = results_dir if results_dir is not None else RESULTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    paths = [target / RESULTS_JSON, target / RESULTS_SUMMARY]
    paths[0].write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths[1].write_text(render_summary(document), encoding="utf-8")
    if grades is not None:
        body = {"grades": stable_grades(grades)}
        if content_problems(body):
            raise RuntimeError("per-run grades would carry content")
        paths.append(target / RESULTS_GRADES)
        paths[2].write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths


DELTA_FIELDS = ("failure_mode",)


def grade_deltas(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every outcome, reason, failure mode, or count that differs, per case, arm, and repeat."""

    def keyed(grades: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, int], Mapping[str, Any]]:
        return {(g["case_id"], g["arm"], int(g["repeat"])): g for g in grades}

    old, new = keyed(before), keyed(after)
    if set(old) != set(new):
        raise ValueError("the two grade sets do not cover the same runs")
    changes: list[dict[str, Any]] = []
    for key in sorted(old):
        a, b = old[key], new[key]
        rows: list[tuple[str, Any, Any]] = [
            (name, a.get(name), b.get(name)) for name in DELTA_FIELDS
        ]
        for name in sorted(set(a["dimensions"]) | set(b["dimensions"])):
            was, now = a["dimensions"].get(name, {}), b["dimensions"].get(name, {})
            rows.append(
                (
                    name,
                    f"{was.get('outcome')}:{was.get('reason')}",
                    f"{now.get('outcome')}:{now.get('reason')}",
                )
            )
        # Count fields added after the earlier grades are new information, not changed outcomes.
        for name in sorted(set(a.get("counts", {})) & set(b.get("counts", {}))):
            rows.append((f"counts.{name}", a["counts"][name], b["counts"][name]))
        for field_name, was, now in rows:
            if was != now:
                changes.append(
                    {
                        "case_id": key[0],
                        "arm": key[1],
                        "repeat": key[2],
                        "field": field_name,
                        "before": was,
                        "after": now,
                    }
                )
    return changes


def new_count_fields(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Count fields the later grades report that the earlier grades did not have."""
    old = {name for grade in before for name in grade.get("counts", {})}
    return sorted({name for grade in after for name in grade.get("counts", {})} - old)


def render_deltas(
    changes: Sequence[Mapping[str, Any]],
    before_commit: str,
    added_fields: Sequence[str] = (),
) -> str:
    """Changes grouped across repeats: identical changes in every repeat are listed once."""
    grouped: Counter[tuple[str, str, str, str, str]] = Counter(
        (c["case_id"], c["arm"], c["field"], str(c["before"]), str(c["after"])) for c in changes
    )
    lines = [
        "# EVAL-2 grade deltas",
        "",
        f"Before: per-run grades from commit `{before_commit}`. After: this run. Scripted "
        "evidence, deterministic repeats; not measured live.",
        "",
    ]
    if added_fields:
        lines += [
            "New count fields (not present before, so not compared): "
            + ", ".join(f"`{name}`" for name in added_fields)
            + ".",
            "",
        ]
    lines += [
        "| Case | Arm | Field | Before | After | Repeats |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {case} | {arm} | {field_name} | {was} | {now} | {count} |"
        for (case, arm, field_name, was, now), count in sorted(grouped.items())
    ]
    if not grouped:
        lines.append("| (no changes) | | | | | |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--case", action="append", dest="cases", default=None)
    parser.add_argument(
        "--compare-with",
        type=invocation_path,
        default=None,
        help="earlier per-run grades (JSON) to report outcome changes against",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="eval2-") as directory:
        phase = run_arms(Path(directory), repeats=args.repeats, case_ids=args.cases)
    graded = grade_runs(phase)
    paths = write_results(graded.document, grades=graded.grades)
    if args.compare_with is not None:
        earlier = json.loads(args.compare_with.read_text(encoding="utf-8"))
        current = stable_grades(graded.grades)
        changes = grade_deltas(earlier["grades"], current)
        delta = RESULTS_DIR / RESULTS_DELTA
        delta.write_text(
            render_deltas(
                changes,
                str(earlier.get("commit", "unrecorded")),
                new_count_fields(earlier["grades"], current),
            ),
            encoding="utf-8",
        )
        paths.append(delta)
    for path in paths:
        print(f"wrote {path.relative_to(ROOT)}")
    print("Scripted evidence, not real-model performance; model plus workflow, not workflow alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
