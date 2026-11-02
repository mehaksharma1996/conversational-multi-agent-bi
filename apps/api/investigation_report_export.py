"""Render a digest-checked verified report as an export file (UI-6c, #156).

Everything is rendered from the ``VerifiedReport``. Nothing is computed, and no SQL or model text
is added. Report values can hold untrusted strings (filenames, cell values, schema labels), so every
one goes through ``_text``: a rendered file cannot carry a link, an image, HTML, or a table break
from its data, and a cell cannot start a spreadsheet formula.
"""

from __future__ import annotations

import json
import re

from packages.investigation.interim_report import LIMITATION_TEMPLATES
from packages.investigation.report import NOTE_TEMPLATES, VerifiedReport

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")
_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#|~!&])")
_SCHEME = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,31}):")
_WWW = re.compile(r"(?i)\bwww\.")
_FORMULA_PREFIXES = ("=", "+", "-", "@")

_SECTION_TITLES = {
    "executive_summary": "Executive summary",
    "findings": "Findings",
    "hypotheses": "Hypotheses",
    "critique": "Critique",
    "limitations": "Limitations",
    "recommendations": "Recommendations",
}


def _text(value: object) -> str:
    """One line of inert Markdown: no control characters, markup, link, or autolink."""
    line = _CONTROL.sub(" ", str(value)).strip()
    line = _SPECIAL.sub(r"\\\1", line)
    line = _SCHEME.sub(r"\1\\:", line)
    return _WWW.sub(lambda match: match.group(0)[:-1] + "\\.", line)


def _cell(value: object) -> str:
    """A table cell. A string that a spreadsheet would run as a formula is escaped."""
    if value is None:
        return "null"
    if not isinstance(value, str):
        return _text(str(value).lower() if isinstance(value, bool) else value)
    rendered = _text(value)
    return f"\\{rendered}" if rendered.startswith(_FORMULA_PREFIXES) else rendered


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "|" + " --- |" * len(headers),
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _limitation_text(code: str) -> str:
    for template_code, wording in LIMITATION_TEMPLATES.items():
        if template_code.value == code:
            return wording
    return NOTE_TEMPLATES.get(code, code)


def render_markdown(investigation_id: str, content_sha256: str, report: VerifiedReport) -> str:
    """The report as Markdown: its label and state, narrative, measurements, and evidence rows."""
    lines = [
        "# Verified investigation report",
        "",
        f"- Investigation: {_text(investigation_id)}",
        f"- Label: {_text(report.label)}",
        f"- Status: {_text(report.investigation_status)}",
        f"- Availability: {_text(report.availability)}",
        f"- Narrative: {_text(report.mode)}",
        f"- Content SHA-256: {_text(content_sha256)}",
        "",
    ]
    for section in report.sections:
        if not section.sentences:
            continue
        lines += [f"## {_SECTION_TITLES[section.name]}", ""]
        lines += [f"- {_text(sentence.text)}" for sentence in section.sentences]
        lines.append("")
    if report.facts:
        lines += ["## Measurements", ""]
        lines += _table(
            ["Fact", "Hypothesis", "Metric", "Baseline", "Comparison", "Difference", "Evidence"],
            [
                [
                    _text(fact.id),
                    _text(fact.hypothesis_id),
                    _text(fact.metric_label),
                    f"{_text(fact.baseline_label)} {_text(fact.baseline.display)}",
                    f"{_text(fact.comparison_label)} {_text(fact.comparison.display)}",
                    _text(fact.difference),
                    f"{_text(fact.derived_evidence_id)}, {_text(fact.query_evidence_id)}",
                ]
                for fact in report.facts
            ],
        )
        lines.append("")
    if report.hypotheses:
        lines += ["## Hypothesis evidence", ""]
        lines += _table(
            ["Hypothesis", "Status", "Verdict", "Reasons", "Evidence"],
            [
                [
                    _text(row.hypothesis_id),
                    _text(row.status),
                    _text(row.verdict or "not evaluated"),
                    _text(", ".join(row.reasons) or "none"),
                    _text(", ".join(row.evidence_ids) or "none"),
                ]
                for row in report.hypotheses
            ],
        )
        lines.append("")
    for table in report.query_tables:
        lines += [f"## Query evidence {_text(table.evidence_id)} (task {_text(table.task_id)})", ""]
        lines += _table(
            [_cell(column) for column in table.columns],
            [[_cell(cell) for cell in row] for row in table.rows],
        )
        lines += ["", f"Rows shown: {len(table.rows)} of {table.source_row_count}."]
        if table.source_truncated:
            lines.append("The query reached its row limit; rows may be missing.")
        lines.append("")
    if report.citations:
        lines += ["## Document citations", ""]
        for citation in report.citations:
            page = "page not recorded" if citation.page_number is None else citation.page_number
            lines.append(
                f"- {_text(citation.filename)}, {_text(page)} "
                f"(evidence {_text(citation.evidence_id)}, task {_text(citation.task_id)})"
            )
        lines.append("")
    stated = [
        *(
            f"{_text(item.task_id or 'run')}: {_text(_limitation_text(item.code.value))}"
            for item in report.limitations
        ),
        *(_text(_limitation_text(item.code)) for item in report.notes),
    ]
    if stated:
        lines += ["## Stated limitations and notes", "", *(f"- {item}" for item in stated), ""]
    return "\n".join(lines)


def render_json(investigation_id: str, content_sha256: str, report: VerifiedReport) -> str:
    """The report as ASCII JSON: inert data whatever its values, with a stable wrapper."""
    return json.dumps(
        {
            "format": "investigation-report-export/v1",
            "investigation_id": investigation_id,
            "content_sha256": content_sha256,
            "report": report.model_dump(mode="json"),
        },
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )


__all__ = ["render_json", "render_markdown"]
