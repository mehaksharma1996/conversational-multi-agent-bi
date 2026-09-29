"""PDF export for business reports."""

from __future__ import annotations

import os
from io import BytesIO

import plotly.io as pio
import reportlab
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import StyleSheet1, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from src.agents.report_agent import BusinessReport
from src.charts.chart_builder import ChartSpec

_UNICODE_FONT_NAME = "Vera"
_fonts_registered = False


def _register_unicode_font() -> str:
    """Register ReportLab's bundled Bitstream Vera Sans (Latin Extended,
    Greek, and Cyrillic coverage) in place of the default Helvetica, which
    is limited to Latin-1/WinAnsi. This does not cover CJK, Arabic, or other
    non-alphabetic scripts, which would need a much larger bundled font.
    """
    global _fonts_registered
    if _fonts_registered:
        return _UNICODE_FONT_NAME

    fonts_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    pdfmetrics.registerFont(TTFont("Vera", os.path.join(fonts_dir, "Vera.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-Bold", os.path.join(fonts_dir, "VeraBd.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-Italic", os.path.join(fonts_dir, "VeraIt.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-BoldItalic", os.path.join(fonts_dir, "VeraBI.ttf")))
    pdfmetrics.registerFontFamily(
        "Vera",
        normal="Vera",
        bold="Vera-Bold",
        italic="Vera-Italic",
        boldItalic="Vera-BoldItalic",
    )
    _fonts_registered = True
    return _UNICODE_FONT_NAME


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
    _apply_unicode_font(styles)
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


def _apply_unicode_font(styles: StyleSheet1) -> None:
    font_name = _register_unicode_font()
    style_fonts = {
        "Title": f"{font_name}-Bold",
        "Heading1": f"{font_name}-Bold",
        "Heading2": f"{font_name}-Bold",
        "BodyText": font_name,
        "Italic": f"{font_name}-Italic",
    }
    for style_name, style_font in style_fonts.items():
        styles[style_name].fontName = style_font


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
