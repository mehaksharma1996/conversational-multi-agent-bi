"""The evaluation package exports stay compatible while its pure graders load no application code.

``packages/evaluation/__init__.py`` serves the suite runner's exports lazily (EVAL-2, #147), because
the runner drives the application and imports ``src.*``. These tests pin both halves: a fresh
interpreter that imports the pure comparison grader loads no application, API, or investigation
module and performs no I/O while grading, and every public package export still resolves to the
same object as before.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import packages.evaluation as evaluation

ROOT = Path(__file__).resolve().parents[1]

# Where each public name is defined; the package must hand out exactly these objects.
EXPORT_SOURCES = {
    "CRITICAL_CHECKS": "packages.evaluation.models",
    "EVALUATOR_VERSION": "packages.evaluation.models",
    "BaselineUpdatePlan": "packages.evaluation.gates",
    "CaseResult": "packages.evaluation.models",
    "Check": "packages.evaluation.models",
    "FixtureError": "packages.evaluation.fixtures",
    "FixtureSet": "packages.evaluation.fixtures",
    "GateResult": "packages.evaluation.gates",
    "build_baseline": "packages.evaluation.gates",
    "build_report": "packages.evaluation.runner",
    "evaluate_gates": "packages.evaluation.gates",
    "load_fixtures": "packages.evaluation.fixtures",
    "plan_baseline_update": "packages.evaluation.gates",
    "run_suite": "packages.evaluation.runner",
    "summarize": "packages.evaluation.runner",
}

_APPLICATION_PROBE = """
import sys
def application_modules():
    return sorted(
        name for name in sys.modules
        if name.split(".")[0] in {"src", "apps"}
        or name == "packages.investigation" or name.startswith("packages.investigation.")
        or name == "packages.evaluation.runner"
    )
"""

_GRADING_WITHOUT_IO = """
import builtins, io, json, socket
from decimal import Decimal
import packages.evaluation.workflow_comparison as wc

def refuse(*args, **kwargs):
    raise RuntimeError("I/O attempted")

builtins.open = refuse
io.open = refuse
socket.socket = refuse
socket.create_connection = refuse

spec = wc.CaseSpec(
    case_id="probe",
    role="compared",
    facts=(wc.Fact("r", "g", "rate", Decimal("0.0710")),),
)
message = {"role": "assistant", "status": "complete", "route": "sql", "answer": "It was 7.1%."}
grade = wc.grade_run(spec, wc.observe_arm_a(message), wc.Tolerances(Decimal("0.0001")), repeat=1)
document = wc.build_document(
    [grade],
    evidence_kind="synthetic",
    provenance={
        "arm_a_investigation_routing": False,
        "arm_b_investigation_routing": True,
        "arm_a_model": "none",
        "arm_b_model": "none",
    },
    planned_repeats=1,
)
wc.render_summary(document)
# Reading a displayed column's meaning from its executed statement is pure too (S1d provenance).
table_view = wc.observe_arm_a({
    **message,
    "answer": "",
    "rows": [{"month": "2025-10", "total": 113432.96}],
    "sql": 'SELECT substr(d, 1, 7) AS "month", ROUND(SUM(revenue), 2) AS total FROM t GROUP BY 1',
})
row_facts = [
    wc.Fact("p.0", "p", "label", "2025-10", (), "p"),
    wc.Fact(
        "p.1", "p", "currency", Decimal("113432.96"), ("2025-10",), "p", metric="total_revenue"
    ),
]
tolerances = wc.Tolerances(Decimal("0.0001"))
table = [o.reason for o in wc.grade_facts(row_facts, "", tolerances, table_view.tables)]
print(json.dumps({
    "numeric": grade["dimensions"]["numeric_accuracy"]["outcome"],
    "table": table,
    "loaded": application_modules(),
}))
"""


_JUDGING_WITHOUT_IO = """
import builtins, io, json, socket
import packages.evaluation.comparison_judge as cj

def refuse(*args, **kwargs):
    raise RuntimeError("I/O attempted")

