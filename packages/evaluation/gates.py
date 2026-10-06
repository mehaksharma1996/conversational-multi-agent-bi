"""Quality gates: hard-fail safety checks, thresholds, and regression detection.

A gate fails (and CI blocks) when:

* any critical check fails, in any case, regardless of pass rates;
* a capability's pass rate is below its threshold (thresholds default to 1.0
  because the offline suite is deterministic);
* a case that passed in the committed baseline now fails, or a baseline case
  disappeared, or a new case is missing from the baseline;
* the dataset revision, evaluator version, prompt fingerprints, embedder, or
  retrieval settings differ from the baseline (each is a reviewable change).

Nothing here computes a blended "quality score".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BASELINE_VERSION = 1
_TRACKED_METADATA = (
    ("dataset_version", ("dataset_version",)),
    ("dataset_revision", ("dataset_revision",)),
    ("evaluator_version", ("evaluator_version",)),
    ("prompt_fingerprints", ("prompt_fingerprints",)),
    ("retrieval_settings", ("retrieval_settings",)),
    ("evaluation_embedder", ("embedding", "evaluation_embedder")),
)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": self.failures}


def evaluate_gates(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> GateResult:
    failures: list[dict[str, str]] = []
    aggregate = report["aggregate"]

    for item in aggregate["critical_failures"]:
        failures.append(
            {
                "gate": "critical_check",
                "detail": f"{item['case']}: {item['check']} {item['detail']}".strip(),
            }
        )

    minimums: dict[str, float] = thresholds["capability_min_pass_rate"]
    by_capability = aggregate["by_capability"]
    for capability, minimum in minimums.items():
        bucket = by_capability.get(capability)
        if bucket is None:
            failures.append({"gate": "capability_missing", "detail": capability})
        elif bucket["pass_rate"] < float(minimum):
            failures.append(
                {
                    "gate": "capability_threshold",
                    "detail": f"{capability}: pass rate {bucket['pass_rate']} < {minimum}",
                }
            )
    for capability in by_capability:
        if capability not in minimums:
            failures.append({"gate": "capability_unthresholded", "detail": capability})

    metric_minimums: dict[str, float] = thresholds.get("retrieval_metrics_min", {})
    if metric_minimums:
        measured = aggregate.get("retrieval_metrics")
        for metric, minimum in metric_minimums.items():
            value = None if measured is None else measured.get(metric)
            if value is None or value < float(minimum):
                failures.append(
                    {
                        "gate": "retrieval_metric",
                        "detail": f"{metric}: {value} < {minimum}",
                    }
                )

    if baseline is None:
        failures.append({"gate": "baseline_missing", "detail": "No committed baseline was found."})
    else:
        failures.extend(_baseline_failures(report, baseline))

    return GateResult(passed=not failures, failures=failures)


def _baseline_failures(report: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for label, path in _TRACKED_METADATA:
        current = _dig(report["metadata"], path)
        recorded = _dig(baseline["metadata"], path)
        if current != recorded:
            failures.append(
                {
                    "gate": "baseline_metadata_changed",
                    "detail": f"{label} changed; review the change and update the baseline",
                }
            )

    accepted = {item["id"] for item in baseline.get("accepted_regressions", [])}
    current_cases = {case["id"]: case for case in report["cases"]}
    for case_id, recorded_case in baseline["cases"].items():
        current_case = current_cases.get(case_id)
        if current_case is None:
            failures.append(
                {"gate": "case_removed", "detail": f"{case_id} is in the baseline but not the run"}
            )
            continue
        if case_id in accepted:
            continue
        for check in current_case["checks"]:
            if recorded_case["checks"].get(check["name"]) is True and not check["passed"]:
                failures.append(
                    {"gate": "regression", "detail": f"{case_id}: {check['name']} passed before"}
                )
        for name, was_passing in recorded_case["checks"].items():
            if was_passing and name not in {c["name"] for c in current_case["checks"]}:
                failures.append(
                    {"gate": "regression", "detail": f"{case_id}: check {name} no longer runs"}
                )
    for case_id in current_cases:
        if case_id not in baseline["cases"]:
            failures.append(
                {"gate": "case_unbaselined", "detail": f"{case_id} is not in the baseline"}
            )
    return failures


def build_baseline(
    report: dict[str, Any],
    accepted_regressions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = report["metadata"]
    return {
        "baseline_version": BASELINE_VERSION,
        "metadata": {
            "dataset_version": metadata["dataset_version"],
            "dataset_revision": metadata["dataset_revision"],
            "evaluator_version": metadata["evaluator_version"],
            "prompt_fingerprints": metadata["prompt_fingerprints"],
            "retrieval_settings": metadata["retrieval_settings"],
            "embedding": {"evaluation_embedder": metadata["embedding"]["evaluation_embedder"]},
        },
        "aggregate": {
            "cases": report["aggregate"]["cases"],
            "by_capability": {
                name: {"cases": bucket["cases"], "pass_rate": bucket["pass_rate"]}
                for name, bucket in report["aggregate"]["by_capability"].items()
            },
        },
        "cases": {
            case["id"]: {
                "capability": case["capability"],
                "passed": case["passed"],
                "checks": {check["name"]: check["passed"] for check in case["checks"]},
            }
            for case in sorted(report["cases"], key=lambda item: item["id"])
        },
        "accepted_regressions": accepted_regressions or [],
    }


@dataclass(frozen=True)
class BaselineUpdatePlan:
    allowed: bool
    blockers: list[str]
    regressions: list[dict[str, Any]]
    changes: list[str]


def plan_baseline_update(
    report: dict[str, Any],
    previous: dict[str, Any] | None,
    accepted_reason: str | None = None,
) -> BaselineUpdatePlan:
    """Decide whether a baseline refresh is legitimate and describe what it changes.

    Critical failures can never be baselined. Regressions can only be recorded
    with an explicit reason, and are then written into the baseline file itself
    so the acceptance is visible in review.
    """
    blockers = [
        f"critical failure cannot be baselined: {item['case']}: {item['check']}"
        for item in report["aggregate"]["critical_failures"]
    ]
    regressions: list[dict[str, Any]] = []
    changes: list[str] = []
    if previous is not None:
        current_cases = {case["id"]: case for case in report["cases"]}
        for case_id, recorded in previous["cases"].items():
            current = current_cases.get(case_id)
            if current is None:
                changes.append(f"case removed: {case_id}")
                continue
            newly_failing = [
                check["name"]
                for check in current["checks"]
                if recorded["checks"].get(check["name"]) is True and not check["passed"]
            ]
            if newly_failing:
                regressions.append({"id": case_id, "checks": newly_failing})
            elif not recorded["passed"] and current["passed"]:
                changes.append(f"case improved: {case_id}")
        changes.extend(f"case added: {i}" for i in current_cases if i not in previous["cases"])
    if regressions and not (accepted_reason and accepted_reason.strip()):
        blockers.append(
            "regressions require --accept-regressions with a written reason: "
            + ", ".join(item["id"] for item in regressions)
        )
    return BaselineUpdatePlan(
        allowed=not blockers,
        blockers=blockers,
        regressions=regressions,
        changes=changes,
    )


def _dig(mapping: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = mapping
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value
