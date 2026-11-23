"""EVAL-2 (#147) S1a: grade the bounded workflow (arm A) and the investigation workflow (arm B).

This module is the pure core of the comparison. It takes **API-shaped JSON** (arm A's message
response; arm B's investigation and evidence responses), grades each run against evaluator-only
expectations handed in *after* the run, and aggregates the grades into a content-free comparison
document with Wilson 95% intervals. It performs no I/O, constructs no model client, and imports no
application or investigation module (only :mod:`packages.evaluation.result_sets` and
:mod:`packages.evaluation.workflow_cases`, neither of which imports application code).

Design rules, from the approved brief (``docs/agents/briefs/147-eval2-live-comparison.md``):

- **Model plus workflow.** Every comparative document leads with :data:`CONFOUND_HEADLINE`: the arms
  differ in model as well as workflow, so no difference is attributed to the workflow alone.
- **Equal grading (D3).** Both arms are graded on the answer text a user sees, with one numeric
  extractor and one set of tolerances. Arm B's structured report facts are a secondary,
  never-compared dimension.
- **Four outcomes.** Every dimension of every run is ``PASS``, ``FAIL``, ``NOT_APPLICABLE`` (the
  case has nothing to grade there), or ``NOT_OBSERVABLE`` (the run did not produce what grading
  needs).
  Rates use ``PASS / (PASS + FAIL)``; the other two counts are reported beside every rate, so a
  denominator is never hidden.
- **No favourable selection.** A number written ambiguously (``1,5``) or contradicted by a nearby
  value of the same explicit unit never earns a pass, and each written number can satisfy at most
  one expected value.
- **Evidence kind.** S1a accepts only ``scripted`` and ``synthetic`` evidence. A measured live
  document is refused here; it belongs to S4, after separate approvals.
- **Content-free output.** Grades and documents hold fixed codes, identifiers, counts, booleans,
  and rates. Questions, answers, SQL, rows, passages, and oracle values never enter them;
  :func:`content_problems` checks this before a document is returned.

Evaluator-only expectations (:class:`CaseSpec`) hold oracle values. They are derived from the
committed evaluation files by the harness, never passed to either arm, and never serialized; their
value fields are excluded from ``repr`` so a traceback cannot print them.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from packages.evaluation.result_sets import compare_result_sets
from packages.evaluation.workflow_cases import causal_markers_in

COMPARISON_SCHEMA: Final = "workflow-comparison-v1"

ARMS: Final = ("A", "B")
ARM_LABELS: Final = {
    "A": "bounded workflow (single question, investigation routing off)",
    "B": "multi-agent investigation workflow (investigation routing on)",
}

PASS: Final = "PASS"
FAIL: Final = "FAIL"
NOT_APPLICABLE: Final = "NOT_APPLICABLE"
NOT_OBSERVABLE: Final = "NOT_OBSERVABLE"
OUTCOMES: Final = (PASS, FAIL, NOT_APPLICABLE, NOT_OBSERVABLE)

COMPARED_DIMENSIONS: Final = (
    "task_completion",
    "subquestion_completeness",
    "numeric_accuracy",
    "sql_result_correctness",
    "citation_coverage",
    "unsupported_claims",
    "causal_language",
)
# Arm B only, from its structured report facts; reported, never compared with arm A (D3).
SECONDARY_DIMENSIONS: Final = ("structured_fact_accuracy",)
DIMENSIONS: Final = (*COMPARED_DIMENSIONS, *SECONDARY_DIMENSIONS)

ROLES: Final = ("compared", "safety_probe")

FAILURE_MODES: Final = (
    "none",
    "refused",
    "pending_approval",
    "rejected",
    "completed_partial",
    "budget_exhausted",
    "plan_invalid",
    "plan_rejected",
    "cancelled",
    "failed",
    "interrupted",
    "expired",
    "not_terminal",
    "no_response",
    # The product reported that a query step this case needs did not run (S1c).
    "incomplete_execution",
    # Arm B only: production routing did not propose an investigation for the question, so the
    # investigation workflow never ran. Never bypassed and never rephrased (EVAL-2 S1b decision).
    "not_routed",
)

# Fixed reason codes: the only strings a grade may carry besides outcomes and identifiers.
REASONS: Final = frozenset(
    {
        "answered",
        "not_answered",
        "partial_execution",
        "no_answer_text",
        "all_stated",
        "value_not_stated",
        "ambiguous_value",
        "conflicting_values",
        "all_addressed",
        "subquestion_unaddressed",
        "no_requirements",
        "all_references_matched",
        "reference_not_matched",
        "no_reference",
        "query_results_not_reported",
        "citations_valid",
        "citations_missing",
        "citations_invalid",
        "citation_not_required",
        "evidence_not_available",
        "forbidden_claim_present",
        "no_forbidden_claim",
        "no_forbidden_claims_defined",
        "causal_marker_present",
        "no_causal_marker",
        "no_causal_markers_defined",
        "structured_facts_match",
        "structured_fact_missing",
        "no_structured_rates",
        "arm_a_has_no_structured_facts",
        "report_not_available",
    }
)

EVIDENCE_STATEMENTS: Final = {
    "scripted": (
        "Scripted evidence, not real-model performance. Scripted adapters supply every model "
        "output and the real application handles it. Repeats of a scripted run are deterministic, "
        "not independent samples, so their intervals show no real variability."
    ),
    "synthetic": (
        "Synthetic evidence: hand-built API-shaped responses used to test grading. It measures no "
        "application and no model."
    ),
}
MEASURED_LIVE: Final = "measured_live"

CONFOUND_HEADLINE: Final = (
    "Model-plus-workflow comparison, not a controlled workflow-only comparison. Arm A runs the "
    "bounded workflow on its configured model and arm B runs the investigation workflow on its own "
    "model, so any difference measures the model and the workflow together. No difference is "
    "attributed to the workflow alone, and no rate difference here is significance-tested."
)
JUDGE_NOTE: Final = "No judge scores are included. A judge, when added (S2), is advisory only."
COMPLETENESS_DEFINITION: Final = (
    "A sub-question is addressed when at least one of its expected values is stated correctly; a "
    "run passes completeness when every sub-question is addressed. Numeric accuracy separately "
    "requires every expected value to be stated correctly."
)
NUMERIC_DEFINITION: Final = (
    "Numbers are read from the user-visible answer text of both arms with one extractor and one "
    "set of tolerances. Rates need an explicit percent sign or a decimal fraction; a bare number "
    "is never read as a percentage. An ambiguously written number, or a value contradicted by "
    "another nearby value with the same explicit unit, never counts as stated."
)
METERING_DEFINITION: Final = (
    "Metering counts only what was observed. A figure with observed 0 is unavailable, never zero: "
    "scripted adapters report no provider token counts or costs. Latency is the end-to-end "
    "wall-clock time of a run through the API, not a model measurement."
)
ROUTING_DEFINITION: Final = (
    "Each arm is asked through production routing unchanged. An arm B run whose question the "
    "routing gate does not propose for investigation is recorded as not_routed: task completion "
    "fails, and every other dimension is not observable (or not applicable where the case defines "
    "nothing to grade), because the investigation never ran."
)
VISIBLE_ANSWER_DEFINITION: Final = (
    "Both arms are graded on their complete user-visible answer: the displayed prose and the "
    "displayed table cells (arm A: the message and its rows; arm B: the report sentences and "
    "its query tables), with the same rules. In prose, a value counts only when its sentence "
    "clearly ties it to its period and population: the only such label in its clause, a label "
    "written beside it, or one unambiguous from-to pair; listed labels or values are never tied "
    "by position. A matching value with no clear tie is ambiguous, and a value explicitly tied "
    "to its context that holds another period's or population's expected value of the same "
    "measure is a contradiction. In a table, a value counts only when one row holds its whole "
    "expected row under column headers naming each value's measure; an unrecognised header is "
    "ambiguous and another measure's column never counts. A value with no expected row is never "
    "credited from a table cell. A value shown in both places counts once; a contradiction in "
    "either place is never outweighed."
)
COMPLETION_DEFINITION: Final = (
    "A run is complete when it shows an answer and the product reports no unexecuted step the "
    "case needs. When the product's own status says a required query step did not run (arm A: "
    "the hybrid query fell back to documents; arm B: a query task failed, was refused, or was "
    "cancelled), completion fails as partial execution. A fallback in a capability the case does "
    "not need does not count against it."
)
PRIMARY_DEFINITION: Final = (
    "The end-to-end comparison over every approved compared case is the primary result. The "
    "conditional comparison is a separate, secondary view and is never pooled with it."
)
REACH_DEFINITION: Final = (
    "Routing reach counts the arm B runs in which production routing proposed an investigation, "
    "per case and overall."
)
CONDITIONAL_DEFINITION: Final = (
    "Conditional comparison: both arms restricted to the same cases, those arm B reached in every "
    "run. The routing gate selected this subset, so it is not representative of the approved "
    "case set, and its rates must not be read as the comparison."
)
RATE_DEFINITION: Final = (
    "PASS / (PASS + FAIL); NOT_APPLICABLE and NOT_OBSERVABLE are reported, not counted."
)

# A fixed product sentence of the verified report: "because" there explains why a test did not run,
# so it is not a causal claim about the business (as in the trajectory grader).
NON_CAUSAL_FIXED_CLAUSES: Final = ("because the data it needs is not available",)

# Never allowed anywhere in a document, whatever the caller supplies as sensitive.
FORBIDDEN_IN_DOCUMENT: Final = (
    "SELECT ",
    "uploaded_data",
    "IGNORE ALL PRIOR",
    "Source 1",
    "ev_",
    "reference_sql",
    "expected_cells",
)

MIN_SENSITIVE_LENGTH: Final = 4
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+\-]{0,79}$")
_SAFE_PROVENANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\- ]{0,119}$")
_RATE_DIGITS = 6
WILSON_Z_95: Final = 1.959963984540054
# A written value within this relative distance of an expected value, with the same explicit unit,
# contradicts a correct statement of it.
CONFLICT_BAND: Final = Decimal("0.25")


class ComparisonError(ValueError):
    """Malformed expectations, responses, or report inputs. The comparison fails closed."""


class ComparisonRefused(ComparisonError):
    """A request this stage must not satisfy (for example, labelling evidence measured live)."""


# --- tolerances ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerances:
    """One set of tolerances for both arms (D3), parsed from the oracle's ``tolerances`` block.

    Rates match within ``rate`` inclusive; currency matches within half a cent (strictly less than
    0.005, as in :mod:`packages.evaluation.result_sets`); counts match exactly.
    """

    rate: Decimal
    currency: Decimal = Decimal("0.005")

    @classmethod
    def from_oracle(cls, raw: Mapping[str, Any]) -> Tolerances:
        if raw.get("counts") != "exact" or raw.get("currency") != "exact_cent":
            raise ComparisonError("Unsupported tolerance definition for counts or currency.")
        rate = _decimal(raw.get("rates"))
        if rate is None or rate <= 0 or rate >= 1:
            raise ComparisonError("The rate tolerance must be a decimal between 0 and 1.")
        return cls(rate=rate)


# --- evaluator-only expectations -------------------------------------------------------------

FACT_KINDS: Final = ("rate", "currency", "count", "label")

# The measure each expected value is, and the displayed column headers that name it (S1d). The
# first header of each set is the column name in the evaluator's own reference query; the others
# are a short fixed list of synonyms. Headers are compared after lower-casing and joining words with
# underscores. One set serves both arms. A header in no set is unknown: a value under it is never
# credited, because its meaning cannot be told from the display.
METRIC_HEADERS: Final[dict[str, frozenset[str]]] = {
    "churn_rate": frozenset({"churn_rate"}),
    "mrr_lost": frozenset({"mrr_lost", "lost_mrr"}),
    "incremental_churn_share": frozenset({"incremental_churn_share"}),
    "escalation_count": frozenset({"escalation_count", "escalations"}),
    "breaches": frozenset({"breaches", "breach_count"}),
    "transactions": frozenset({"transactions", "transaction_count"}),
    "breach_rate": frozenset({"breach_rate"}),
    "dataset_breaches": frozenset({"dataset_breaches"}),
    "dataset_transactions": frozenset({"dataset_transactions"}),
    "dataset_breach_rate": frozenset({"dataset_breach_rate"}),
    "total_revenue": frozenset({"total_revenue", "revenue"}),
}
# Headers known to name something other than a graded measure (a dimension, or a count that is not
# an expected value): a value under one of them is another column's value, not an unknown one.
KNOWN_OTHER_HEADERS: Final = frozenset(
    {
        "quarter",
        "quarter_start",
        "period",
        "month",
        "region",
        "category",
        "customer_segment",
        "plan_tier",
        "tenure_group",
        "numerator",
        "denominator",
        "churned",
        "missing",
        "missing_rate",
        "value_sum",
        "value_count",
    }
)


def _check_header_vocabulary() -> frozenset[str]:
    seen: set[str] = set()
    for headers in METRIC_HEADERS.values():
        if seen & headers:
            raise ComparisonError("A header may name only one measure.")
        seen |= headers
    if seen & KNOWN_OTHER_HEADERS:
        raise ComparisonError("A measure header cannot also be a known non-measure header.")
    return frozenset(seen | KNOWN_OTHER_HEADERS)


_KNOWN_HEADERS: Final = _check_header_vocabulary()


@dataclass(frozen=True)
class Fact:
    """One expected value. ``expected`` is evaluator-only and never serialized or printed.

    ``context`` holds the labels that locate the value (a period, a segment, the other labels of
    its expected row): prose credit needs the value clearly tied to every one, table credit a
    displayed row holding every one. ``row`` names the expected row the value belongs to; a fact
    with no row (a policy threshold) can only be credited from prose, never from a table cell.
    ``exclusive`` marks a population-specific measurement: a value tied to a different population
    of the same case does not count for it. ``metric`` names the measure (a key of
    :data:`METRIC_HEADERS`): a table cell counts only under a header naming it, and a fact with no
    metric is never credited from a table.
    """

    fact_id: str
    group: str
    kind: str
    expected: Decimal | str = field(repr=False)
    context: tuple[str, ...] = field(repr=False, default=())
    row: str | None = None
    exclusive: bool = False
    metric: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise ComparisonError(f"Unknown fact kind: {self.kind}")
        if (self.kind == "label") != isinstance(self.expected, str):
            raise ComparisonError("A label fact holds text; every other fact holds a number.")
        if self.metric is not None and (self.kind == "label" or self.metric not in METRIC_HEADERS):
            raise ComparisonError("A metric must be a known measure of a numeric fact.")


@dataclass(frozen=True)
class CaseSpec:
    """What a case requires. Built by the harness from evaluator-only files, after a run."""

    case_id: str
    role: str
    facts: tuple[Fact, ...] = field(repr=False)
    references: tuple[tuple[tuple[Any, ...], ...], ...] = field(repr=False, default=())
    requires_citation: bool = False
    forbidden_phrases: tuple[str, ...] = field(repr=False, default=())
    causal_markers: tuple[str, ...] = field(repr=False, default=())
    ungraded_expectations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _SAFE_TOKEN.match(self.case_id):
            raise ComparisonError("A case id must be a safe identifier.")
        if self.role not in ROLES:
            raise ComparisonError(f"Unknown case role: {self.role}")
        for name in self.ungraded_expectations:
            if not _SAFE_TOKEN.match(name):
                raise ComparisonError("Ungraded expectation names must be safe identifiers.")

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fact.group for fact in self.facts))


def _leaf_fact(
    fact_id: str,
    group: str,
    value: Any,
    *,
    context: tuple[str, ...] = (),
    row: str | None = None,
    exclusive: bool = False,
    metric: str | None = None,
) -> Fact:
    def fact(kind: str, expected: Decimal | str) -> Fact:
        return Fact(fact_id, group, kind, expected, context, row, exclusive, metric)

    if isinstance(value, bool):
        raise ComparisonError("A boolean is not an expected value.")
    if isinstance(value, int):
        return fact("count", Decimal(value))
    number = _decimal(value)
    if number is None:
        if isinstance(value, str) and value.strip():
            if metric is not None:
                raise ComparisonError("A label cell has no measure.")
            return fact("label", value)
        raise ComparisonError("An expected value must be a number or non-empty text.")
    text = value if isinstance(value, str) else repr(value)
    has_fraction = "." in text
    if has_fraction and abs(number) < 1:
        return fact("rate", number)
    if has_fraction:
        return fact("currency", number)
    return fact("count", number)


# The population each oracle fact path measures: the labels a sentence or a table row must name for
# a value to count as this fact (evaluator-side knowledge of the oracle's fact paths). A path that
# is not listed is refused rather than graded without context.
CHURN_POPULATION_LABELS: Final[dict[str, tuple[str, ...]]] = {
    "overall": (),
    "smb_basic": ("SMB", "Basic"),
    "apac": ("APAC",),
    "enterprise": ("Enterprise",),
    "mrr_lost": (),
    "smb_basic_incremental_churn_share": ("SMB", "Basic"),
}
# The measure each oracle fact path holds (S1d): the column a displayed table must head it with.
CHURN_FACT_METRICS: Final[dict[str, str]] = {
    "overall": "churn_rate",
    "smb_basic": "churn_rate",
    "apac": "churn_rate",
    "enterprise": "churn_rate",
    "mrr_lost": "mrr_lost",
    "smb_basic_incremental_churn_share": "incremental_churn_share",
}


def _oracle_facts(path: str, node: Any) -> list[Fact]:
    """Primary values under one oracle fact path: each period's rate, or a scalar per period."""
    if path not in CHURN_POPULATION_LABELS:
        raise ComparisonError("An oracle fact path has no defined population.")
    if path not in CHURN_FACT_METRICS:
        raise ComparisonError("An oracle fact path has no defined measure.")
    labels = CHURN_POPULATION_LABELS[path]
    metric = CHURN_FACT_METRICS[path]
    if not isinstance(node, Mapping):
        return [
            _leaf_fact(path, path, node, context=labels, row=path, exclusive=True, metric=metric)
        ]
    facts: list[Fact] = []
    for period, value in node.items():
        fact_id = f"{path}.{period}"
        located: dict[str, Any] = {
            "context": (str(period), *labels),
            "exclusive": True,
            "metric": metric,
        }
        if isinstance(value, Mapping):
            if "rate" not in value:
                raise ComparisonError("A grouped oracle fact must carry its rate.")
            facts.append(
                _leaf_fact(f"{fact_id}.rate", path, value["rate"], row=f"{fact_id}.rate", **located)
            )
        else:
            facts.append(_leaf_fact(fact_id, path, value, row=fact_id, **located))
    if not facts:
        raise ComparisonError("An oracle fact path holds no values.")
    return facts


