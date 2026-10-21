"""The versioned, deterministic prompt for the INV-8 report narrative call (#141, V3/V7/V10).

Rendered only by the tool gateway, from the deterministic report body (``ReportBody``) of the
validated state view and its own verified payloads; no caller supplies text. Its contents:
- the facts as structured fields (IDs, labels, displayed values, units, directions);
- hypothesis, task, finding, limitation, note, and recommendation IDs with their fixed codes.

It contains no rows, SQL, document text or answers, filenames, hypothesis statements, scope
identifiers, authority, budgets, prices, or evaluator data. The model may only return typed claims
that reference these IDs (``NarrativeDraft``); the report's text is rendered from reviewed
templates and verified by ``verify_report``.

Every instruction, marker, schema line, and layout rule lives in this module, so the prompt
registry's module fingerprint covers its whole construction (V10). The values it lists are data.
"""

from __future__ import annotations

import json

from packages.investigation.report import ReportBody

NARRATIVE_PROMPT_VERSION = "inv8-narrative-prompt-v1"
NARRATIVE_MARKER = "INVESTIGATION REPORT NARRATIVE"

_INSTRUCTIONS = (
    "Organise the verified results of a finished investigation into report sections.",
    "You cannot write sentences. Choose and order typed claims; each claim is rendered from "
    "reviewed wording and checked against the evidence.",
    "Use only the IDs and values listed below. Do not invent facts, citations, verdicts, "
    "limitations, or recommendations.",
    "Observational data shows association only: every claim's relationship is association.",
    "State every fact, every evaluated hypothesis's verdict, every listed finding, every "
    "limitation and note, and every recommendation (or no_recommendation when none is listed).",
    "Sections, in order: executive_summary, findings, hypotheses, critique, limitations, "
    "recommendations. The executive summary has one scope claim and the "
    "causation_not_established limitation.",
)
_SCHEMA = (
    'Return only JSON: {"sections":[{"name":"<section>","claims":[<claim>, ...]}]}. Claims:',
    '{"type":"scope","status":"<status>","hypotheses":<n>,"tested":<n>,"facts":<n>}',
    '{"type":"fact","fact_id":"<fact>","direction":"increase|decrease|no_change",'
    '"relationship":"association","evidence_ids":[]}',
    '{"type":"comparison","fact_ids":["<fact>","<fact>"],"relation":"larger|smaller|equal"} '
    "(magnitude of the two differences; same unit only)",
    '{"type":"verdict","hypothesis_id":"<hypothesis>","verdict":"<verdict>",'
    '"relationship":"association","evidence_ids":[]}',
    '{"type":"finding","round":<round>,"index":<index>,"code":"<finding code>"}',
    '{"type":"task","task_id":"<task>","status":"<task status>"}',
    '{"type":"limitation","code":"<code>","task_id":null,"hypothesis_id":null}',
    '{"type":"recommendation","recommendation_id":"<recommendation>","rule_id":"<rule>"}',
    '{"type":"no_recommendation"}',
    "Allowed per section: executive_summary any but finding and task; findings fact and "
    "comparison; hypotheses verdict; critique finding; limitations limitation and task; "
    "recommendations recommendation and no_recommendation.",
)


def _line(**fields: object) -> str:
    """One listed item as compact JSON: values are data and are quoted, never interpreted."""
    return "- " + json.dumps(fields, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def render_narrative_prompt(body: ReportBody) -> str:
    tested = sum(row.status == "tested" for row in body.hypotheses)
    lines = [f"{NARRATIVE_MARKER} ({NARRATIVE_PROMPT_VERSION})", *_INSTRUCTIONS, *_SCHEMA]
    lines.append(
        "Scope: "
        + json.dumps(
            {
                "status": body.status,
                "hypotheses": len(body.hypotheses),
                "tested": tested,
                "facts": len(body.facts),
            },
            separators=(",", ":"),
        )
    )
    lines.append("Facts:")
    for fact in body.facts:
        lines.append(
            _line(
                fact_id=fact.id,
                hypothesis_id=fact.hypothesis_id,
                metric=fact.metric_label,
                filters={item.field: item.value for item in fact.filters},
                standardized_by=fact.standardized_by.field if fact.standardized_by else None,
                unit=fact.unit,
                baseline=[fact.baseline_label, fact.baseline.display],
                comparison=[fact.comparison_label, fact.comparison.display],
                difference=fact.difference,
                direction=fact.direction,
            )
        )
    lines.append("Hypotheses:")
    for row in body.hypotheses:
        lines.append(_line(hypothesis_id=row.hypothesis_id, status=row.status, verdict=row.verdict))
    lines.append("Tasks:")
    for task in body.tasks:
        lines.append(_line(task_id=task.task_id, status=task.status))
    lines.append("Findings:")
    for record in body.critiques:
        for index, finding in enumerate(record.findings):
            lines.append(
                _line(
                    round=record.round,
                    index=index,
                    code=finding.code,
                    hypothesis_id=finding.hypothesis_id,
                )
            )
    lines.append("Limitations and notes:")
    for item in body.limitations:
        lines.append(_line(code=item.code.value, task_id=item.task_id))
    for note in body.notes(fallback=False):
        lines.append(_line(code=note.code, hypothesis_id=note.hypothesis_id))
    lines.append("Recommendations:")
    for recommendation in body.recommendations:
        lines.append(
            _line(
                recommendation_id=recommendation.id,
                rule_id=recommendation.rule_id,
                hypothesis_id=recommendation.hypothesis_id,
            )
        )
    return "\n".join(lines)


__all__ = ["NARRATIVE_MARKER", "NARRATIVE_PROMPT_VERSION", "render_narrative_prompt"]
