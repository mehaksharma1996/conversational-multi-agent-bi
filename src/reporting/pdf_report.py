"""PDF export for business reports."""

from __future__ import annotations

from io import BytesIO

import plotly.io as pio
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from src.agents.report_agent import BusinessReport
from src.charts.chart_builder import ChartSpec


def build_report_pdf(
    report: BusinessReport,
    chart_specs: list[ChartSpec] | None = None,
) -> bytes:
    """Render a business report to PDF bytes."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=48,
        leftMargin=48,
        topMargin=48,
        bottomMargin=48,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(_escape_text(report.title), styles["Title"]), Spacer(1, 12)]

    for section in report.sections:
        story.append(Paragraph(_escape_text(section.title), styles["Heading2"]))
        if section.body:
            story.append(Paragraph(_escape_text(section.body), styles["BodyText"]))
            story.append(Spacer(1, 6))
        if section.bullets:
            items = [
                ListItem(Paragraph(_escape_text(bullet), styles["BodyText"]))
                for bullet in section.bullets
            ]
            story.append(ListFlowable(items, bulletType="bullet", leftIndent=18))
        story.append(Spacer(1, 10))

    if chart_specs:
        story.append(Paragraph("Chart Images", styles["Heading1"]))
        story.append(Spacer(1, 8))
        for chart in chart_specs:
            story.append(Paragraph(_escape_text(chart.title), styles["Heading2"]))
            story.append(Paragraph(_escape_text(chart.description), styles["BodyText"]))
            story.append(Spacer(1, 6))
            chart_image = _chart_to_image(chart)
            if chart_image is None:
                story.append(
                    Paragraph(
                        "Chart image could not be exported in this environment.",
                        styles["Italic"],
                    )
                )
            else:
                story.append(chart_image)
            story.append(Spacer(1, 12))

    document.build(story)
    return buffer.getvalue()


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chart_to_image(chart: ChartSpec) -> Image | None:
    try:
        png_bytes = pio.to_image(
            chart.figure,
            format="png",
            width=900,
            height=500,
            scale=1,
        )
    except Exception:
        return None

    image = Image(BytesIO(png_bytes))
    image.drawWidth = 500
    image.drawHeight = 278
    return image


def chart_export_error(chart_specs: list[ChartSpec]) -> str | None:
    """Return a user-facing chart export problem, if one is detected."""
    if not chart_specs:
        return None
    try:
        pio.to_image(chart_specs[0].figure, format="png", width=100, height=60, scale=1)
    except Exception as exc:
        message = str(exc)
        if "chrome" in message.lower() or "kaleido" in message.lower():
            return (
                "Chart images cannot be embedded because Kaleido/Chrome is unavailable. "
                "The PDF will still contain the report text."
            )
        return f"Chart images cannot be embedded in this environment: {message}"
    return None