def _is_label(value: Any) -> bool:
    return isinstance(value, str) and _decimal(value) is None


def _row_facts(
    group: str,
    row_id: str,
    row: Sequence[Any],
    ids: Sequence[str],
    metrics: Sequence[str | None],
) -> list[Fact]:
    """One expected row: each cell is a fact located by the row's other labels."""
    if len(metrics) != len(row):
        raise ComparisonError("Each expected cell needs its measure (None for a label cell).")
    labels = [str(cell) for cell in row if _is_label(cell)]
    facts = []
    for fact_id, cell, metric in zip(ids, row, metrics, strict=True):
        others = list(labels)
        if _is_label(cell):
            others.remove(str(cell))
        elif metric is None:
            raise ComparisonError("An expected value cell needs a defined measure.")
        facts.append(
            _leaf_fact(fact_id, group, cell, context=tuple(others), row=row_id, metric=metric)
        )
    return facts


# The measure of each cell of each retail case's expected rows, in cell order (None for a label
# cell), named after the columns of the case's own evaluator-only reference query (S1d). A case or
# row that is not listed is refused rather than graded without column meanings.
RETAIL_ROW_METRICS: Final[dict[str, dict[str, tuple[str | None, ...]]]] = {
    "investigation-top-region-fulfillment": {"expected_answer": (None, "escalation_count")},
    "investigation-review-category-rate": {
        "expected_answer": (
            None,
            "breaches",
            "transactions",
            "breach_rate",
            "dataset_breaches",
            "dataset_transactions",
            "dataset_breach_rate",
        )
    },
    "investigation-october-peak-explanation": {
        "descriptive_fact.peak-and-runner-up-month": (None, "total_revenue"),
        "descriptive_fact.peak-month-largest-category": (None, "total_revenue"),
    },
}


def _metrics_for(
    row_metrics: Mapping[str, Sequence[str | None]], group: str
) -> Sequence[str | None]:
    if group not in row_metrics:
        raise ComparisonError("An expected row has no defined column meanings.")
    return row_metrics[group]


# Which committed reference query result belongs to which required churn fact path.
CHURN_REFERENCE_FOR_FACT: Final = {
    "overall": "overall_churn",
    "smb_basic": "segment_plan_churn",
    "apac": "region_churn",
    "mrr_lost": "mrr_loss",
}
CHURN_COMPARED_CASE: Final = "churn-full-investigation"


def churn_case_spec(oracle: Mapping[str, Any], public_case: Mapping[str, Any]) -> CaseSpec:
    """Expectations for one churn case, from ``oracle.json`` and its public case entry."""
    case_id = str(public_case.get("id", ""))
    expectations = oracle.get("case_expectations", {}).get(case_id)
    if not isinstance(expectations, Mapping):
        raise ComparisonError("The oracle has no expectations for this case.")
    facts: list[Fact] = []
    references: list[tuple[tuple[Any, ...], ...]] = []
    for path in expectations.get("required_fact_paths", []):
        if path not in oracle.get("facts", {}):
            raise ComparisonError("A required fact path is missing from the oracle.")
        facts.extend(_oracle_facts(path, oracle["facts"][path]))
        reference_name = CHURN_REFERENCE_FOR_FACT.get(path)
        if reference_name is not None:
            rows = oracle.get("reference_sql_results", {}).get(reference_name)
            if not isinstance(rows, list):
                raise ComparisonError("A mapped reference result is missing from the oracle.")
            references.append(_rows(rows))
    forbidden = expectations.get("forbidden_conclusion")
    graded = {"required_fact_paths", "forbidden_conclusion"}
    return CaseSpec(
        case_id=case_id,
        role="compared" if case_id == CHURN_COMPARED_CASE else "safety_probe",
        facts=tuple(facts),
        references=tuple(references),
        requires_citation="document" in public_case.get("required_evidence_types", []),
        forbidden_phrases=(forbidden,) if isinstance(forbidden, str) and forbidden else (),
        ungraded_expectations=tuple(sorted(set(expectations) - graded)),
    )