builtins.open = refuse
io.open = refuse
socket.socket = refuse
socket.create_connection = refuse

message = {"role": "assistant", "answer": "It was 7.1%.", "rows": [{"rate": 0.071}], "sources": []}
packet = cj.packet_arm_a("What was the rate?", message)
prompt = cj.build_prompt(packet)

class Scripted:
    identity = cj.JudgeIdentity(kind="scripted", provider="probe", model="probe", version="1")
    def complete(self, text):
        dims = {name: {"status": "rated", "rating": 4, "issues": [], "evidence": ["P1"]}
                for name in cj.DIMENSION_NAMES}
        return json.dumps({"rubric_version": cj.RUBRIC_VERSION, "packet_id": packet.packet_id,
                           "decision": "assessed", "dimensions": dims})

outcome = cj.judge_packet(Scripted(), packet)
print(json.dumps({"status": outcome.status, "loaded": application_modules()}))
"""


def test_the_comparison_judge_runs_without_io_or_application_initialization() -> None:
    outcome = json.loads(_fresh(_JUDGING_WITHOUT_IO))
    assert outcome == {"status": "assessed", "loaded": []}


def _fresh(code: str) -> str:
    environment = {**os.environ, "PYTHON_DOTENV_DISABLED": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _APPLICATION_PROBE + code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize(
    "module",
    [
        "packages.evaluation",
        "packages.evaluation.workflow_comparison",
        "packages.evaluation.comparison_judge",
        "packages.evaluation.result_sets",
        "packages.evaluation.workflow_cases",
    ],
)
def test_importing_pure_evaluation_modules_loads_no_application_code(module: str) -> None:
    loaded = json.loads(
        _fresh(f"import {module}\nimport json\nprint(json.dumps(application_modules()))")
    )
    assert loaded == []


def test_the_comparison_grader_runs_without_io_or_application_initialization() -> None:
    outcome = json.loads(_fresh(_GRADING_WITHOUT_IO))
    assert outcome == {"numeric": "PASS", "table": ["all_stated", "all_stated"], "loaded": []}


def test_asking_for_a_runner_export_still_loads_the_runner() -> None:
    loaded = json.loads(
        _fresh(
            "from packages.evaluation import run_suite\nimport json\n"
            "print(json.dumps('packages.evaluation.runner' in application_modules()))"
        )
    )
    assert loaded is True


def test_the_public_api_is_unchanged() -> None:
    assert sorted(evaluation.__all__) == sorted(EXPORT_SOURCES)


@pytest.mark.parametrize("name", sorted(EXPORT_SOURCES))
def test_every_export_is_the_object_its_defining_module_holds(name: str) -> None:
    source = importlib.import_module(EXPORT_SOURCES[name])
    assert getattr(evaluation, name) is getattr(source, name)


def test_callable_exports_stay_callable_and_package_level_imports_work() -> None:
    from packages.evaluation import build_report, load_fixtures, run_suite, summarize
    from packages.evaluation.runner import run_suite as runner_run_suite

    assert run_suite is runner_run_suite
    for function in (build_report, load_fixtures, run_suite, summarize):
        assert callable(function)


def test_star_import_and_dir_still_list_every_export() -> None:
    namespace: dict[str, object] = {}
    exec("from packages.evaluation import *", namespace)
    assert sorted(k for k in namespace if not k.startswith("__")) == sorted(EXPORT_SOURCES)
    assert set(EXPORT_SOURCES) <= set(dir(evaluation))


def test_unknown_names_still_raise_attribute_error() -> None:
    with pytest.raises(AttributeError):
        evaluation.not_an_export  # noqa: B018
    with pytest.raises(ImportError):
        exec("from packages.evaluation import not_an_export", {})


def test_the_evaluation_entry_point_still_imports_its_package_exports() -> None:
    from scripts import run_evaluations

    assert run_evaluations.run_suite is evaluation.run_suite
    assert run_evaluations.load_fixtures is evaluation.load_fixtures
