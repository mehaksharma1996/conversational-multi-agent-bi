"""Typed, process-local evidence payloads and their provenance check (ADR 0024, issue #138 INV-5).

State keeps only ``EvidenceRecord`` metadata (``packages/investigation/state.py``). The payload
behind a record (query rows, document sources, the model's answer) stays in the process that ran
the tool and is never checkpointed. These contracts give that payload one strict, immutable,
JSON-native shape so a later consumer (the interim report, INV-5 S5) never reads a raw DataFrame or
a provider object:

- ``QueryEvidencePayload``: column names and rows of JSON scalars, bounded by the executor's row
  limit, with an explicit ``truncated`` flag. It never carries SQL: the SQL stays in the gateway.
- ``DocumentEvidencePayload``: source locations, citations, evidence statuses, and the answer text
  wrapped as ``UntrustedModelText``. The answer is model output: it is data to show, never an
  instruction, a task, or a finding on its own.
- ``DerivedEvidencePayload`` (INV-6 decision D4): one fixed operation, the difference of two exact
  ratios (``rate_difference`` or ``mean_difference``), over the counts of one query evidence of the
  same task. Only the gateway's owner-only ``derive_evidence`` produces it, after recomputing it
  from the payload it holds; no model-written number can become one. INV-7 adds
  ``standardized_rate_difference``: a stratified follow-up's comparison-period band rates,
  weighted by the baseline's band populations (direct standardization), against the crude baseline
  rate; every band's counts travel with the payload, so the value always recomputes.

The gateway issues payloads alongside their records (INV-5 S2). ``verify_payload`` checks a payload
against its record before anything uses it; a mismatch excludes the payload. Errors and mismatch
codes are content-free.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from packages.investigation.contracts import TaskId
from packages.investigation.state import (
    MAX_DERIVED_SOURCES,
    MAX_EVIDENCE_STATUSES,
    MAX_SOURCES,
    DerivedOperationName,
    DerivedProvenance,
    DocumentProvenance,
    EvidenceRecord,
    EvidenceStatusName,
    GatewayEvidenceId,
    QueryProvenance,
    Score,
    SourceLocation,
)

PAYLOAD_SCHEMA_VERSION: Literal[1] = 1

# Mirrors ``src.storage.query_executor.execute_read_query``'s default ``max_rows`` (pinned by
# a test; this package cannot import ``src``).
MAX_QUERY_ROWS = 500
MAX_QUERY_COLUMNS = 64
MAX_COLUMN_NAME_CHARS = 128
MAX_CELL_CHARS = 4_096
MAX_CITATIONS = 32
MAX_ANSWER_CHARS = 8_000
MAX_FILENAME_CHARS = 255
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

Attempt = Annotated[int, Strict(), Field(ge=1, le=2)]
Count = Annotated[int, Strict(), Field(ge=0)]
SourceNumber = Annotated[int, Strict(), Field(ge=1)]
ColumnName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_COLUMN_NAME_CHARS)]
# A display name: no control characters, so it cannot smuggle terminal or markup control codes.
Filename = Annotated[
    str, StringConstraints(pattern=rf"^[^\x00-\x1f\x7f]{{1,{MAX_FILENAME_CHARS}}}$")
]
Cell = (
    None
    | Annotated[bool, Strict()]
    | Annotated[int, Strict(), Field(ge=INT64_MIN, le=INT64_MAX)]
    | Annotated[float, Strict(), Field(allow_inf_nan=False)]
    | Annotated[str, Strict(), StringConstraints(max_length=MAX_CELL_CHARS)]
)
Row = tuple[Cell, ...]

PayloadMismatch = Literal[
    "kind_mismatch",
    "id_mismatch",
    "task_mismatch",
    "attempt_mismatch",
    "shape_mismatch",
    "source_mismatch",
    "citation_mismatch",
    "status_mismatch",
]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _exact_cell(value: object) -> bool:
    # Exact types: a numpy scalar, ``Decimal``, enum member, or ``str`` subclass is not a cell.
    # Bounds are checked here too: an out-of-range ``int`` must not fall through to ``float``.
    if value is None or type(value) in (bool, str):
        return True
    if type(value) is int:
        return INT64_MIN <= value <= INT64_MAX
    return type(value) is float and math.isfinite(value)


def exact_rows(value: object) -> object:
    """Refuse rows unless every cell is exactly a JSON scalar (a ``mode="before"`` check)."""
    if not isinstance(value, list | tuple) or type(value) not in (list, tuple):
        raise ValueError("rows must be a sequence of rows")
    for row in value:
        if type(row) not in (list, tuple) or not all(_exact_cell(cell) for cell in row):
            raise ValueError("every cell must be a JSON scalar")
    return value


class QueryEvidencePayload(_Payload):
    """Rows behind query evidence: JSON scalars only, never the SQL that produced them.

    ``row_limit`` is the executor's limit for the call. ``truncated`` is true exactly when the
    result reached that limit, so more rows may exist; a short result is complete.
    """

    schema_version: Literal[1] = PAYLOAD_SCHEMA_VERSION
    kind: Literal["query"] = "query"
    evidence_id: GatewayEvidenceId
    task_id: TaskId
    attempt: Attempt
    columns: tuple[ColumnName, ...] = Field(min_length=1, max_length=MAX_QUERY_COLUMNS)
    rows: tuple[Row, ...] = Field(default=(), max_length=MAX_QUERY_ROWS)
    row_limit: Annotated[int, Strict(), Field(ge=1, le=MAX_QUERY_ROWS)] = MAX_QUERY_ROWS
    truncated: Annotated[bool, Strict()]

    @field_validator("rows", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return exact_rows(value)

    @model_validator(mode="after")
    def _shape(self) -> QueryEvidencePayload:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("column names must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("every row must have one cell per column")
        if len(self.rows) > self.row_limit:
            raise ValueError("rows exceed the row limit")
        if self.truncated != (len(self.rows) == self.row_limit):
            raise ValueError("truncated is true exactly when the row limit was reached")
        return self


class DocumentSource(_Payload):
    """One retrieved passage's location. The passage text itself is never part of a payload."""

    number: SourceNumber
    filename: Filename
    page_number: Count | None = None
    chunk_index: Count | None = None
    relevance_score: Score | None = None

    def location(self) -> SourceLocation:
        """The content-free location state records for this source (``DocumentProvenance``)."""
        return SourceLocation(
            number=self.number,
            page_number=self.page_number,
            chunk_index=self.chunk_index,
            relevance_score=self.relevance_score,
        )