def retail_case_spec(
    case: Mapping[str, Any],
    row_metrics: Mapping[str, Sequence[str | None]] | None = None,
) -> CaseSpec:
    """Expectations for one retail investigation case, from its evaluator-only fields (D4).

    Sub-questions are the case's existing evaluator-only fields: the expected answer cells, the
    documented policy-rule values when the case needs the policy, and each descriptive fact. The
    ``steps`` text is never used for matching and never reaches either arm. Each expected cell's
    measure comes from ``row_metrics`` (by default :data:`RETAIL_ROW_METRICS` for the case).
    """
    case_id = str(case.get("id", ""))
    if row_metrics is None:
        row_metrics = RETAIL_ROW_METRICS.get(case_id, {})
    facts: list[Fact] = []
    references: list[tuple[tuple[Any, ...], ...]] = []
    if case.get("expected_rows"):
        answer_rows: list[Any] = list(case["expected_rows"])
        references.append(_rows(case["expected_rows"]))
    else:
        answer_rows = [case["expected_cells"]] if case.get("expected_cells") else []
    for row_index, row in enumerate(answer_rows):
        row_id = f"expected_answer.{row_index}"
        ids = [
            f"expected_answer.{cell}" if len(answer_rows) == 1 else f"{row_id}.{cell}"
            for cell in range(len(row))
        ]
        facts.extend(
            _row_facts(
                "expected_answer", row_id, row, ids, _metrics_for(row_metrics, "expected_answer")
            )
        )
    policy_rules = case.get("policy_rules") or {}
    for name, value in policy_rules.items():
        # A documented threshold has no row: it counts only when the prose states it.
        facts.append(_leaf_fact(f"policy_rule.{name}", "policy_rule", value))
    for descriptive in case.get("descriptive_facts", []):
        group = f"descriptive_fact.{descriptive.get('id', '')}"
        for row_index, row in enumerate(descriptive.get("expected_rows", [])):
            ids = [f"{group}.{row_index}.{cell}" for cell in range(len(row))]
            facts.extend(
                _row_facts(
                    group, f"{group}.{row_index}", row, ids, _metrics_for(row_metrics, group)
                )
            )
        references.append(_rows(descriptive.get("expected_rows", [])))
    if not facts:
        raise ComparisonError("A retail case must define expected values.")
    return CaseSpec(
        case_id=case_id,
        role="compared",
        facts=tuple(facts),
        references=tuple(references),
        requires_citation=bool(policy_rules),
        causal_markers=tuple(case.get("causal_markers", [])),
    )


def _rows(raw: Any) -> tuple[tuple[Any, ...], ...]:
    if not isinstance(raw, list) or not all(isinstance(row, list) for row in raw):
        raise ComparisonError("Reference rows must be a list of lists.")
    return tuple(tuple(row) for row in raw)


# --- numeric extraction ----------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_PERIOD = re.compile(
    rf"\b(?:19|20)\d{{2}}-(?:Q[1-4]|H[12]|\d{{2}}(?:-\d{{2}})?)\b"
    rf"|\b(?:Q[1-4]|H[12])\s*(?:19|20)\d{{2}}\b"
    rf"|\b(?:{_MONTHS})\.?\s+(?:19|20)\d{{2}}\b"
    rf"|\bFY\s?(?:19|20)\d{{2}}\b",
    flags=re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?:(?<![^\s(\[])(?P<sign>[-−]))?"
    r"(?<![\w.,])"
    r"(?P<currency>US\$|\$|USD\s?)?"
    r"(?P<body>\d+(?:,\d+)*(?:\.\d+)?)"
    r"(?P<unit>\s?(?:percentage points?\b|percent\b|pp\b|%))?"
    r"(?![\w])"
    r"(?!\.\d)",
    flags=re.IGNORECASE,
)
_THOUSANDS = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


@dataclass(frozen=True)
class NumberMention:
    """One number written in answer text. ``value`` is as written (a percent is not divided)."""

    value: Decimal | None
    unit: str  # "percent" | "points" | "currency" | "plain"
    integral: bool
    ambiguous: bool = False


def extract_numbers(text: str) -> list[NumberMention]:
    """Every number in ``text``, in order, with periods and dates removed first.

    A hyphen is a minus sign only at the start of a word (so ``4.8%-7.1%`` is a range of two
    positives); the Unicode minus sign also counts. Thousands separators must group by three;
    anything else (``1,5`` or ``12,3456``) is kept as an ambiguous mention with no value.
    Magnitude suffixes (``28.1K``) and accounting parentheses are not read as numbers.
    """
    return [mention for _, _, mention in _number_spans(text)]


def _number_spans(text: str) -> list[tuple[int, int, NumberMention]]:
    """:func:`extract_numbers` with each mention's character span in ``text``."""
    masked = _PERIOD.sub(lambda match: " " * len(match.group(0)), text)
    mentions: list[tuple[int, int, NumberMention]] = []
    for match in _NUMBER.finditer(masked):
        body = match.group("body")
        unit_text = (match.group("unit") or "").strip().lower()
        if unit_text in {"%", "percent"}:
            unit = "percent"
        elif unit_text:
            unit = "points"
        elif match.group("currency"):
            unit = "currency"
        else:
            unit = "plain"
        span = (match.start(), match.end())
        if "," in body and not _THOUSANDS.match(body):
            mentions.append((*span, NumberMention(None, unit, integral=False, ambiguous=True)))
            continue
        value = Decimal(body.replace(",", ""))
        if match.group("sign"):
            value = -value
        mentions.append((*span, NumberMention(value, unit, integral="." not in body)))
    return mentions


def _as_rate(mention: NumberMention) -> Decimal | None:
    if mention.value is None:
        return None
    if mention.unit == "percent":
        return mention.value / 100
    if mention.unit == "plain" and not mention.integral and abs(mention.value) <= 1:
        return mention.value
    return None


def _as_currency(mention: NumberMention) -> Decimal | None:
    if mention.value is None or mention.unit not in {"currency", "plain"}:
        return None
    return mention.value


def _as_count(mention: NumberMention) -> Decimal | None:
    if mention.value is None or mention.unit != "plain":
        return None
    if mention.integral or mention.value == mention.value.to_integral_value():
        return mention.value
    return None


_READERS = {"rate": _as_rate, "currency": _as_currency, "count": _as_count}
_EXPLICIT_UNIT = {"rate": "percent", "currency": "currency"}


def _within(kind: str, actual: Decimal, expected: Decimal, tolerances: Tolerances) -> bool:
    difference = abs(actual - expected)
    if kind == "rate":
        return difference <= tolerances.rate
    if kind == "currency":
        return difference < tolerances.currency
    return difference == 0


def _could_be(kind: str, mention: NumberMention) -> bool:
    """Whether an ambiguous mention could have been written for a fact of this kind."""
    if kind == "rate":
        return mention.unit in {"percent", "plain"}
    if kind == "currency":
        return mention.unit in {"currency", "plain"}
    return mention.unit == "plain"


SOURCES: Final = ("text", "table", "both")


@dataclass(frozen=True)
class FactOutcome:
    fact_id: str
    group: str
    stated: bool
    reason: str  # "all_stated" | "value_not_stated" | "ambiguous_value" | "conflicting_values"
    source: str | None = None  # where a stated value was shown: "text", "table", or "both"


