"""The deterministic workflow baseline: per-case outcomes, coverage metrics, and safety invariants.

A run of the workflow cases (``evals/baseline/v2/manifest.json``) through the application produces
one baseline document. With the scripted model it is *application-path regression evidence*: it
shows the application still handles known-correct model output. It is never a measurement of model
accuracy, and the document says so.

Scoring rules:

- Only ``measured`` cases are graded. Every measured case is in the pass-rate denominator, including
  failures and cases the run never reached (``not_attempted``).
- Cases with any other coverage label are ``not_measured``: they are run and observed, but never
  counted as a pass or a failure.
- Rates carry a Wilson 95% interval, because the case counts are small.

Safety invariants are evaluated from harness evidence, independently of the scores and of the
application's own guards, and fail closed: missing or incomplete evidence is a failure. No update
option can override one. The known-string leakage check finds only the strings it is given; it is
not a general secret-disclosure detector.

The baseline holds no timestamps, random identifiers, latency, token counts, costs, answers, SQL
text, or dataset rows, so two runs of the same code produce the same document.

This module is framework-free. ``tests/e2e/workflow_baseline_runner.py`` (development only) runs
the application with the scripted model and collects the evidence.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.evaluation.live_demo import DIMENSIONS, cells_contain
from packages.evaluation.workflow_cases import (
    CATEGORIES,
    COVERAGE_LABELS,
    causal_markers_in,
    fixture_sha256,
)

BASELINE_SCHEMA = "workflow-baseline-v1"
EVIDENCE_KIND = "scripted-application-path-regression"
EVIDENCE_STATEMENT = (
    "Scripted-provider results are application-path regression evidence: they show that the "
    "application handles known-correct model output. They are not a measurement of model accuracy."
)
OUTCOMES = ("passed", "failed", "not_attempted", "not_measured")
SAFETY_INVARIANTS = (
    "read_only_sql",
    "no_known_string_leakage",
    "no_server_errors",
    "dataset_unchanged",
    "no_live_model",
)
WILSON_Z_95 = 1.959963984540054
MIN_JUSTIFICATION_CHARS = 20
_RATE_DIGITS = 6
# SQLite authorizer actions a read-only query needs. Anything else (writes, schema changes,
# PRAGMA, ATTACH, transactions) is a write or a side effect.
_READ_ONLY_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
# Observation fields copied from the harness's per-case record; none can carry answer text, SQL
# text, rows, or secrets.
_RECORD_OBSERVATIONS = (
    "http_status",
    "route",
    "status",
    "row_count",
    "grounding_status",
    "policy_sql_status",
    "structured_output_repairs",
    "structured_output_failures",
    "model_calls",
)


class BaselineError(ValueError):
    """The inputs cannot produce a trustworthy baseline (for example, a manifest mismatch)."""


# --------------------------------------------------------------------------------------------
# Evidence collected by the harness
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TracedStatement:
    """One statement SQLite executed on a query-executor connection, as its trace reported it."""

    request_index: int | None
    database: str
    read_only_connection: bool
    sql: str


@dataclass(frozen=True)
class HttpExchange:
    """One API request made during the run. ``status`` is None when no response started."""

    index: int
    method: str
    path: str
    status: int | None
    body: bytes

    @property
    def is_message(self) -> bool:
        return self.method == "POST" and self.path.endswith("/messages")


@dataclass(frozen=True)
class DatasetSnapshot:
    """Content digests of every dataset database, read through read-only connections."""

    digests: Mapping[str, str]
    schema_sql: Sequence[str]
    tables: int


@dataclass(frozen=True)
class ModelEvidence:
    """What the harness can show about which model answered."""

    expected_provider: str
    expected_model: str
    scripted_calls: int
    network_attempts: Sequence[str] | None  # None: the network audit hook was not active


@dataclass
class RunEvidence:
    """Everything one run produced. ``None`` means the evidence was not collected."""

    report: Mapping[str, Any]
    exchanges: Sequence[HttpExchange] | None
    statements: Sequence[TracedStatement] | None
    dataset_before: DatasetSnapshot | None
    dataset_after: DatasetSnapshot | None
    fixtures_before: Mapping[str, str] | None
    fixtures_after: Mapping[str, str] | None
    model: ModelEvidence | None
    secrets: Sequence[str] = ()
    forbidden_fragments: Sequence[str] = ()
    messages: dict[str, Mapping[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------


def wilson_interval(
    successes: int, trials: int, z: float = WILSON_Z_95
) -> tuple[float, float] | None:
    """The Wilson score interval for a binomial proportion; None when there are no trials."""
    if trials < 0 or not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    if trials == 0:
        return None
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    return (
        round(max(0.0, centre - half), _RATE_DIGITS),
        round(min(1.0, centre + half), _RATE_DIGITS),
    )


def rate_block(passed: int, measured: int) -> dict[str, Any]:
    interval = wilson_interval(passed, measured)
    return {
        "measured": measured,
        "passed": passed,
        "pass_rate": round(passed / measured, _RATE_DIGITS) if measured else None,
        "wilson_95": list(interval) if interval else None,
    }


# --------------------------------------------------------------------------------------------
# Dataset integrity and independent read-only checks
# --------------------------------------------------------------------------------------------


def _read_only_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def dataset_digest(path: Path) -> tuple[str, list[str]]:
    """A content digest of every table in a SQLite file, and its ``CREATE TABLE`` statements.

    The database is opened read-only. Rows are read in ``rowid`` order and each value is encoded
    with its type, so a changed, added, or removed row or a type change alters the digest.
    """
    digest = hashlib.sha256()
    schema: list[str] = []
    with closing(sqlite3.connect(_read_only_uri(path), uri=True)) as connection:
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        for name, sql in tables:
            schema.append(str(sql))
            digest.update(json.dumps(["table", name, sql]).encode())
            quoted = '"' + str(name).replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
                digest.update(json.dumps([[type(v).__name__, repr(v)] for v in row]).encode())
    return digest.hexdigest(), schema


def snapshot_datasets(paths: Iterable[Path]) -> DatasetSnapshot:
    digests: dict[str, str] = {}
    schema: list[str] = []
    for path in sorted({p.resolve() for p in paths}):
        digests[str(path)], tables = dataset_digest(path)
        schema.extend(tables)
    return DatasetSnapshot(digests=digests, schema_sql=schema, tables=len(schema))


def statement_is_read_only(schema_sql: Sequence[str], sql: str) -> bool | None:
    """Whether SQLite would only read to run ``sql``; None when that cannot be decided.

    The statement is prepared (via ``EXPLAIN``, never run) on an empty in-memory copy of the
    schema, under an authorizer that allows only reads. This does not use the application's
    validator, so it checks the guard rather than repeating it.
    """
    denied: list[int] = []

    def authorize(action: int, *_args: Any) -> int:
        if action in _READ_ONLY_ACTIONS:
            return sqlite3.SQLITE_OK
        denied.append(action)
        return sqlite3.SQLITE_DENY

    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            for statement in schema_sql:
                connection.execute(statement)
        except sqlite3.Error:
            return None
        connection.set_authorizer(authorize)
        try:
            connection.execute(f"EXPLAIN {sql}")
        except (sqlite3.Error, ValueError):
            return False if denied else None
    return not denied


# --------------------------------------------------------------------------------------------
# Safety invariants (independent of the scores; every check fails closed)
# --------------------------------------------------------------------------------------------


def _verdict(reason: str | None, **evidence: Any) -> dict[str, Any]:
    return {"status": "fail" if reason else "pass", "reason": reason or "ok", "evidence": evidence}


def _message_body(exchange: HttpExchange) -> Mapping[str, Any] | None:
    try:
        body = json.loads(exchange.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def check_read_only_sql(evidence: RunEvidence) -> dict[str, Any]:
    """Every traced statement ran on a read-only connection and is read-only when prepared alone.

    Also fails when an answer reports executed SQL that the trace did not see.
    """
    statements, snapshot = evidence.statements, evidence.dataset_before
    if statements is None or snapshot is None or not snapshot.schema_sql:
        return _verdict("missing_evidence")
    if evidence.exchanges is None:
        return _verdict("missing_evidence")
    known = set(snapshot.digests)
    for statement in statements:
        if not statement.read_only_connection:
            return _verdict("connection_not_read_only", statements=len(statements))
        if statement.database not in known:
            return _verdict("unknown_database", statements=len(statements))
        verdict = statement_is_read_only(snapshot.schema_sql, statement.sql)
        if verdict is None:
            return _verdict("indeterminate_statement", statements=len(statements))
        if not verdict:
            return _verdict("write_statement", statements=len(statements))
    traced = {s.request_index for s in statements}
    executed_answers = 0
    for exchange in evidence.exchanges:
        body = _message_body(exchange) if exchange.is_message else None
        if body and body.get("sql") and body.get("rows") is not None:
            executed_answers += 1
            if exchange.index not in traced:
                return _verdict("unobserved_execution", statements=len(statements))
    return _verdict(None, statements=len(statements), answers_with_execution=executed_answers)


def check_known_string_leakage(evidence: RunEvidence) -> dict[str, Any]:
    """No response body contains a configured secret or a known forbidden prompt fragment.

    It finds only these strings; it is not a general secret-disclosure detector.
    """
    exchanges = evidence.exchanges
    needles = [s for s in evidence.secrets if s]
    fragments = [f.casefold() for f in evidence.forbidden_fragments if f]
    if exchanges is None or not any(e.is_message for e in exchanges):
        return _verdict("missing_evidence")
    if not needles and not fragments:
        return _verdict("missing_evidence")
    for exchange in exchanges:
        text = exchange.body.decode("utf-8", errors="replace")
        folded = text.casefold()
        if any(n in text for n in needles) or any(f in folded for f in fragments):
            return _verdict("known_string_found", responses=len(exchanges))
    return _verdict(
        None,
        responses=len(exchanges),
        secrets_checked=len(needles),
        fragments_checked=len(fragments),
    )


def check_no_server_errors(evidence: RunEvidence) -> dict[str, Any]:
    """Every request got a response, none was a 5xx, and every attempted case has its response."""
    exchanges = evidence.exchanges
    if exchanges is None or not exchanges:
        return _verdict("missing_evidence")
    if any(e.status is None for e in exchanges):
        return _verdict("missing_response", responses=len(exchanges))
    if any(e.status is not None and e.status >= 500 for e in exchanges):
        return _verdict("server_error", responses=len(exchanges))
    attempted = len(evidence.report.get("cases", []))
    if sum(1 for e in exchanges if e.is_message) != attempted:
        return _verdict("incomplete_evidence", responses=len(exchanges))
    return _verdict(None, responses=len(exchanges))


def check_dataset_unchanged(evidence: RunEvidence) -> dict[str, Any]:
    """Dataset databases and source fixtures have identical content before and after the run."""
    before, after = evidence.dataset_before, evidence.dataset_after
    if before is None or after is None or not before.digests:
        return _verdict("missing_evidence")
    if evidence.fixtures_before is None or evidence.fixtures_after is None:
        return _verdict("missing_evidence")
    if not evidence.fixtures_before:
        return _verdict("missing_evidence")
    if dict(before.digests) != dict(after.digests):
        return _verdict("dataset_changed", databases=len(before.digests))
    if dict(evidence.fixtures_before) != dict(evidence.fixtures_after):
        return _verdict("fixture_changed", databases=len(before.digests))
    return _verdict(
        None,
        databases=len(before.digests),
        tables=before.tables,
        fixtures=len(evidence.fixtures_before),
    )


def _is_local(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def check_no_live_model(evidence: RunEvidence) -> dict[str, Any]:
    """The run was labelled scripted, every model call reached the scripted model, and nothing
    in the process tried to reach a non-loopback host."""
    model, metadata = evidence.model, evidence.report.get("metadata") or {}
    if model is None or model.network_attempts is None:
        return _verdict("missing_evidence")
    if metadata.get("model_is_live") is not False or metadata.get("mode") == "live":
        return _verdict("labelled_live")
    if (metadata.get("provider"), metadata.get("model")) != (
        model.expected_provider,
        model.expected_model,
    ):
        return _verdict("unexpected_provider")
    model_calls = (evidence.report.get("summary") or {}).get("model_calls")
    if model_calls is None or model_calls != model.scripted_calls:
        return _verdict("unaccounted_model_calls")
    if any(not _is_local(host) for host in model.network_attempts):
        return _verdict("network_egress_attempted")
    return _verdict(None, model_calls=model_calls, network_attempts=0)


_CHECKS = {
    "read_only_sql": check_read_only_sql,
    "no_known_string_leakage": check_known_string_leakage,
    "no_server_errors": check_no_server_errors,
    "dataset_unchanged": check_dataset_unchanged,
    "no_live_model": check_no_live_model,
}


def evaluate_safety(evidence: RunEvidence) -> dict[str, dict[str, Any]]:
    """Every invariant; an exception while checking one is a failure, never a pass."""
    results: dict[str, dict[str, Any]] = {}
    for name in SAFETY_INVARIANTS:
        try:
            results[name] = _CHECKS[name](evidence)
        except Exception as exc:  # noqa: BLE001 - fail closed on any checker error
            results[name] = _verdict("check_error", error=type(exc).__name__)
    return results


# --------------------------------------------------------------------------------------------
# Cases, metrics, and the baseline document
# --------------------------------------------------------------------------------------------


def investigation_observations(
    case: Mapping[str, Any], message: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Non-sensitive facts about an investigation answer. Recorded, never scored.

    Investigation cases have no ``expected_route``; only the fields they define are used.
    """
    if message is None:
        return {"expected_cells_contained": None, "causal_marker_count": None}
    cells = case.get("expected_cells")
    markers = case.get("causal_markers")
    return {
        "expected_cells_contained": (cells_contain(cells, message.get("rows")) if cells else None),
        "causal_marker_count": (
            len(causal_markers_in(str(message.get("answer", "")), markers)) if markers else None
        ),
    }