class UntrustedModelText(_Payload):
    """Model-written prose. Shown as quoted data at most; never parsed, executed, or obeyed.

    The fixed ``origin`` and ``trusted`` fields make the provenance part of the type, so nothing
    can pass model text where a trusted instruction or finding is expected.
    """

    origin: Literal["model_output"] = "model_output"
    trusted: Literal[False] = False
    text: Annotated[str, Strict(), StringConstraints(max_length=MAX_ANSWER_CHARS)]


class DocumentEvidencePayload(_Payload):
    """Sources, citations, and the untrusted answer behind document evidence."""

    schema_version: Literal[1] = PAYLOAD_SCHEMA_VERSION
    kind: Literal["document"] = "document"
    evidence_id: GatewayEvidenceId
    task_id: TaskId
    attempt: Attempt
    sources: tuple[DocumentSource, ...] = Field(default=(), max_length=MAX_SOURCES)
    cited_source_numbers: tuple[SourceNumber, ...] = Field(default=(), max_length=MAX_CITATIONS)
    invalid_citations: tuple[Annotated[int, Strict()], ...] = Field(
        default=(), max_length=MAX_CITATIONS
    )
    evidence_statuses: tuple[EvidenceStatusName, ...] = Field(
        default=(), max_length=MAX_EVIDENCE_STATUSES
    )
    answer: UntrustedModelText

    @model_validator(mode="after")
    def _numbered(self) -> DocumentEvidencePayload:
        # The gateway numbers retrieved sources 1..n in retrieval order.
        if tuple(source.number for source in self.sources) != tuple(
            range(1, len(self.sources) + 1)
        ):
            raise ValueError("sources are numbered 1..n in order")
        return self


DerivedOperation = DerivedOperationName
Finite = Annotated[float, Strict(), Field(allow_inf_nan=False)]


class DerivedRatio(_Payload):
    """One exact ratio: an event count (or value sum) over a population (or row) count."""

    numerator: Finite
    denominator: Annotated[int, Strict(), Field(ge=1)]

    @property
    def exact(self) -> Fraction:
        return Fraction(self.numerator) / self.denominator