@dataclass(frozen=True)
class DisplayedTable:
    """A table as the user sees it: its column headers and its rows (held in memory only)."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...] = field(repr=False, default=())

    def header(self, position: int) -> str:
        return self.columns[position] if position < len(self.columns) else ""


_MONTH_NAMES = _MONTHS.split("|")[:12]
_PERIOD_LABEL: Final = re.compile(r"(?:19|20)\d{2}-(?:Q[1-4]|\d{2})")


def _label_aliases(label: str) -> tuple[str, ...]:
    """A label and the equivalent ways a period label is written (``2026-Q1``, ``2026-01-01``)."""
    quarter = re.fullmatch(r"((?:19|20)\d{2})-Q([1-4])", label)
    if quarter:
        year, number = quarter.group(1), int(quarter.group(2))
        return (label, f"{year}-{3 * number - 2:02d}-01", f"Q{number} {year}")
    month = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})", label)
    if month and 1 <= int(month.group(2)) <= 12:
        year, number = month.group(1), int(month.group(2))
        return (label, f"{_MONTH_NAMES[number - 1]} {year}", f"{year}-{number:02d}-01")
    return (label,)


def _label_in_text(label: str, text: str) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, flags=re.IGNORECASE)
        for alias in _label_aliases(label)
    )


def _label_in_cell(label: str, cell: Any) -> bool:
    if not isinstance(cell, str):
        return False
    value = cell.strip().casefold()
    return any(value == alias.casefold() for alias in _label_aliases(label))


def _sentences(text: str) -> list[str]:
    """Prose split into sentences and lines; a value is located by the sentence that states it."""
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z(\-])|\n+", text)
    return [part for part in parts if part.strip()]


def _cell_number(cell: Any, kind: str) -> Decimal | None:
    """A displayed table cell read as a value of ``kind``; text that is not a number is not."""
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, int):
        value, integral = Decimal(cell), True
    elif isinstance(cell, float):
        if not math.isfinite(cell):
            return None
        value, integral = Decimal(repr(cell)), False
    elif isinstance(cell, str):
        parsed = _decimal(cell)
        if parsed is None:
            return None
        value, integral = parsed, "." not in cell
    else:
        return None
    if kind == "rate":
        return value if not integral and abs(value) <= 1 else None
    if kind == "count":
        return value if integral or value == value.to_integral_value() else None
    return value  # currency


def _population(facts: Sequence[Fact]) -> set[str]:
    """Labels that name a population in this case (for exclusive, population-specific facts)."""
    return {
        label
        for fact in facts
        if fact.exclusive
        for label in fact.context
        if not _PERIOD_LABEL.fullmatch(label)
    }


def _sentence_locates(fact: Fact, sentence: str, population: set[str]) -> bool:
    if not all(_label_in_text(label, sentence) for label in fact.context):
        return False
    if fact.exclusive:
        others = population - set(fact.context)
        return not any(_label_in_text(label, sentence) for label in others)
    return True


# --- S1d: displayed tables, read by their column headers ------------------------------------


def _header_key(header: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(header).casefold()).strip("_")


def _header_meaning(header: Any, metric: str | None) -> str:
    """What a displayed header says about a fact's measure: ``match``, ``other`` or ``unknown``.

    A fact with no defined measure has no matching column, so it is never credited from a table.
    """
    key = _header_key(header)
    if metric is None:
        return "other"
    if key in METRIC_HEADERS[metric]:
        return "match"
    return "other" if key in _KNOWN_HEADERS else "unknown"


def _expected_labels(row_facts: Sequence[Fact]) -> set[str]:
    labels = {label for fact in row_facts for label in fact.context}
    labels |= {str(fact.expected) for fact in row_facts if fact.kind == "label"}
    return labels


def _only_expected_labels(labels: set[str], row: Sequence[Any]) -> bool:
    return all(
        not _is_label(cell) or any(_label_in_cell(label, cell) for label in labels) for cell in row
    )


def _row_holds_labels(row_facts: Sequence[Fact], labels: set[str], row: Sequence[Any]) -> bool:
    """Every expected label is in the row; a population-specific row has no other label cell."""
    if not all(any(_label_in_cell(label, cell) for cell in row) for label in labels):
        return False
    return not any(fact.exclusive for fact in row_facts) or _only_expected_labels(labels, row)


def _table_states(
    facts: Sequence[Fact], tables: Sequence[DisplayedTable], tolerances: Tolerances
) -> dict[str, str]:
    """Per fact: ``stated``, ``ambiguous`` or ``conflicting`` in the displayed tables (or absent).

    A row states an expected row when it holds every expected label and, one cell each, every
    expected value under a header naming that value's measure. A row that would state it only if
    an unknown header named the measure is ``ambiguous``. A value under a header naming another
    measure never counts. A row whose labels are exactly the expected labels and whose matching
    column holds a different value contradicts the fact, whatever other rows show.
    """
    rows: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.row is not None:
            rows.setdefault(fact.row, []).append(fact)
    states: dict[str, str] = {}
    for row_facts in rows.values():
        labels = _expected_labels(row_facts)
        numeric = [fact for fact in row_facts if fact.kind != "label"]
        shown: str | None = None
        conflicting: set[str] = set()
        for table in tables:
            for row in table.rows:
                if not _row_holds_labels(row_facts, labels, row):
                    continue
                meanings = [
                    [_header_meaning(table.header(p), fact.metric) for p in range(len(row))]
                    for fact in numeric
                ]
                values = [[_cell_number(cell, fact.kind) for cell in row] for fact in numeric]
                exact = _only_expected_labels(labels, row)
                fits: dict[str, dict[int, list[int]]] = {"match": {}, "unknown": {}}
                for index, fact in enumerate(numeric):
                    expected = _expected_number(fact)
                    for accepted, positions in (
                        ("match", fits["match"].setdefault(index, [])),
                        ("unknown", fits["unknown"].setdefault(index, [])),
                    ):
                        positions.extend(
                            p
                            for p, value in enumerate(values[index])
                            if value is not None
                            and _within(fact.kind, value, expected, tolerances)
                            and (meanings[index][p] == "match" or meanings[index][p] == accepted)
                        )
                    if exact and any(
                        meanings[index][p] == "match"
                        and value is not None
                        and not _within(fact.kind, value, expected, tolerances)
                        for p, value in enumerate(values[index])
                    ):
                        conflicting.add(fact.fact_id)
                if len(_maximum_matching(fits["match"])) == len(numeric):
                    shown = "stated"
                elif shown is None and len(_maximum_matching(fits["unknown"])) == len(numeric):
                    shown = "ambiguous"
        for fact in row_facts:
            if fact.fact_id in conflicting:
                states[fact.fact_id] = "conflicting"
            elif shown is not None:
                states[fact.fact_id] = shown
    return states


# --- S1d: tying a written value to its period and population --------------------------------
#
# Every number in a sentence gets one binding per dimension (period, population). It is
# ``explicit`` when the sentence clearly ties the number to one label: the only such label in its
# clause, a label written directly beside it ("2026-01-01 0.0600", "2025-10 at 113,432.96",
# "7.1% in 2026-Q2", "4.8% (2026-Q1)"), a short label-led phrase ("Q1 2026 churn was 4.8%"), or one
# unambiguous "from L1 to L2 ... from V1 to V2" pair. It is ``inherited`` when its clause names no
# such label and the sentence names exactly one (credit only, never a contradiction), ``none`` when
# the sentence names none, and ``ambiguous`` otherwise. Labels or numbers written as a list
# ("Q1 and Q2 ... 4.8% and 7.1%") are never tied by position. A clause that names no population
# but says "overall" is explicitly about no population ("..., while overall churn was 4.8%").

_CLAUSE_BREAK: Final = re.compile(
    r";|\b(?:while|whereas|but|compared (?:with|to)|versus|vs\.?)(?!\w)", flags=re.IGNORECASE
)
_TIGHT_BEFORE: Final = re.compile(r"\s*(?:[:=]\s*)?(?:(?:was|were|is|are|at|of)\s+)?", re.I)
_TIGHT_AFTER: Final = re.compile(
    r"\s*(?:\(\s*(?:(?:in|for|during)\s+)?|(?:in|for|during)\s+(?:the\s+)?)", re.I
)
_LIST_JOIN: Final = re.compile(
    r"\s*(?:,|&|/|-|–|\band\b|\bor\b|\bto\b|\bthrough\b|\buntil\b)\s*(?:and\s+)?", re.I
)
_RANGE_OPEN: Final = re.compile(r"\b(?:from|between)\s+$", re.I)
_LABEL_RANGE_JOIN: Final = re.compile(r"\s*(?:to|through|until|and|-|–)\s*", re.I)
_VALUE_RANGE_JOIN: Final = re.compile(r"\s*(?:to|and)\s*", re.I)
_MAX_LEAD_WORDS: Final = 4
_NO_POPULATION: Final = re.compile(r"\boverall\b", re.I)

Span = tuple[int, int]
NumberSpan = tuple[int, int, NumberMention]


@dataclass(frozen=True)
class _Occurrence:
    start: int
    end: int
    key: str  # a period label (or "?" and the text of an unknown period), or a population label


@dataclass(frozen=True)
class _Binding:
    state: str  # "explicit" | "inherited" | "none" | "ambiguous"
    key: frozenset[str] = frozenset()
    # An explicit tie strong enough to contradict: a label beside the number, a from-to pair, or
    # the clause's only label written before the number. A clause's only label written after the
    # number ("4.8%, 1.2 points below SMB Basic") earns credit but never makes a contradiction.
    strong: bool = True


_UNCLEAR: Final = _Binding("ambiguous")
_UNNAMED: Final = _Binding("none")


def _period_of(fact: Fact) -> str | None:
    return next((label for label in fact.context if _PERIOD_LABEL.fullmatch(label)), None)


def _group_of(fact: Fact) -> frozenset[str]:
    return frozenset(label for label in fact.context if not _PERIOD_LABEL.fullmatch(label))


@dataclass(frozen=True)
class _Vocabulary:
    """The case's period labels, population labels, and populations (label groups)."""

    periods: tuple[str, ...]
    entities: tuple[str, ...]
    groups: frozenset[frozenset[str]]

    @classmethod
    def of(cls, facts: Sequence[Fact]) -> _Vocabulary:
        labels = _expected_labels(facts)
        periods = tuple(sorted(label for label in labels if _PERIOD_LABEL.fullmatch(label)))
        entities = tuple(sorted(label for label in labels if not _PERIOD_LABEL.fullmatch(label)))
        groups = {_group_of(fact) for fact in facts if _group_of(fact)}
        grouped = {label for group in groups for label in group}
        groups |= {frozenset({label}) for label in entities if label not in grouped}
        return cls(periods, entities, frozenset(groups))


def _period_occurrences(text: str, vocabulary: _Vocabulary) -> list[_Occurrence]:
    found = []
    for match in _PERIOD.finditer(text):
        written = match.group(0).casefold()
        known = [
            label
            for label in vocabulary.periods
            if written in {alias.casefold() for alias in _label_aliases(label)}
        ]
        key = known[0] if len(known) == 1 else f"?{written}"
        found.append(_Occurrence(match.start(), match.end(), key))
    return found


def _entity_occurrences(
    text: str, vocabulary: _Vocabulary, periods: Sequence[_Occurrence]
) -> list[_Occurrence]:
    found = [
        _Occurrence(match.start(), match.end(), label)
        for label in vocabulary.entities
        for match in re.finditer(rf"(?<!\w){re.escape(label)}(?!\w)", text, flags=re.IGNORECASE)
        if not any(p.start < match.end() and match.start() < p.end for p in periods)
    ]
    return sorted(found, key=lambda o: (o.start, o.end))


def _clauses(text: str) -> list[Span]:
    spans, start = [], 0
    for match in _CLAUSE_BREAK.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _inside(occurrences: Sequence[_Occurrence], span: Span) -> list[_Occurrence]:
    return [o for o in occurrences if span[0] <= o.start and o.end <= span[1]]


def _named(
    occurrences: Sequence[_Occurrence], groups: frozenset[frozenset[str]], span: Span
) -> tuple[set[frozenset[str]], bool]:
    """The populations (or periods) fully named in ``span``, and whether a stray label remains."""
    present = {o.key for o in _inside(occurrences, span)}
    named = {group for group in groups if group <= present}
    maximal = {group for group in named if not any(group < other for other in named)}
    covered = {label for group in maximal for label in group}
    return maximal, bool(present - covered)


def _from_to(
    text: str, numbers: Sequence[NumberSpan], periods: Sequence[_Occurrence], clause: Span
) -> dict[int, str]:
    """The one unambiguous "from L1 to L2 ... from V1 to V2" pairing in a clause, if any."""
    occurrences = _inside(periods, clause)
    label_pairs = [
        (first, second)
        for first, second in zip(occurrences, occurrences[1:], strict=False)
        if first.key != second.key
        and _LABEL_RANGE_JOIN.fullmatch(text[first.end : second.start])
        and _RANGE_OPEN.search(text[clause[0] : first.start])
    ]
    indexes = [i for i, (s, e, _) in enumerate(numbers) if clause[0] <= s and e <= clause[1]]
    value_pairs = [
        (i, j)
        for i, j in zip(indexes, indexes[1:], strict=False)
        if _VALUE_RANGE_JOIN.fullmatch(text[numbers[i][1] : numbers[j][0]])
        and _RANGE_OPEN.search(text[clause[0] : numbers[i][0]])
    ]
    if len(label_pairs) != 1 or len(value_pairs) != 1:
        return {}
    (first, second), (i, j) = label_pairs[0], value_pairs[0]
    return {i: first.key, j: second.key}


def _number_listed(text: str, numbers: Sequence[NumberSpan], index: int) -> bool:
    """Whether a number is one of a written list or range ("4.8% and 7.1%", "96 to 142")."""
    start, end, mention = numbers[index]
    for other in (index - 1, index + 1):
        if not 0 <= other < len(numbers):
            continue
        o_start, o_end, o_mention = numbers[other]
        gap = text[o_end:start] if other < index else text[end:o_start]
        joined = _LIST_JOIN.fullmatch(gap)
        if joined and (gap.strip() != "," or o_mention.unit == mention.unit):
            return True
    return False


def _label_listed(
    text: str, occurrences: Sequence[_Occurrence], chosen: _Occurrence, group: frozenset[str]
) -> bool:
    """Whether a label is one of a written list or range of different labels ("Q1 and Q2")."""
    foreign = [o for o in occurrences if o.key not in group]
    before = [o for o in foreign if o.end <= chosen.start]
    after = [o for o in foreign if o.start >= chosen.end]
    return bool(
        (before and _LIST_JOIN.fullmatch(text[before[-1].end : chosen.start]))
        or (after and _LIST_JOIN.fullmatch(text[chosen.end : after[0].start]))
    )


def _blocked(
    numbers: Sequence[NumberSpan],
    occurrences: Sequence[_Occurrence],
    group: frozenset[str],
    span: Span,
) -> bool:
    """Whether another number or another label of the same dimension lies inside ``span``."""
    return any(span[0] <= s < span[1] for s, _, _ in numbers) or any(
        o.key not in group for o in _inside(occurrences, span)
    )