def _case_entry(
    entry: Mapping[str, Any],
    case: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    statements_by_case: Mapping[str, int],
    message: Mapping[str, Any] | None,
) -> dict[str, Any]:
    measured = entry["coverage"] == "measured"
    attempted = record is not None
    checks: Mapping[str, Any] = (record or {}).get("checks") or {}
    applicable = {k: bool(v) for k, v in checks.items() if v is not None and k in DIMENSIONS}
    if not measured:
        outcome = "not_measured"
    elif not attempted:
        outcome = "not_attempted"
    elif record is not None and record.get("passed") is True and applicable:
        outcome = "passed"
    else:
        outcome = "failed"  # including a measured case with no applicable check: fail closed
    observations: dict[str, Any] = {name: (record or {}).get(name) for name in _RECORD_OBSERVATIONS}
    observations["has_sql"] = bool((record or {}).get("sql")) if attempted else None
    observations["executed_statements"] = statements_by_case.get(entry["id"], 0)
    if case.get("kind") == "investigation":
        observations.update(investigation_observations(case, message))
    return {
        "id": entry["id"],
        "source": entry["source"],
        "category": entry["category"],
        "coverage": entry["coverage"],
        "attempted": attempted,
        "outcome": outcome,
        # Graded dimensions exist only for measured, attempted cases.
        "dimensions": applicable if measured and attempted else {},
        "observations": observations,
    }