MAX_STRATA = 4
# Floats hold every integer up to 2**53 exactly; a reduced standardized fraction must fit.
_EXACT_FLOAT_INT = 2**53


class DerivedStratum(_Payload):
    """One band's baseline and comparison counts (events over population), as recorded."""

    label: Annotated[str, StringConstraints(pattern=r"^band_[0-3]$")]
    baseline: DerivedRatio
    comparison: DerivedRatio


def standardized_rate(strata: tuple[DerivedStratum, ...] | list[DerivedStratum]) -> Fraction:
    """Direct standardization with baseline weights (INV-7 decision C4), exactly.

    ``sum_b (baseline population_b / baseline population) * (comparison events_b / comparison
    population_b)``: the comparison period's within-band rates, weighted by the baseline's band
    composition. Descriptive only; it is not a causal adjustment.
    """
    total = sum(item.baseline.denominator for item in strata)
    if not strata or total < 1:
        raise ValueError("standardization needs a baseline population")
    return sum(
        (Fraction(item.baseline.denominator, total) * item.comparison.exact for item in strata),
        Fraction(0),
    )


def crude_baseline(strata: tuple[DerivedStratum, ...] | list[DerivedStratum]) -> DerivedRatio:
    """The baseline rate over every band together: summed events over summed population."""
    return DerivedRatio(
        numerator=float(sum(int(item.baseline.numerator) for item in strata)),
        denominator=sum(item.baseline.denominator for item in strata),
    )


def exact_ratio(value: Fraction) -> DerivedRatio:
    """``value`` as a reduced ratio whose numerator a float holds exactly; else ``ValueError``."""
    if value < 0 or value.numerator >= _EXACT_FLOAT_INT:
        raise ValueError("the standardized rate is not exactly representable")
    return DerivedRatio(numerator=float(value.numerator), denominator=value.denominator)


class DerivedEvidencePayload(_Payload):
    """``measure - reference`` for one approved test, recomputed by the gateway (D4).

    ``value`` must equal the exact difference of the two ratios; ``verify_payload`` also requires
    the record's operation and sources. The counts come from the source query payload, which the
    gateway checks before issuing; nothing here is taken from a model or the plan.

    ``standardized_rate_difference`` (INV-7 decision C4) also carries every band's counts in
    ``strata``. ``standardized`` names the side holding the standardized comparison rate (the
    reduced exact fraction recomputed from the strata); the other side is the crude baseline, the
    strata's summed baseline counts. Both are rechecked here, so a payload with altered counts,
    weights, or rates cannot be built.
    """

    schema_version: Literal[1] = PAYLOAD_SCHEMA_VERSION
    kind: Literal["derived"] = "derived"
    evidence_id: GatewayEvidenceId
    task_id: TaskId
    attempt: Attempt
    operation: DerivedOperation
    source_evidence_ids: tuple[GatewayEvidenceId, ...] = Field(
        min_length=1, max_length=MAX_DERIVED_SOURCES
    )
    measure: DerivedRatio
    reference: DerivedRatio
    value: Finite
    strata: tuple[DerivedStratum, ...] = Field(default=(), max_length=MAX_STRATA)
    standardized: Literal["measure", "reference"] | None = None

    @model_validator(mode="after")
    def _sources(self) -> DerivedEvidencePayload:
        if len(set(self.source_evidence_ids)) != len(self.source_evidence_ids):
            raise ValueError("source evidence must be unique")
        if self.evidence_id in self.source_evidence_ids:
            raise ValueError("derived evidence cannot cite itself")
        if self.operation == "rate_difference" and any(
            ratio.numerator != int(ratio.numerator) or ratio.numerator > ratio.denominator
            for ratio in (self.measure, self.reference)
        ):
            raise ValueError("a rate is a whole event count within its population")
        standardized = self.operation == "standardized_rate_difference"
        if standardized != bool(self.strata) or standardized != (self.standardized is not None):
            raise ValueError("only a standardized difference carries strata")
        if standardized:
            self._check_standardized()
        if self.value != float(self.measure.exact - self.reference.exact):
            raise ValueError("the value is the exact difference of the two ratios")
        return self

    def _check_standardized(self) -> None:
        strata = self.strata
        if len(strata) < 2 or tuple(item.label for item in strata) != tuple(
            f"band_{index}" for index in range(len(strata))
        ):
            raise ValueError("strata are the bands band_0..band_n, in order")
        for item in strata:
            for ratio in (item.baseline, item.comparison):
                if ratio.numerator != int(ratio.numerator) or ratio.numerator > ratio.denominator:
                    raise ValueError("a band rate is a whole event count within its population")
        crude = crude_baseline(strata)
        standard = exact_ratio(standardized_rate(strata))
        expected = (standard, crude) if self.standardized == "measure" else (crude, standard)
        if (self.measure, self.reference) != expected:
            raise ValueError("the sides are the standardized rate and the crude baseline")