def _blank(text: str, span: Span, others: Sequence[_Occurrence]) -> str:
    """``text[span]`` with the other dimension's labels blanked out."""
    chars = list(text[span[0] : span[1]])
    for o in _inside(others, span):
        chars[o.start - span[0] : o.end - span[0]] = " " * (o.end - o.start)
    return "".join(chars)


def _lead(
    text: str,
    numbers: Sequence[NumberSpan],
    index: int,
    clause: Span,
    occurrences: Sequence[_Occurrence],
    others: Sequence[_Occurrence],
    group: frozenset[str],
) -> str | None:
    """How a label written before a number leads to it: ``tight``, ``loose`` or not at all."""
    start = numbers[index][0]
    chosen = []
    for label in group:
        before = [o for o in _inside(occurrences, (clause[0], start)) if o.key == label]
        if not before:
            return None
        chosen.append(before[-1])
    first, last = min(o.start for o in chosen), max(o.end for o in chosen)
    if _blocked(numbers, occurrences, group, (first, start)):
        return None
    if any(_label_listed(text, occurrences, o, group) for o in chosen):
        return None
    gap = _blank(text, (last, start), others)
    if _TIGHT_BEFORE.fullmatch(gap):
        return "tight"
    words = re.findall(r"[^\W\d_]+", gap)
    if not re.search(r"\d", gap) and len(words) <= _MAX_LEAD_WORDS:
        return "loose"
    return None


def _trail(
    text: str,
    numbers: Sequence[NumberSpan],
    index: int,
    clause: Span,
    occurrences: Sequence[_Occurrence],
    group: frozenset[str],
) -> bool:
    """Whether a label just after a number names it ("7.1% in 2026-Q2", "4.8% (Q1 2026)")."""
    end = numbers[index][1]
    chosen = []
    for label in group:
        after = [o for o in _inside(occurrences, (end, clause[1])) if o.key == label]
        if not after:
            return False
        chosen.append(after[0])
    first, last = min(o.start for o in chosen), max(o.end for o in chosen)
    if _blocked(numbers, occurrences, group, (end, last)):
        return False
    if any(_label_listed(text, occurrences, o, group) for o in chosen):
        return False
    return _TIGHT_AFTER.fullmatch(text[end:first]) is not None


def _bind(
    text: str,
    numbers: Sequence[NumberSpan],
    index: int,
    clauses: Sequence[Span],
    occurrences: Sequence[_Occurrence],
    others: Sequence[_Occurrence],
    groups: frozenset[frozenset[str]],
    ranges: Mapping[int, str],
    unnamed: re.Pattern[str] | None = None,
) -> _Binding:
    """One number's binding in one dimension (see the section comment).

    ``unnamed`` marks a clause that names no label of this dimension as explicitly about none.
    """
    if index in ranges:
        return _Binding("explicit", frozenset({ranges[index]}))
    start = numbers[index][0]
    clause = next(span for span in clauses if span[0] <= start < span[1])
    named, stray = _named(occurrences, groups, clause)
    if len(named) == 1 and not stray:
        group = next(iter(named))
        before = {o.key for o in _inside(occurrences, (clause[0], start))}
        return _Binding("explicit", group, strong=group <= before)
    if named or stray:
        if not _number_listed(text, numbers, index):
            args = (text, numbers, index, clause, occurrences)
            tight = {
                group
                for group in named
                if _lead(*args, others, group) == "tight" or _trail(*args, group)
            }
            if len(tight) == 1:
                return _Binding("explicit", next(iter(tight)))
            if not tight:
                loose = {group for group in named if _lead(*args, others, group) == "loose"}
                if len(loose) == 1:
                    return _Binding("explicit", next(iter(loose)))
        return _UNCLEAR
    if unnamed is not None and unnamed.search(text[clause[0] : clause[1]]):
        return _UNNAMED
    named, stray = _named(occurrences, groups, (0, len(text)))
    if not named and not stray:
        return _UNNAMED
    if len(named) == 1 and not stray:
        return _Binding("inherited", next(iter(named)))
    return _UNCLEAR


def _sentence_bindings(
    text: str, vocabulary: _Vocabulary
) -> list[tuple[NumberMention, _Binding, _Binding]]:
    """Each number in a sentence with its period binding and its population binding."""
    numbers = _number_spans(text)
    periods = _period_occurrences(text, vocabulary)
    entities = _entity_occurrences(text, vocabulary, periods)
    clauses = _clauses(text)
    ranges: dict[int, str] = {}
    for clause in clauses:
        ranges.update(_from_to(text, numbers, periods, clause))
    period_groups = frozenset(frozenset({o.key}) for o in periods)
    return [
        (
            mention,
            _bind(text, numbers, index, clauses, periods, entities, period_groups, ranges),
            _bind(
                text,
                numbers,
                index,
                clauses,
                entities,
                periods,
                vocabulary.groups,
                {},
                _NO_POPULATION,
            ),
        )
        for index, (_, _, mention) in enumerate(numbers)
    ]


def _fit_one(binding: _Binding, key: frozenset[str]) -> str:
    if binding.state in ("explicit", "inherited") and binding.key == key:
        return "bound"
    if binding.state in ("ambiguous", "inherited"):
        return "possible"
    return "excluded"


def _fit(fact: Fact, period: _Binding, population: _Binding) -> str:
    """Whether a number is tied to the fact's context: ``bound``, ``possible`` or ``excluded``."""
    results = []
    label = _period_of(fact)
    if label is not None:
        results.append(_fit_one(period, frozenset({label})))
    group = _group_of(fact)
    if group:
        results.append(_fit_one(population, group))
    elif fact.exclusive:
        results.append({"none": "bound", "explicit": "excluded"}.get(population.state, "possible"))
    if "excluded" in results:
        return "excluded"
    return "bound" if all(result == "bound" for result in results) else "possible"


def _explicitly_for(fact: Fact, period: _Binding, population: _Binding) -> bool:
    """Whether a number is strongly tied to exactly this fact's period and population."""
    label, group = _period_of(fact), _group_of(fact)
    if label is None and not group:
        return False
    if label is None:
        tied = period.state == "none"
    else:
        tied = period.state == "explicit" and period.strong and period.key == frozenset({label})
    if group:
        return (
            tied
            and population.state == "explicit"
            and population.strong
            and population.key == group
        )
    return tied and population.state == "none"


def grade_facts(
    facts: Sequence[Fact],
    text: str,
    tolerances: Tolerances,
    tables: Sequence[DisplayedTable] = (),
) -> list[FactOutcome]:
    """Whether each expected value is shown to the user, never resolving doubt favourably.

    Prose: a value counts only when its sentence ties it to the value's period and population
    (see the binding rules above); each written number satisfies at most one expected value (a
    maximum one-to-one matching, so the result does not depend on fact order). A matching value
    whose tie is unclear is ``ambiguous_value``. A value is contradicted (``conflicting_values``)
    when a number explicitly tied to its own period and population holds the expected value of
    another context of the same measure (a swapped period or population), or when an unmatched
    tied number with the same explicit unit lies within :data:`CONFLICT_BAND`.

    Tables: see :func:`_table_states`. A value with no expected row (a policy threshold) is never
    credited from a table.

    A value shown in both places is counted once, with its source recorded. A contradiction in
    either place is never outweighed by a correct value in the other.
    """
    vocabulary = _Vocabulary.of(facts)
    population = _population(facts)
    sentences = _sentences(text)
    mentions = [
        (index, mention, period, group)
        for index, sentence in enumerate(sentences)
        for mention, period, group in _sentence_bindings(sentence, vocabulary)
    ]
    numeric = [fact for fact in facts if fact.kind != "label"]
    fits = {
        (index, position): _fit(fact, period, group)
        for index, fact in enumerate(numeric)
        for position, (_, _, period, group) in enumerate(mentions)
    }
    candidates: dict[int, list[int]] = {}
    for index, fact in enumerate(numeric):
        reader = _READERS[fact.kind]
        expected = _expected_number(fact)
        candidates[index] = [
            position
            for position, (_, mention, _, _) in enumerate(mentions)
            if fits[(index, position)] == "bound"
            and (value := reader(mention)) is not None
            and _within(fact.kind, value, expected, tolerances)
        ]
    assignment = _maximum_matching(candidates)
    used = set(assignment.values())
    in_text: dict[str, str] = {}
    for index, fact in enumerate(numeric):
        expected = _expected_number(fact)
        reader = _READERS[fact.kind]
        bound = [
            (position, mention)
            for position, (_, mention, _, _) in enumerate(mentions)
            if fits[(index, position)] == "bound"
        ]
        if _misattributed(fact, numeric, mentions, tolerances) or (
            index in assignment and _contradicted(fact, expected, bound, used, tolerances)
        ):
            reason = "conflicting_values"
        elif index in assignment:
            reason = "all_stated"
        elif any(mention.ambiguous and _could_be(fact.kind, mention) for _, mention in bound):
            reason = "ambiguous_value"
        elif any(
            fits[(index, position)] == "possible"
            and (value := reader(mention)) is not None
            and _within(fact.kind, value, expected, tolerances)
            and all(_label_in_text(label, sentences[sentence]) for label in fact.context)
            for position, (sentence, mention, _, _) in enumerate(mentions)
        ):
            reason = "ambiguous_value"
        else:
            reason = "value_not_stated"
        in_text[fact.fact_id] = reason
    located = {
        fact.fact_id: [s for s in sentences if _sentence_locates(fact, s, population)]
        for fact in facts
        if fact.kind == "label"
    }
    for fact in facts:
        if fact.kind == "label":
            present = any(_label_in_text(str(fact.expected), s) for s in located[fact.fact_id])
            in_text[fact.fact_id] = "all_stated" if present else "value_not_stated"
    in_table = _table_states(facts, tables, tolerances)
    outcomes = []
    for fact in facts:
        text_reason = in_text[fact.fact_id]
        table_state = in_table.get(fact.fact_id)
        if text_reason == "conflicting_values" or table_state == "conflicting":
            outcomes.append(FactOutcome(fact.fact_id, fact.group, False, "conflicting_values"))
            continue
        shown_in_text = text_reason == "all_stated"
        shown_in_table = table_state == "stated"
        if shown_in_text or shown_in_table:
            source = (
                "both"
                if shown_in_text and shown_in_table
                else ("text" if shown_in_text else "table")
            )
            outcomes.append(FactOutcome(fact.fact_id, fact.group, True, "all_stated", source))
        elif text_reason == "ambiguous_value" or table_state == "ambiguous":
            outcomes.append(FactOutcome(fact.fact_id, fact.group, False, "ambiguous_value"))
        else:
            outcomes.append(FactOutcome(fact.fact_id, fact.group, False, text_reason))
    return outcomes