def compute_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    measured = [c for c in cases if c["coverage"] == "measured"]
    count = {o: sum(1 for c in cases if c["outcome"] == o) for o in OUTCOMES}
    by_category: dict[str, Any] = {}
    for category in CATEGORIES:
        members = [c for c in cases if c["category"] == category]
        if not members:
            continue
        in_scope = [c for c in members if c["coverage"] == "measured"]
        by_category[category] = {
            "cases": len(members),
            **{o: sum(1 for c in members if c["outcome"] == o) for o in OUTCOMES},
            **rate_block(sum(1 for c in in_scope if c["outcome"] == "passed"), len(in_scope)),
        }
    dimensions = {}
    for name in DIMENSIONS:
        values = [c["dimensions"][name] for c in measured if name in c["dimensions"]]
        if values:
            dimensions[name] = {"passed": sum(values), "applicable": len(values)}
    return {
        "cases": len(cases),
        "measured": len(measured),
        "attempted": sum(1 for c in cases if c["attempted"]),
        "measured_attempted": sum(1 for c in measured if c["attempted"]),
        **count,
        "by_coverage": {
            label: sum(1 for c in cases if c["coverage"] == label) for label in COVERAGE_LABELS
        },
        "overall": rate_block(count["passed"], len(measured)),
        "by_category": by_category,
        "dimensions": dimensions,
    }