EvidencePayload = Annotated[
    QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload,
    Field(discriminator="kind"),
]
_PAYLOAD_ADAPTER: TypeAdapter[
    QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload
] = TypeAdapter(EvidencePayload)
_PAYLOAD_TYPES = (QueryEvidencePayload, DocumentEvidencePayload, DerivedEvidencePayload)


def parse_evidence_payload(
    raw: object,
) -> QueryEvidencePayload | DocumentEvidencePayload | DerivedEvidencePayload:
    """Validate a JSON-native payload. Raises ``pydantic.ValidationError`` on any defect."""
    return _PAYLOAD_ADAPTER.validate_python(raw)


def canonical_json(model: BaseModel) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN. Equal models give equal text."""
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def verify_payload(record: EvidenceRecord, payload: object) -> PayloadMismatch | None:
    """``None`` when ``payload`` is exactly the evidence ``record`` describes; else a fixed code.

    Identity (ID, task, attempt, kind) must be equal. Query rows and columns must match the
    recorded counts. Document sources must match the recorded locations exactly, and citation
    counts and evidence statuses must be equal. A derived payload must name the record's operation
    and sources exactly (its value is checked by its own contract). Types are exact: subclasses are
    refused.
    """
    if type(record) is not EvidenceRecord or not isinstance(payload, _PAYLOAD_TYPES):
        return "kind_mismatch"
    if type(payload) not in _PAYLOAD_TYPES:  # a subclass is not a gateway-issued payload
        return "kind_mismatch"
    if payload.kind != record.kind:
        return "kind_mismatch"
    if payload.evidence_id != record.id:
        return "id_mismatch"
    if payload.task_id != record.task_id:
        return "task_mismatch"
    if payload.attempt != record.attempt:
        return "attempt_mismatch"
    provenance = record.provenance
    if type(payload) is QueryEvidencePayload and type(provenance) is QueryProvenance:
        if (len(payload.rows), len(payload.columns)) != (
            provenance.row_count,
            provenance.column_count,
        ):
            return "shape_mismatch"
        return None
    if type(payload) is DocumentEvidencePayload and type(provenance) is DocumentProvenance:
        if tuple(source.location() for source in payload.sources) != provenance.sources:
            return "source_mismatch"
        if (
            len(payload.cited_source_numbers) != provenance.citation_count
            or len(payload.invalid_citations) != provenance.invalid_citation_count
        ):
            return "citation_mismatch"
        if payload.evidence_statuses != provenance.evidence_statuses:
            return "status_mismatch"
        return None
    if type(payload) is DerivedEvidencePayload and type(provenance) is DerivedProvenance:
        if (
            payload.operation != provenance.operation
            or payload.source_evidence_ids != provenance.source_evidence_ids
        ):
            return "source_mismatch"
        return None
    return "kind_mismatch"


_UNVERIFIED_STATUSES = frozenset({"INVALID_REFERENCE", "QUOTE_MISMATCH"})


def document_citations_valid(payload: DocumentEvidencePayload) -> bool:
    """True when every citation resolves to a retrieved source and nothing was flagged invalid.

    Needs at least one citation, every cited number among the payload's sources, no invalid
    citation, and no ``INVALID_REFERENCE`` or ``QUOTE_MISMATCH`` status. Otherwise the evidence is
    reported only as the limitation ``document_evidence_unverified``.
    """
    if type(payload) is not DocumentEvidencePayload:
        return False
    numbers = {source.number for source in payload.sources}
    return (
        bool(payload.cited_source_numbers)
        and all(number in numbers for number in payload.cited_source_numbers)
        and not payload.invalid_citations
        and not _UNVERIFIED_STATUSES.intersection(payload.evidence_statuses)
    )