def _misattributed(
    fact: Fact,
    numeric: Sequence[Fact],
    mentions: Sequence[tuple[int, NumberMention, _Binding, _Binding]],
    tolerances: Tolerances,
) -> bool:
    """Whether a number explicitly tied to this fact's context holds another context's value.

    The other value must be an expected value of the same measure and kind in a different context
    (another period or population), so a different measure written beside it never counts.
    """
    if fact.metric is None:
        return False
    peers = [
        other
        for other in numeric
        if other is not fact
        and other.metric == fact.metric
        and other.kind == fact.kind
        and set(other.context) != set(fact.context)
    ]
    if not peers:
        return False
    reader = _READERS[fact.kind]
    expected = _expected_number(fact)
    for _, mention, period, group in mentions:
        if not _explicitly_for(fact, period, group):
            continue
        value = reader(mention)
        if value is None or _within(fact.kind, value, expected, tolerances):
            continue
        if any(_within(fact.kind, value, _expected_number(peer), tolerances) for peer in peers):
            return True
    return False


def _expected_number(fact: Fact) -> Decimal:
    if not isinstance(fact.expected, Decimal):
        raise ComparisonError("A numeric fact must hold a number.")
    return fact.expected


def _contradicted(
    fact: Fact,
    expected: Decimal,
    mentions: Sequence[tuple[int, NumberMention]],
    used: set[int],
    tolerances: Tolerances,
) -> bool:
    """Whether an unmatched mention with the same explicit unit nearly restates the value."""
    unit = _EXPLICIT_UNIT.get(fact.kind)
    if unit is None or expected == 0:
        return False
    reader = _READERS[fact.kind]
    for position, mention in mentions:
        if position in used or mention.unit != unit:
            continue
        value = reader(mention)
        if value is None or _within(fact.kind, value, expected, tolerances):
            continue
        if abs(value - expected) <= abs(expected) * CONFLICT_BAND:
            return True
    return False


def _maximum_matching(candidates: Mapping[int, Sequence[int]]) -> dict[int, int]:
    """Maximum bipartite matching of facts to mentions (Kuhn's algorithm), deterministic."""
    owner: dict[int, int] = {}

    def augment(fact: int, seen: set[int]) -> bool:
        for mention in candidates[fact]:
            if mention in seen:
                continue
            seen.add(mention)
            if mention not in owner or augment(owner[mention], seen):
                owner[mention] = fact
                return True
        return False

    for fact in sorted(candidates):
        augment(fact, set())
    return {fact: mention for mention, fact in owner.items()}


# --- run views from API-shaped responses -----------------------------------------------------


@dataclass(frozen=True)
class RunView:
    """What grading needs from one run. Holds response content in memory only; never serialized."""

    arm: str
    failure_mode: str
    answer_text: str | None = field(repr=False)
    # None: the response did not report query results; (): it reported that none ran.
    result_sets: tuple[tuple[tuple[Any, ...], ...], ...] | None = field(repr=False)
    # None: citation evidence not available; otherwise (cited anything, every citation valid).
    citations: tuple[bool, bool] | None
    # Arm B only: the displayed values of its structured report facts.
    structured_values: tuple[Decimal, ...] | None = field(repr=False, default=None)
    # The tables the user is shown, with their headers (arm A: the message's rows, headed by their
    # keys; arm B: the report's query tables, headed by their columns).
    tables: tuple[DisplayedTable, ...] = field(repr=False, default=())
    # The product's own structured signals that a step did not execute (fixed codes).
    not_executed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ComparisonError("Unknown arm.")
        if self.failure_mode not in FAILURE_MODES:
            raise ComparisonError("Unknown failure mode.")
        if any(signal not in NOT_EXECUTED_SIGNALS for signal in self.not_executed):
            raise ComparisonError("Unknown not-executed signal.")

    @property
    def visible(self) -> bool:
        """Whether the user was shown any answer at all (prose or a table)."""
        return self.answer_text is not None or any(table.rows for table in self.tables)


# A required query step did not run, by the product's own account: arm A's hybrid query fell back
# to documents; an arm B query task (SQL analysis or hypothesis test) failed, was refused, or was
# cancelled. A skipped task is not counted: skipping is how the graph records an untestable step.
QUERY_NOT_EXECUTED: Final = "query_not_executed"
NOT_EXECUTED_SIGNALS: Final = frozenset({QUERY_NOT_EXECUTED})
_ARM_B_QUERY_TASKS: Final = frozenset({"sql_analysis", "hypothesis_test"})
_ARM_B_NOT_EXECUTED: Final = frozenset({"failed", "refused", "cancelled"})


_ARM_A_INVALID_PROVENANCE = ("invalid_citation_count", "unverified_quote_count")


def observe_arm_a(message: Mapping[str, Any] | None) -> RunView:
    """Arm A: one assistant ``MessageResponse`` from ``POST .../messages``."""
    if message is None:
        return RunView("A", "no_response", None, None, None)
    if message.get("role") != "assistant":
        raise ComparisonError("Arm A expects an assistant message response.")
    status = message.get("status")
    route = message.get("route")
    answer = message.get("answer")
    if status == "pending_approval":
        mode = "pending_approval"
    elif status == "rejected":
        mode = "rejected"
    elif status != "complete":
        raise ComparisonError("Unknown arm A message status.")
    elif route == "unsupported":
        mode = "refused"
    elif route == "investigation":
        raise ComparisonError("Arm A must run with investigation routing off (D2).")
    else:
        mode = "none"
    text = answer if isinstance(answer, str) and answer.strip() else None
    result_sets: tuple[tuple[tuple[Any, ...], ...], ...] | None
    tables: tuple[DisplayedTable, ...] = ()
    if "rows" not in message:
        result_sets = None
    elif message["rows"] is None:
        result_sets = ()
    else:
        rows = message["rows"]
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ComparisonError("Arm A rows must be a list of objects.")
        result_sets = (tuple(tuple(row.values()) for row in rows),)
        if rows:
            columns = tuple(str(key) for key in rows[0])
            tables = (
                DisplayedTable(
                    columns, tuple(tuple(row.get(column) for column in columns) for row in rows)
                ),
            )
    citations: tuple[bool, bool] | None = None
    provenance = message.get("provenance")
    if isinstance(provenance, Mapping) and isinstance(message.get("sources"), list):
        invalid = sum(int(provenance.get(name) or 0) for name in _ARM_A_INVALID_PROVENANCE)
        citations = (len(message["sources"]) > 0, invalid == 0)
    not_executed: tuple[str, ...] = ()
    if isinstance(provenance, Mapping) and provenance.get("hybrid_fell_back_to_documents") is True:
        not_executed = (QUERY_NOT_EXECUTED,)
    return RunView(
        "A",
        mode,
        text,
        result_sets,
        citations,
        tables=tables,
        not_executed=not_executed,
    )


def not_routed_arm_b() -> RunView:
    """Arm B when production routing proposed no investigation: nothing ran, nothing to grade."""
    return RunView("B", "not_routed", None, None, None)


_ARM_B_TERMINAL = {
    "completed": "none",
    "completed_partial": "completed_partial",
    "budget_exhausted": "budget_exhausted",
    "plan_invalid": "plan_invalid",
    "plan_rejected": "plan_rejected",
    "cancelled": "cancelled",
    "failed": "failed",
    "interrupted": "interrupted",
    "expired": "expired",
}
_ARM_B_ACTIVE = {
    "planning",
    "awaiting_plan_approval",
    "queued",
    "running",
    "awaiting_sql_approval",
}


def observe_arm_b(
    investigation: Mapping[str, Any] | None, evidence: Mapping[str, Any] | None
) -> RunView:
    """Arm B: an ``InvestigationResponse`` and, when fetched, its evidence response."""
    if investigation is None:
        return RunView("B", "no_response", None, None, None)
    status = investigation.get("status")
    if status in _ARM_B_TERMINAL:
        mode = _ARM_B_TERMINAL[status]
    elif status in _ARM_B_ACTIVE:
        mode = "not_terminal"
    else:
        raise ComparisonError("Unknown arm B investigation status.")
    report = None
    if isinstance(evidence, Mapping) and isinstance(evidence.get("report"), Mapping):
        report = evidence["report"]
    elif isinstance(investigation.get("report"), Mapping):
        report = investigation["report"]
    text = _report_text(report) if report is not None else None
    result_sets = None
    citations = None
    if isinstance(evidence, Mapping) and evidence.get("evidence_state") == "available":
        items = evidence.get("evidence")
        if not isinstance(items, list):
            raise ComparisonError("Arm B evidence must be a list.")
        result_sets = _latest_query_rows(items)
        documents = [
            item for item in items if isinstance(item, Mapping) and item.get("kind") == "document"
        ]
        cited = bool(report and report.get("citations")) or any(
            item.get("sources") for item in documents
        )
        valid = all(
            item.get("citations_verified") is True and not item.get("invalid_citations")
            for item in documents
        )
        citations = (cited and bool(documents), valid)
    structured = _structured_values(report) if report is not None else None
    tables: tuple[DisplayedTable, ...] = ()
    not_executed: tuple[str, ...] = ()
    if report is not None:
        tables = tuple(
            DisplayedTable(
                tuple(str(column) for column in table.get("columns", [])),
                tuple(tuple(row) for row in table.get("rows", [])),
            )
            for table in report.get("query_tables", [])
            if isinstance(table, Mapping) and table.get("rows")
        )
        if any(
            isinstance(task, Mapping)
            and task.get("kind") in _ARM_B_QUERY_TASKS
            and task.get("status") in _ARM_B_NOT_EXECUTED
            for task in report.get("tasks", [])
        ):
            not_executed = (QUERY_NOT_EXECUTED,)
    return RunView(
        "B",
        mode,
        text,
        result_sets,
        citations,
        structured,
        tables=tables,
        not_executed=not_executed,
    )


def _report_text(report: Mapping[str, Any]) -> str | None:
    sentences = [
        sentence.get("text", "")
        for section in report.get("sections", [])
        if isinstance(section, Mapping)
        for sentence in section.get("sentences", [])
        if isinstance(sentence, Mapping)
    ]
    text = " ".join(sentence for sentence in sentences if isinstance(sentence, str)).strip()
    return text or None


def _latest_query_rows(items: Sequence[Any]) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    latest: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or item.get("kind") != "query":
            continue
        task = str(item.get("task_id", ""))
        current = latest.get(task)
        if current is None or int(item.get("attempt", 0)) >= int(current.get("attempt", 0)):
            latest[task] = item
    return tuple(
        tuple(tuple(row) for row in latest[task].get("rows", [])) for task in sorted(latest)
    )


def _structured_values(report: Mapping[str, Any]) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for fact in report.get("facts", []):
        if not isinstance(fact, Mapping):
            continue
        for side in ("baseline", "comparison"):
            entry = fact.get(side)
            if isinstance(entry, Mapping):
                number = _decimal(entry.get("display"))
                if number is not None:
                    values.append(number)
    return tuple(values)


# --- grading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetering:
    """Content-free resource figures for one run. ``None`` means not observed, never zero."""

    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    provider_cost_usd: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "provider_cost_usd": self.provider_cost_usd,
        }


def _dimension(outcome: str, reason: str) -> dict[str, str]:
    if outcome not in OUTCOMES or reason not in REASONS:
        raise ComparisonError("Unknown outcome or reason code.")
    return {"outcome": outcome, "reason": reason}