def _statements_by_case(evidence: RunEvidence) -> dict[str, int]:
    if evidence.exchanges is None or evidence.statements is None:
        return {}
    messages = [e for e in evidence.exchanges if e.is_message]
    records = list(evidence.report.get("cases", []))
    by_index = {e.index: r["id"] for e, r in zip(messages, records, strict=False)}
    counts: dict[str, int] = {}
    for statement in evidence.statements:
        if statement.request_index is None:
            continue
        case_id = by_index.get(statement.request_index)
        if case_id:
            counts[case_id] = counts.get(case_id, 0) + 1
    return counts


def build_baseline(
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    case_definitions: Mapping[str, Mapping[str, Any]],
    evidence: RunEvidence,
    extra_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The deterministic baseline document for one run. Raises on any case-set mismatch."""
    entries = list(manifest["cases"])
    manifest_ids = [e["id"] for e in entries]
    if set(case_definitions) != set(manifest_ids):
        raise BaselineError("case definitions do not match the manifest case set")
    report = evidence.report
    records = {r["id"]: r for r in report.get("cases", [])}
    not_attempted = set((report.get("summary") or {}).get("cases_not_attempted", []))
    unknown = (set(records) | not_attempted) - set(manifest_ids)
    if unknown:
        raise BaselineError(f"the run reported cases not in the manifest: {sorted(unknown)}")
    missing = set(manifest_ids) - set(records) - not_attempted
    if missing:
        raise BaselineError(f"the run neither attempted nor skipped: {sorted(missing)}")

    statements = _statements_by_case(evidence)
    cases = [
        _case_entry(
            entry,
            case_definitions[entry["id"]],
            records.get(entry["id"]),
            statements,
            evidence.messages.get(entry["id"]),
        )
        for entry in entries
    ]
    safety = evaluate_safety(evidence)
    metadata = report.get("metadata") or {}
    return {
        "schema": BASELINE_SCHEMA,
        "evidence": {
            "kind": EVIDENCE_KIND,
            "statement": EVIDENCE_STATEMENT,
            "model_is_live": bool(metadata.get("model_is_live")),
        },
        "provenance": {
            "manifest_schema": manifest["schema"],
            "manifest_version": manifest["manifest_version"],
            "manifest_sha256": manifest_sha256,
            "fixtures": {k: dict(v) for k, v in sorted(manifest["fixtures"].items())},
            "sources": {k: dict(v) for k, v in sorted(manifest["sources"].items())},
            "live_report_schema": report.get("schema"),
            "mode": metadata.get("mode"),
            "provider": metadata.get("provider"),
            "model": metadata.get("model"),
            "embedding_model": metadata.get("embedding_model"),
            **dict(extra_provenance or {}),
        },
        "metrics": compute_metrics(cases),
        "safety": {k: {"status": v["status"], "reason": v["reason"]} for k, v in safety.items()},
        "safety_evidence": {k: v["evidence"] for k, v in safety.items()},
        "cases": cases,
    }


def safety_failures(baseline: Mapping[str, Any]) -> list[str]:
    """Invariants that did not pass. A missing invariant counts as failed."""
    safety = baseline.get("safety") or {}
    return [name for name in SAFETY_INVARIANTS if (safety.get(name) or {}).get("status") != "pass"]


# --------------------------------------------------------------------------------------------
# Comparison and explicit updates
# --------------------------------------------------------------------------------------------

_CRITICAL_SECTIONS = ("schema", "evidence", "provenance", "metrics", "safety")
_NONCRITICAL_SECTIONS = ("safety_evidence",)
_OUTCOME_RANK = {"passed": 2, "failed": 1, "not_attempted": 0}


@dataclass(frozen=True)
class Change:
    path: str
    before: Any
    after: Any
    regression: bool = False

    def describe(self) -> str:
        tag = "REGRESSION " if self.regression else ""
        return f"{tag}{self.path}: {json.dumps(self.before)} -> {json.dumps(self.after)}"


@dataclass(frozen=True)
class Comparison:
    critical: tuple[Change, ...]
    noncritical: tuple[Change, ...]

    @property
    def regressions(self) -> tuple[Change, ...]:
        return tuple(c for c in self.critical if c.regression)

    @property
    def identical(self) -> bool:
        return not self.critical and not self.noncritical


def _flatten(value: Any, prefix: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key in sorted(value):
            flat.update(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return flat or {prefix: {}}
    return {prefix: value}


def _diff(before: Any, after: Any, prefix: str) -> list[tuple[str, Any, Any]]:
    old, new = _flatten(before, prefix), _flatten(after, prefix)
    missing = object()
    return [
        (path, old.get(path), new.get(path))
        for path in sorted(set(old) | set(new))
        if old.get(path, missing) != new.get(path, missing)
    ]


def _case_regression(path: str, before: Any, after: Any, old: Mapping[str, Any]) -> bool:
    """A previously measured case got worse or left the measured denominator (demotion)."""
    if old.get("coverage") != "measured":
        return False
    if path.endswith(".coverage"):
        return after != "measured"
    if path.endswith(".outcome"):
        return _OUTCOME_RANK.get(after, -1) < _OUTCOME_RANK.get(before, -1)
    if ".dimensions." in path:
        return before is True and after is not True
    return False


def compare_baselines(committed: Mapping[str, Any], current: Mapping[str, Any]) -> Comparison:
    """Exact comparison. Provenance, coverage, scores, and safety are critical; observations and
    safety evidence counts are reported separately as noncritical."""
    critical: list[Change] = []
    noncritical: list[Change] = []
    for section in _CRITICAL_SECTIONS:
        for path, before, after in _diff(committed.get(section), current.get(section), section):
            regression = section == "safety" and path.endswith(".status") and after != "pass"
            critical.append(Change(path, before, after, regression))
    for section in _NONCRITICAL_SECTIONS:
        for path, before, after in _diff(committed.get(section), current.get(section), section):
            noncritical.append(Change(path, before, after))

    old_cases = {c["id"]: c for c in committed.get("cases", [])}
    new_cases = {c["id"]: c for c in current.get("cases", [])}
    for case_id in sorted(set(old_cases) | set(new_cases)):
        old, new = old_cases.get(case_id), new_cases.get(case_id)
        if old is None or new is None:
            # Deleting a previously measured case would shrink the measured denominator.
            removed_measured = new is None and old is not None and old["coverage"] == "measured"
            critical.append(
                Change(f"cases.{case_id}", old is not None, new is not None, removed_measured)
            )
            continue
        graded_old = {k: v for k, v in old.items() if k != "observations"}
        graded_new = {k: v for k, v in new.items() if k != "observations"}
        for path, before, after in _diff(graded_old, graded_new, f"cases.{case_id}"):
            critical.append(Change(path, before, after, _case_regression(path, before, after, old)))
        for path, before, after in _diff(
            old.get("observations"), new.get("observations"), f"cases.{case_id}.observations"
        ):
            noncritical.append(Change(path, before, after))
    return Comparison(tuple(critical), tuple(noncritical))


@dataclass(frozen=True)
class UpdateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    comparison: Comparison | None


def decide_update(
    committed: Mapping[str, Any] | None, current: Mapping[str, Any], justification: str
) -> UpdateDecision:
    """Whether ``current`` may replace ``committed``. There is no override parameter.

    Refused, whatever the justification, when any safety invariant failed or when a previously
    measured case got worse (an outcome or check), was deleted, or was reclassified as unmeasured:
    measured cases never leave the denominator through an ordinary update. Any other change
    (additions, improvements, provenance, observations) needs a written justification of at least
    ``MIN_JUSTIFICATION_CHARS`` characters. Outcomes come from the run and are never edited, so an
    update cannot turn a failure into a pass.
    """
    reasons: list[str] = []
    failures = safety_failures(current)
    if failures:
        reasons.append(f"safety invariants failed: {', '.join(failures)}; no update can accept it")
    comparison = None if committed is None else compare_baselines(committed, current)
    if comparison is not None and comparison.regressions:
        reasons.append(
            f"{len(comparison.regressions)} regression(s) in previously measured cases (worse "
            "results, deletion, or demotion); no update can accept them"
        )
    changed = comparison is None or not comparison.identical
    if changed and len(justification.strip()) < MIN_JUSTIFICATION_CHARS:
        reasons.append(
            f"a written justification of at least {MIN_JUSTIFICATION_CHARS} characters is required"
        )
    return UpdateDecision(not reasons, tuple(reasons), comparison)


def with_review(
    baseline: Mapping[str, Any], justification: str, comparison: Comparison | None
) -> dict[str, Any]:
    """The baseline to write, recording why it was accepted and which changes it accepts."""
    accepted = (
        ["initial baseline"]
        if comparison is None
        else [c.describe() for c in (*comparison.critical, *comparison.noncritical)]
    )
    return {
        **baseline,
        "review": {"justification": justification.strip(), "accepted_changes": accepted},
    }


def render(baseline: Mapping[str, Any]) -> str:
    """Stable JSON text: sorted keys, LF line endings, trailing newline."""
    return json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def fixture_hashes(manifest: Mapping[str, Any], repo_root: Path) -> dict[str, str]:
    """Current hashes of the manifest's fixtures and sources, by the manifest's methods."""
    entries = {**manifest.get("fixtures", {}), **manifest.get("sources", {})}
    return {
        name: fixture_sha256(repo_root / entry["path"], entry["hash"])
        for name, entry in sorted(entries.items())
    }


def combine_case_documents(documents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One cases document from several sources that share the same dataset, policy, and table."""
    if not documents:
        raise BaselineError("no case sources")
    shared = ("dataset", "policy", "table")
    first = documents[0]
    for document in documents[1:]:
        if any(document.get(key) != first.get(key) for key in shared):
            raise BaselineError("case sources must share dataset, policy, and table")
    cases = [case for document in documents for case in document["cases"]]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise BaselineError("duplicate case ids across sources")
    return {
        "version": "+".join(str(d["version"]) for d in documents),
        **{key: first[key] for key in shared},
        "cases": cases,
    }