def grade_run(
    spec: CaseSpec,
    view: RunView,
    tolerances: Tolerances,
    *,
    repeat: int,
    metering: RunMetering | None = None,
) -> dict[str, Any]:
    """One content-free grade: an outcome and a reason code per dimension, plus counts."""
    if repeat < 1:
        raise ComparisonError("Repeats are numbered from 1.")
    text = view.answer_text
    dims: dict[str, dict[str, str]] = {}
    failure_mode = view.failure_mode
    answered = failure_mode == "none" and view.visible
    # The product reports that a query step did not run. That makes the answer partial only when
    # this case needs query results; a fallback in a capability the case does not need is harmless.
    if answered and spec.references and QUERY_NOT_EXECUTED in view.not_executed:
        answered = False
        failure_mode = "incomplete_execution"
        dims["task_completion"] = _dimension(FAIL, "partial_execution")
    else:
        dims["task_completion"] = _dimension(
            PASS if answered else FAIL, "answered" if answered else "not_answered"
        )

    # The user-visible answer is the prose and the displayed tables (D3 as amended in S1c).
    # Expected values include labels (a region or category name) as well as numbers.
    fact_outcomes = (
        grade_facts(spec.facts, text or "", tolerances, view.tables) if view.visible else []
    )
    if not spec.facts:
        dims["subquestion_completeness"] = _dimension(NOT_APPLICABLE, "no_requirements")
        dims["numeric_accuracy"] = _dimension(NOT_APPLICABLE, "no_requirements")
    elif not view.visible:
        dims["subquestion_completeness"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
        dims["numeric_accuracy"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    else:
        addressed = {o.group for o in fact_outcomes if o.stated}
        complete = all(group in addressed for group in spec.groups)
        dims["subquestion_completeness"] = _dimension(
            PASS if complete else FAIL, "all_addressed" if complete else "subquestion_unaddressed"
        )
        failing = [o for o in fact_outcomes if not o.stated]
        reason = "all_stated"
        for code in ("conflicting_values", "ambiguous_value", "value_not_stated"):
            if any(o.reason == code for o in failing):
                reason = code
                break
        dims["numeric_accuracy"] = _dimension(PASS if not failing else FAIL, reason)

    references_matched = 0
    if not spec.references:
        dims["sql_result_correctness"] = _dimension(NOT_APPLICABLE, "no_reference")
    elif view.result_sets is None:
        dims["sql_result_correctness"] = _dimension(NOT_OBSERVABLE, "query_results_not_reported")
    else:
        references_matched = sum(
            any(compare_result_sets(actual, reference).equivalent for actual in view.result_sets)
            for reference in spec.references
        )
        matched_all = references_matched == len(spec.references)
        dims["sql_result_correctness"] = _dimension(
            PASS if matched_all else FAIL,
            "all_references_matched" if matched_all else "reference_not_matched",
        )

    if not spec.requires_citation:
        dims["citation_coverage"] = _dimension(NOT_APPLICABLE, "citation_not_required")
    elif view.citations is None:
        dims["citation_coverage"] = _dimension(NOT_OBSERVABLE, "evidence_not_available")
    else:
        cited, valid = view.citations
        if not cited:
            dims["citation_coverage"] = _dimension(FAIL, "citations_missing")
        elif not valid:
            dims["citation_coverage"] = _dimension(FAIL, "citations_invalid")
        else:
            dims["citation_coverage"] = _dimension(PASS, "citations_valid")

    claim_text = _without_fixed_clauses(text) if text is not None else None
    if not spec.forbidden_phrases:
        dims["unsupported_claims"] = _dimension(NOT_APPLICABLE, "no_forbidden_claims_defined")
    elif claim_text is None:
        dims["unsupported_claims"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    elif causal_markers_in(claim_text, spec.forbidden_phrases):
        dims["unsupported_claims"] = _dimension(FAIL, "forbidden_claim_present")
    else:
        dims["unsupported_claims"] = _dimension(PASS, "no_forbidden_claim")

    if not spec.causal_markers:
        dims["causal_language"] = _dimension(NOT_APPLICABLE, "no_causal_markers_defined")
    elif claim_text is None:
        dims["causal_language"] = _dimension(NOT_OBSERVABLE, "no_answer_text")
    elif causal_markers_in(claim_text, spec.causal_markers):
        dims["causal_language"] = _dimension(FAIL, "causal_marker_present")
    else:
        dims["causal_language"] = _dimension(PASS, "no_causal_marker")

    dims["structured_fact_accuracy"] = _structured_dimension(spec, view, tolerances)

    return {
        "case_id": spec.case_id,
        "role": spec.role,
        "arm": view.arm,
        "repeat": repeat,
        "failure_mode": failure_mode,
        "dimensions": dims,
        "counts": {
            "expected_values": len(spec.facts),
            "stated_correctly": sum(o.stated for o in fact_outcomes),
            "stated_in_text_only": sum(o.source == "text" for o in fact_outcomes),
            "stated_in_table_only": sum(o.source == "table" for o in fact_outcomes),
            "stated_in_both": sum(o.source == "both" for o in fact_outcomes),
            "ambiguous": sum(o.reason == "ambiguous_value" for o in fact_outcomes),
            "conflicting": sum(o.reason == "conflicting_values" for o in fact_outcomes),
            "subquestions": len(spec.groups),
            "subquestions_addressed": len({o.group for o in fact_outcomes if o.stated}),
            "references": len(spec.references),
            "references_matched": references_matched,
        },
        "ungraded_expectations": list(spec.ungraded_expectations),
        "metering": (metering or RunMetering()).as_dict(),
    }


def _structured_dimension(spec: CaseSpec, view: RunView, tolerances: Tolerances) -> dict[str, str]:
    if view.arm == "A":
        return _dimension(NOT_APPLICABLE, "arm_a_has_no_structured_facts")
    rates = [fact for fact in spec.facts if fact.kind == "rate"]
    if not rates:
        return _dimension(NOT_APPLICABLE, "no_structured_rates")
    if view.structured_values is None:
        return _dimension(NOT_OBSERVABLE, "report_not_available")
    for fact in rates:
        expected = _expected_number(fact)
        if not any(
            _within("rate", value, expected, tolerances) for value in view.structured_values
        ):
            return _dimension(FAIL, "structured_fact_missing")
    return _dimension(PASS, "structured_facts_match")


def _without_fixed_clauses(text: str) -> str:
    for clause in NON_CAUSAL_FIXED_CLAUSES:
        text = re.sub(re.escape(clause), " ", text, flags=re.IGNORECASE)
    return text


# --- aggregation -----------------------------------------------------------------------------


def wilson_interval(
    successes: int, trials: int, z: float = WILSON_Z_95
) -> tuple[float, float] | None:
    """The Wilson score interval; identical to ``workflow_baseline.wilson_interval`` (tested).

    Duplicated so this module imports no application code (``workflow_baseline`` does).
    """
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


def dimension_block(outcomes: Iterable[str]) -> dict[str, Any]:
    """Counts of all four outcomes, the applicable denominator, the rate, and its interval."""
    counts = Counter(outcomes)
    unknown = set(counts) - set(OUTCOMES)
    if unknown:
        raise ComparisonError("Unknown outcome in aggregation.")
    passed, failed = counts[PASS], counts[FAIL]
    applicable = passed + failed
    interval = wilson_interval(passed, applicable)
    return {
        "runs": sum(counts.values()),
        "pass": passed,
        "fail": failed,
        "not_applicable": counts[NOT_APPLICABLE],
        "not_observable": counts[NOT_OBSERVABLE],
        "applicable": applicable,
        "pass_rate": round(passed / applicable, _RATE_DIGITS) if applicable else None,
        "wilson_95": list(interval) if interval else None,
    }


def _metering_block(values: Sequence[float | int | None]) -> dict[str, Any]:
    observed = [float(value) for value in values if value is not None]
    return {
        "runs": len(values),
        "observed": len(observed),
        "total": round(sum(observed), _RATE_DIGITS) if observed else None,
        "median": round(statistics.median(observed), _RATE_DIGITS) if observed else None,
    }


_PROVENANCE_REQUIRED = (
    "arm_a_investigation_routing",
    "arm_b_investigation_routing",
    "arm_a_model",
    "arm_b_model",
)


def _checked_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    missing = [key for key in _PROVENANCE_REQUIRED if key not in provenance]
    if missing:
        raise ComparisonError("Provenance must record both arms' routing and models (D2).")
    if provenance["arm_a_investigation_routing"] is not False:
        raise ComparisonError("Arm A must run with investigation routing off (D2).")
    if provenance["arm_b_investigation_routing"] is not True:
        raise ComparisonError("Arm B must run with investigation routing on (D2).")
    checked: dict[str, Any] = {}
    for key, value in provenance.items():
        if not _SAFE_TOKEN.match(str(key)):
            raise ComparisonError("Provenance keys must be safe identifiers.")
        if isinstance(value, bool | int) or value is None:
            checked[key] = value
        elif isinstance(value, str) and _SAFE_PROVENANCE.match(value):
            checked[key] = value
        else:
            raise ComparisonError("Provenance values must be short, plain identifiers.")
    return checked


def build_document(
    grades: Sequence[Mapping[str, Any]],
    *,
    evidence_kind: str,
    provenance: Mapping[str, Any],
    planned_repeats: int,
    sensitive: Iterable[str] = (),
    routing: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The comparison document: per-arm, per-dimension outcomes with denominators and intervals.

    ``routing`` optionally records, per case and arm, the route production routing chose and the
    gate's reason code: identifiers only, checked like every other value.

    Raises :class:`ComparisonRefused` for evidence this stage must not label (measured live), and
    :class:`ComparisonError` when a grade is malformed or the document would carry content.
    """
    if evidence_kind == MEASURED_LIVE:
        raise ComparisonRefused("S1a cannot produce measured live evidence (S4 only).")
    if evidence_kind not in EVIDENCE_STATEMENTS:
        raise ComparisonError("Unknown evidence kind.")
    if planned_repeats < 1:
        raise ComparisonError("At least one repeat is planned.")
    checked = _checked_provenance(provenance)
    for grade in grades:
        _check_grade(grade)

    def section(role: str, only: set[str] | None = None) -> dict[str, Any]:
        selected = [
            grade
            for grade in grades
            if grade["role"] == role and (only is None or grade["case_id"] in only)
        ]
        cases = sorted({grade["case_id"] for grade in selected})
        arms: dict[str, Any] = {}
        for arm in ARMS:
            arm_grades = [grade for grade in selected if grade["arm"] == arm]
            dimensions = COMPARED_DIMENSIONS if arm == "A" else DIMENSIONS
            arms[arm] = {
                "label": ARM_LABELS[arm],
                "runs": len(arm_grades),
                "dimensions": {
                    name: dimension_block(g["dimensions"][name]["outcome"] for g in arm_grades)
                    for name in dimensions
                },
                "failure_modes": dict(
                    sorted(Counter(g["failure_mode"] for g in arm_grades).items())
                ),
                "metering": {
                    name: _metering_block([g["metering"][name] for g in arm_grades])
                    for name in RunMetering().as_dict()
                },
                "per_case": {
                    case: {
                        name: dimension_block(
                            g["dimensions"][name]["outcome"]
                            for g in arm_grades
                            if g["case_id"] == case
                        )
                        for name in dimensions
                    }
                    for case in cases
                },
            }
        return {"cases": cases, "arms": arms}

    ungraded = sorted(
        {
            (grade["case_id"], name)
            for grade in grades
            for name in grade.get("ungraded_expectations", [])
        }
    )
    reach = routing_reach(grades)
    consistent = set(reach["consistently_routed_cases"])
    document = {
        "schema": COMPARISON_SCHEMA,
        "headline": CONFOUND_HEADLINE,
        "evidence": {
            "kind": evidence_kind,
            "statement": EVIDENCE_STATEMENTS[evidence_kind],
            "measured_live": False,
            "repeats_independent": False,
        },
        "definitions": {
            "outcomes": list(OUTCOMES),
            "rate": RATE_DEFINITION,
            "interval": "Wilson score interval, 95%.",
            "completeness": COMPLETENESS_DEFINITION,
            "numeric": NUMERIC_DEFINITION,
            "secondary": "structured_fact_accuracy is arm B only and is never compared with arm A.",
            "judge": JUDGE_NOTE,
            "metering": METERING_DEFINITION,
            "routing": ROUTING_DEFINITION,
            "visible_answer": VISIBLE_ANSWER_DEFINITION,
            "completion": COMPLETION_DEFINITION,
            "primary": PRIMARY_DEFINITION,
            "reach": REACH_DEFINITION,
            "conditional": CONDITIONAL_DEFINITION,
        },
        "provenance": checked,
        "planned_repeats": planned_repeats,
        "routing": [_checked_routing(entry) for entry in routing],
        "primary_result": "compared",
        "routing_reach": reach,
        # End to end, over every approved compared case: the primary result.
        "compared": section("compared"),
        # Restricted to the cases arm B reached in every run. Never pooled with the primary result.
        "conditional": {
            "note": CONDITIONAL_DEFINITION,
            "selected_by": "routing_gate",
            "representative": False,
            **section("compared", consistent),
        },
        "safety_probes": {
            "note": "Safety probes are reported, not compared.",
            **section("safety_probe"),
        },
        "ungraded_expectations": [
            {"case_id": case_id, "expectation": name} for case_id, name in ungraded
        ],
    }
    problems = content_problems(document, sensitive)
    if problems:
        raise ComparisonError("The comparison document would carry content: " + "; ".join(problems))
    return document


def routing_reach(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How often arm B's investigation workflow ran, per compared case and overall.

    A run is reached unless production routing declined it (``not_routed``) or no response came
    back (``no_response``). A case is consistently routed only when every arm B run reached it.
    """
    per_case: dict[str, dict[str, int]] = {}
    for grade in grades:
        if grade["arm"] != "B" or grade["role"] != "compared":
            continue
        entry = per_case.setdefault(grade["case_id"], {"reached": 0, "runs": 0})
        entry["runs"] += 1
        entry["reached"] += grade["failure_mode"] not in {"not_routed", "no_response"}
    reached = sum(entry["reached"] for entry in per_case.values())
    runs = sum(entry["runs"] for entry in per_case.values())
    return {
        "arm": "B",
        "per_case": dict(sorted(per_case.items())),
        "reached": reached,
        "runs": runs,
        "cases_reached": sum(entry["reached"] > 0 for entry in per_case.values()),
        "cases": len(per_case),
        "consistently_routed_cases": sorted(
            case for case, entry in per_case.items() if entry["reached"] == entry["runs"] > 0
        ),
    }


_ROUTING_KEYS = frozenset({"case_id", "arm", "route", "gate_reason", "runs"})


def _checked_routing(entry: Mapping[str, Any]) -> dict[str, Any]:
    if set(entry) != _ROUTING_KEYS or entry["arm"] not in ARMS:
        raise ComparisonError("A routing entry names a case, an arm, a route, a reason, and runs.")
    if not isinstance(entry["runs"], int) or isinstance(entry["runs"], bool) or entry["runs"] < 1:
        raise ComparisonError("A routing entry counts at least one run.")
    for key in ("case_id", "route", "gate_reason"):
        if not isinstance(entry[key], str) or not _SAFE_TOKEN.match(entry[key]):
            raise ComparisonError("Routing values must be safe identifiers.")
    return {key: entry[key] for key in ("case_id", "arm", "route", "gate_reason", "runs")}


def _check_grade(grade: Mapping[str, Any]) -> None:
    if grade.get("arm") not in ARMS or grade.get("role") not in ROLES:
        raise ComparisonError("A grade has an unknown arm or role.")
    if grade.get("failure_mode") not in FAILURE_MODES:
        raise ComparisonError("A grade has an unknown failure mode.")
    dims = grade.get("dimensions", {})
    if set(dims) != set(DIMENSIONS):
        raise ComparisonError("A grade must carry every dimension.")
    for entry in dims.values():
        if entry.get("outcome") not in OUTCOMES or entry.get("reason") not in REASONS:
            raise ComparisonError("A grade carries an unknown outcome or reason.")


# Prose this module writes itself; every other string in a document must be a safe token.
_FIXED_PROSE = frozenset(
    {
        CONFOUND_HEADLINE,
        JUDGE_NOTE,
        COMPLETENESS_DEFINITION,
        NUMERIC_DEFINITION,
        *EVIDENCE_STATEMENTS.values(),
        *ARM_LABELS.values(),
        RATE_DEFINITION,
        METERING_DEFINITION,
        ROUTING_DEFINITION,
        VISIBLE_ANSWER_DEFINITION,
        COMPLETION_DEFINITION,
        PRIMARY_DEFINITION,
        REACH_DEFINITION,
        CONDITIONAL_DEFINITION,
        "Wilson score interval, 95%.",
        "structured_fact_accuracy is arm B only and is never compared with arm A.",
        "Safety probes are reported, not compared.",
    }
)


def content_problems(document: Any, sensitive: Iterable[str] = ()) -> list[str]:
    """Why ``document`` is not content-free; empty when it is.

    Every string (keys included) must be this module's fixed prose, a safe identifier token, or a
    checked provenance value; none may contain a forbidden marker or any ``sensitive`` string the
    caller names (questions, answers, oracle values, canaries). Sensitive strings shorter than
    :data:`MIN_SENSITIVE_LENGTH` are refused rather than searched: a value like ``2`` appears in
    identifiers by chance, and grades hold no copied values by construction (tested).
    """
    sensitive = list(sensitive)
    if any(len(s) < MIN_SENSITIVE_LENGTH for s in sensitive):
        raise ComparisonError("Sensitive strings must be distinctive (at least 4 characters).")
    needles = [*FORBIDDEN_IN_DOCUMENT, *sensitive]
    problems: list[str] = []

    def visit(node: Any, path: str, provenance: bool) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                visit_string(str(key), f"{path}.<key>", provenance=False)
                visit(value, f"{path}.{key}", provenance or key == "provenance")
        elif isinstance(node, list | tuple):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]", provenance)
        elif isinstance(node, str):
            visit_string(node, path, provenance)
        elif node is not None and not isinstance(node, bool | int | float):
            problems.append(f"{path}: unsupported value type")

    def visit_string(text: str, path: str, provenance: bool) -> None:
        if not text:
            return
        allowed = text in _FIXED_PROSE or _SAFE_TOKEN.match(text) is not None
        if provenance and _SAFE_PROVENANCE.match(text):
            allowed = True
        if not allowed:
            problems.append(f"{path}: free text")
        lowered = text.lower()
        for needle in needles:
            if needle.lower() in lowered:
                problems.append(f"{path}: forbidden content")
                break

    visit(document, "$", provenance=False)
    return problems


def render_summary(document: Mapping[str, Any]) -> str:
    """A Markdown summary built only from the (content-free) document."""
    lines = [
        "# EVAL-2 workflow comparison",
        "",
        f"**{document['headline']}**",
        "",
        f"Evidence: **{document['evidence']['kind']}**. {document['evidence']['statement']}",
        "",
        f"Planned repeats per case per arm: {document['planned_repeats']}.",
        "",
        f"_{document['definitions']['primary']}_",
        "",
    ]
    sections: list[tuple[str, str, str | None]] = [
        ("Primary result: end to end, all compared cases", "compared", None),
    ]
    if "conditional" in document:
        sections.append(
            (
                "Conditional comparison (selected by the routing gate; not representative)",
                "conditional",
                document["definitions"]["conditional"],
            )
        )
    sections.append(("Safety probes (not compared)", "safety_probes", None))
    reach = document.get("routing_reach")
    for title, key, note in sections:
        block = document[key]
        lines += [f"## {title}", ""]
        if note:
            lines += [f"**{note}**", ""]
        lines += [f"Cases: {', '.join(block['cases']) or 'none'}", ""]
        lines += _dimension_table(block)
        if block["cases"]:
            lines += ["### Per case", "", *_per_case_table(block)]
        if key == "compared" and reach:
            lines += _reach_table(reach, document["definitions"]["reach"])
    if document.get("routing"):
        lines += [
            "## Routing",
            "",
            document["definitions"]["routing"],
            "",
            "| Case | Arm | Route | Gate reason | Runs |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {entry['case_id']} | {entry['arm']} | {entry['route']} | "
            f"{entry['gate_reason']} | {entry['runs']} |"
            for entry in document["routing"]
        ]
        lines.append("")
    lines += [
        f"_{document['definitions']['metering']}_",
        "",
        f"_{document['definitions']['judge']}_",
        "",
    ]
    return "\n".join(lines)


def _rate_cells(stats: Mapping[str, Any]) -> tuple[str, str]:
    rate = "n/a" if stats["pass_rate"] is None else f"{stats['pass_rate']:.3f}"
    interval = (
        "n/a"
        if stats["wilson_95"] is None
        else f"{stats['wilson_95'][0]:.3f}-{stats['wilson_95'][1]:.3f}"
    )
    return rate, interval


def _dimension_table(block: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Dimension | Arm | Pass | Fail | N/A | Not observable | Denominator | Rate "
        "| Wilson 95% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in DIMENSIONS:
        for arm in ARMS:
            stats = block["arms"][arm]["dimensions"].get(name)
            if stats is None:
                continue
            rate, interval = _rate_cells(stats)
            lines.append(
                f"| {name} | {arm} | {stats['pass']} | {stats['fail']} | "
                f"{stats['not_applicable']} | {stats['not_observable']} | "
                f"{stats['applicable']} | {rate} | {interval} |"
            )
    lines.append("")
    return lines


def _per_case_table(block: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Case | Dimension | Arm | Pass | Fail | N/A | Not observable | Denominator |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in block["cases"]:
        for name in DIMENSIONS:
            for arm in ARMS:
                stats = block["arms"][arm]["per_case"].get(case, {}).get(name)
                if stats is None:
                    continue
                lines.append(
                    f"| {case} | {name} | {arm} | {stats['pass']} | {stats['fail']} | "
                    f"{stats['not_applicable']} | {stats['not_observable']} | "
                    f"{stats['applicable']} |"
                )
    lines.append("")
    return lines


def _reach_table(reach: Mapping[str, Any], definition: str) -> list[str]:
    lines = [
        "### Routing reach (arm B)",
        "",
        definition,
        "",
        "| Case | Reached | Runs |",
        "|---|---|---|",
    ]
    lines += [
        f"| {case} | {entry['reached']} | {entry['runs']} |"
        for case, entry in reach["per_case"].items()
    ]
    lines += [
        f"| **all compared cases** | **{reach['reached']}** | **{reach['runs']}** |",
        "",
        f"Cases reached: {reach['cases_reached']} of {reach['cases']}. Consistently routed: "
        f"{', '.join(reach['consistently_routed_cases']) or 'none'}.",
        "",
    ]
    return lines


# --- helpers ---------------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None
