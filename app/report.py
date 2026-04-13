"""
app/report.py

Generate a branded PDF audit report for a medical billing review.

Sections
--------
1. Cover page          — KamSAI Systems branding, practice name, audit date
2. Executive summary   — key metric cards + analysis narrative
3. Error detail table  — one row per billing error, colour-coded by confidence
4. Dispute letters     — one page per letter, preformatted Courier text

Usage
-----
    from app.report import generate_audit_pdf
    path = generate_audit_pdf(bill_id="abc-123", db_path="/path/to/medbill.db")

Returns
-------
    Absolute path to the saved PDF file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import textwrap
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import Flowable

# ─────────────────────────────────────────────────────────────────────────────
# Brand palette
# ─────────────────────────────────────────────────────────────────────────────
_C_NAVY       = colors.HexColor("#0D2B55")   # cover / section headers
_C_BLUE       = colors.HexColor("#1B6CA8")   # accent lines, table headers
_C_BLUE_LIGHT = colors.HexColor("#D0E8F8")   # zebra rows, metric card bg
_C_TEXT       = colors.HexColor("#1A1A2E")   # body text
_C_MUTED      = colors.HexColor("#6B7280")   # captions, sub-labels
_C_DIVIDER    = colors.HexColor("#CBD5E1")   # horizontal rules

_C_CONF_HIGH   = colors.HexColor("#C0392B")   # confidence ≥ 0.9
_C_CONF_MED    = colors.HexColor("#E67E22")   # 0.7–0.89
_C_CONF_LOW    = colors.HexColor("#D4AC0D")   # 0.5–0.69
_C_CONF_FAINT  = colors.HexColor("#95A5A6")   # < 0.5

_PAGE_W, _PAGE_H = letter   # 612 × 792 pt
_MARGIN = 0.85 * inch


# ─────────────────────────────────────────────────────────────────────────────
# Style registry
# ─────────────────────────────────────────────────────────────────────────────
def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()

    def s(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base["Normal"], **kw)

    return {
        # ── cover ──────────────────────────────────────────────────────────
        "cov_brand": s(
            "cov_brand", fontSize=30, fontName="Helvetica-Bold",
            textColor=colors.white, alignment=TA_CENTER, spaceAfter=2,
        ),
        "cov_tagline": s(
            "cov_tagline", fontSize=11, fontName="Helvetica",
            textColor=_C_BLUE_LIGHT, alignment=TA_CENTER, spaceAfter=52,
        ),
        "cov_report_title": s(
            "cov_report_title", fontSize=24, fontName="Helvetica-Bold",
            textColor=colors.white, alignment=TA_CENTER, spaceAfter=6,
        ),
        "cov_report_sub": s(
            "cov_report_sub", fontSize=13, fontName="Helvetica",
            textColor=_C_BLUE_LIGHT, alignment=TA_CENTER, spaceAfter=48,
        ),
        "cov_field_label": s(
            "cov_field_label", fontSize=9, fontName="Helvetica-Bold",
            textColor=_C_BLUE_LIGHT, alignment=TA_CENTER, spaceAfter=1,
        ),
        "cov_field_value": s(
            "cov_field_value", fontSize=13, fontName="Helvetica",
            textColor=colors.white, alignment=TA_CENTER, spaceAfter=18,
        ),
        # ── section headings ───────────────────────────────────────────────
        "h1": s(
            "h1", fontSize=17, fontName="Helvetica-Bold",
            textColor=_C_NAVY, spaceBefore=22, spaceAfter=6,
        ),
        "h2": s(
            "h2", fontSize=13, fontName="Helvetica-Bold",
            textColor=_C_BLUE, spaceBefore=14, spaceAfter=4,
        ),
        # ── body ───────────────────────────────────────────────────────────
        "body": s(
            "body", fontSize=10, fontName="Helvetica",
            textColor=_C_TEXT, leading=15, spaceAfter=5,
        ),
        "body_j": s(
            "body_j", fontSize=10, fontName="Helvetica",
            textColor=_C_TEXT, leading=15, alignment=TA_JUSTIFY, spaceAfter=5,
        ),
        "caption": s(
            "caption", fontSize=8, fontName="Helvetica-Oblique",
            textColor=_C_MUTED, alignment=TA_CENTER,
        ),
        # ── metric cards ───────────────────────────────────────────────────
        "metric_num": s(
            "metric_num", fontSize=22, fontName="Helvetica-Bold",
            textColor=_C_NAVY, alignment=TA_CENTER, spaceAfter=2,
        ),
        "metric_lbl": s(
            "metric_lbl", fontSize=9, fontName="Helvetica",
            textColor=_C_MUTED, alignment=TA_CENTER,
        ),
        # ── table cells ────────────────────────────────────────────────────
        "th": s(
            "th", fontSize=9, fontName="Helvetica-Bold",
            textColor=colors.white, alignment=TA_CENTER,
        ),
        "td": s(
            "td", fontSize=9, fontName="Helvetica",
            textColor=_C_TEXT, leading=12,
        ),
        "td_c": s(
            "td_c", fontSize=9, fontName="Helvetica",
            textColor=_C_TEXT, leading=12, alignment=TA_CENTER,
        ),
        "td_r": s(
            "td_r", fontSize=9, fontName="Helvetica",
            textColor=_C_TEXT, leading=12, alignment=TA_RIGHT,
        ),
        # ── dispute letter ─────────────────────────────────────────────────
        "letter_h1": s(
            "letter_h1", fontSize=13, fontName="Helvetica-Bold",
            textColor=_C_NAVY, spaceAfter=4,
        ),
        "letter_meta": s(
            "letter_meta", fontSize=9, fontName="Helvetica-Bold",
            textColor=_C_BLUE, spaceAfter=2,
        ),
        "letter_body": s(
            "letter_body", fontSize=9, fontName="Courier",
            textColor=_C_TEXT, leading=13, spaceAfter=4,
        ),
        "letter_enc_item": s(
            "letter_enc_item", fontSize=9, fontName="Helvetica",
            textColor=_C_TEXT, leftIndent=12, spaceAfter=2,
        ),
        "letter_escalation": s(
            "letter_escalation", fontSize=9, fontName="Helvetica-Oblique",
            textColor=_C_MUTED, spaceAfter=4,
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Custom flowable: thin coloured rule
# ─────────────────────────────────────────────────────────────────────────────
class _Rule(Flowable):
    def __init__(self, width: float = None, thickness: float = 0.75,
                 color: colors.Color = _C_DIVIDER, space_before: float = 4,
                 space_after: float = 4):
        super().__init__()
        self._w = width
        self.thickness = thickness
        self.color = color
        self.spaceAfter = space_after
        self.spaceBefore = space_before

    def wrap(self, avail_w, avail_h):
        self.width = self._w if self._w else avail_w
        self.height = self.thickness
        return self.width, self.height + self.spaceBefore + self.spaceAfter

    def draw(self):
        c = self.canv
        c.setStrokeColor(self.color)
        c.setLineWidth(self.thickness)
        c.line(0, 0, self.width, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Page event callbacks (header / footer / cover background)
# ─────────────────────────────────────────────────────────────────────────────
_FOOTER_TEXT = "Generated by MedBill AI \u2014 KamSAI Systems"


def _draw_cover_bg(canvas, doc):
    """Full-page navy gradient rectangle for the cover page."""
    canvas.saveState()
    canvas.setFillColor(_C_NAVY)
    canvas.rect(0, 0, _PAGE_W, _PAGE_H, fill=1, stroke=0)
    # Decorative accent bar
    canvas.setFillColor(_C_BLUE)
    canvas.rect(0, _PAGE_H * 0.38, _PAGE_W, 4, fill=1, stroke=0)
    canvas.restoreState()


def _draw_content_footer(canvas, doc):
    """Footer on every content page: divider line + branding + page number."""
    canvas.saveState()
    y = _MARGIN - 0.35 * inch
    canvas.setStrokeColor(_C_DIVIDER)
    canvas.setLineWidth(0.5)
    canvas.line(_MARGIN, y + 10, _PAGE_W - _MARGIN, y + 10)

    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(_C_MUTED)
    canvas.drawString(_MARGIN, y, _FOOTER_TEXT)

    page_label = f"Page {doc.page}"
    canvas.drawRightString(_PAGE_W - _MARGIN, y, page_label)
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: confidence colour
# ─────────────────────────────────────────────────────────────────────────────
def _conf_color(score: float) -> colors.Color:
    if score >= 0.9:
        return _C_CONF_HIGH
    if score >= 0.7:
        return _C_CONF_MED
    if score >= 0.5:
        return _C_CONF_LOW
    return _C_CONF_FAINT


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.0f}%"


# ─────────────────────────────────────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_cover(
    bill_id: str,
    extraction: dict[str, Any],
    analysis: dict[str, Any] | None,
    audit_date: str,
    S: dict,
) -> list:
    """Return flowables for the cover page."""
    provider = extraction.get("provider_name") or "Medical Provider"

    story: list = []
    story.append(Spacer(1, 1.1 * inch))
    story.append(Paragraph("KamSAI Systems", S["cov_brand"]))
    story.append(Paragraph("Intelligent Healthcare Billing Solutions", S["cov_tagline"]))

    story.append(Paragraph("Medical Billing Audit Report", S["cov_report_title"]))
    story.append(Paragraph("Confidential — For Patient Use Only", S["cov_report_sub"]))

    # Metadata table centred on page
    meta_rows = [
        ("Practice / Provider", provider),
        ("Patient", extraction.get("patient_name") or "—"),
        ("Insurance Plan", extraction.get("insurance_plan") or "—"),
        ("Audit Date", audit_date),
        ("Report ID", bill_id[:8].upper()),
    ]
    meta_cells = []
    for label, value in meta_rows:
        meta_cells.append(
            [Paragraph(label, S["cov_field_label"]),
             Paragraph(value, S["cov_field_value"])]
        )

    meta_table = Table(meta_cells, colWidths=[2.2 * inch, 3.4 * inch])
    meta_table.setStyle(TableStyle([
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)

    story.append(NextPageTemplate("content"))
    story.append(PageBreak())
    return story


def _build_exec_summary(
    extraction: dict[str, Any],
    analysis: dict[str, Any] | None,
    S: dict,
) -> list:
    """Return flowables for the executive summary section."""
    story: list = []
    story.append(Paragraph("Executive Summary", S["h1"]))
    story.append(_Rule(color=_C_BLUE, thickness=2))

    # ── Metric cards ─────────────────────────────────────────────────────
    total_billed  = extraction.get("total_billed")
    ins_paid      = extraction.get("total_insurance_paid")
    pat_balance   = extraction.get("total_patient_balance")
    recovery      = analysis["total_estimated_recovery"] if analysis else None
    error_count   = analysis["error_count"] if analysis else None

    metrics = [
        (_fmt_usd(total_billed),  "Total Billed"),
        (_fmt_usd(ins_paid),      "Insurance Paid"),
        (_fmt_usd(pat_balance),   "Patient Balance"),
        (_fmt_usd(recovery),      "Estimated Recovery"),
    ]

    card_data = [[
        Table([[Paragraph(v, S["metric_num"])],
               [Paragraph(l, S["metric_lbl"])]],
              colWidths=[1.45 * inch])
        for v, l in metrics
    ]]
    card_table = Table(card_data, colWidths=[1.55 * inch] * 4,
                       rowHeights=[0.85 * inch])
    card_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), _C_BLUE_LIGHT),
        ("BOX",         (0, 0), (-1, -1), 0.5, _C_DIVIDER),
        ("INNERGRID",   (0, 0), (-1, -1), 0.5, _C_DIVIDER),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",  (0, 0), (-1, -1), 8),
        ("BOTPADDING",  (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",(0, 0), (-1, -1), 4),
        # Highlight recovery card
        ("BACKGROUND",  (3, 0), (3, 0), colors.HexColor("#E8F5E9")),
        ("TEXTCOLOR",   (3, 0), (3, 0), colors.HexColor("#2E7D32")),
    ]))
    story.append(card_table)
    story.append(Spacer(1, 10))

    # ── Error count badge ─────────────────────────────────────────────────
    if error_count is not None:
        badge_text = (
            f"<b>{error_count} potential billing error{'s' if error_count != 1 else ''} "
            f"detected</b> across {len(extraction.get('service_lines', []))} service lines."
        )
        story.append(Paragraph(badge_text, S["body"]))

    # ── Narrative ─────────────────────────────────────────────────────────
    if analysis:
        story.append(Spacer(1, 6))
        story.append(Paragraph("Audit Findings", S["h2"]))
        for line in analysis.get("analysis_summary", "").split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(line, S["body_j"]))

    # ── Bill metadata strip ───────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(Paragraph("Bill Details", S["h2"]))

    dx_codes = extraction.get("diagnosis_codes") or []
    lines = extraction.get("service_lines") or []
    detail_rows = [
        ["Provider NPI",       extraction.get("provider_npi") or "—"],
        ["Provider Address",   extraction.get("provider_address") or "—"],
        ["Claim Number",       extraction.get("claim_number") or "—"],
        ["Account Number",     extraction.get("patient_account_number") or "—"],
        ["Dates of Service",   _service_date_range(lines)],
        ["Diagnosis Codes",    ", ".join(dx_codes) if dx_codes else "—"],
        ["Service Lines",      str(len(lines))],
        ["EOB Compared",       "Yes" if (analysis or {}).get("eob_comparison_possible") else "No"],
    ]

    detail_table = Table(
        [[Paragraph(r[0], S["td"]), Paragraph(r[1], S["td"])] for r in detail_rows],
        colWidths=[1.8 * inch, 4.6 * inch],
    )
    detail_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (0, -1), _C_BLUE_LIGHT),
        ("FONTNAME",    (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("BOTPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _C_BLUE_LIGHT]),
        ("BOX",         (0, 0), (-1, -1), 0.5, _C_DIVIDER),
        ("INNERGRID",   (0, 0), (-1, -1), 0.5, _C_DIVIDER),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(detail_table)
    return story


def _service_date_range(service_lines: list[dict]) -> str:
    dates = [sl.get("date_of_service") for sl in service_lines if sl.get("date_of_service")]
    if not dates:
        return "—"
    dates_sorted = sorted(dates)
    return dates_sorted[0] if dates_sorted[0] == dates_sorted[-1] else f"{dates_sorted[0]} – {dates_sorted[-1]}"


def _build_error_table(
    analysis: dict[str, Any] | None,
    S: dict,
) -> list:
    """Return flowables for the error detail section."""
    story: list = []
    story.append(Paragraph("Billing Error Detail", S["h1"]))
    story.append(_Rule(color=_C_BLUE, thickness=2))

    if not analysis or not analysis.get("errors"):
        story.append(Paragraph(
            "No billing errors were identified in this audit.", S["body"]
        ))
        return story

    errors = analysis["errors"]

    # Column widths: type | cpt | billed | expected | difference | conf | description
    col_w = [1.3*inch, 0.75*inch, 0.75*inch, 0.75*inch, 0.75*inch, 0.55*inch, 1.8*inch]

    header = [
        Paragraph("Error Type",   S["th"]),
        Paragraph("CPT Code",     S["th"]),
        Paragraph("Billed",       S["th"]),
        Paragraph("Expected",     S["th"]),
        Paragraph("Recovery",     S["th"]),
        Paragraph("Conf.",        S["th"]),
        Paragraph("Description",  S["th"]),
    ]
    rows = [header]
    row_colors: list[tuple] = []   # (row_idx, bg_color) pairs for confidence tint

    for i, err in enumerate(errors, start=1):
        billed_amt = err.get("billed_amount")
        recovery   = err.get("estimated_recovery_amount", 0.0)
        expected   = (billed_amt - recovery) if billed_amt is not None else None
        conf       = err.get("confidence_score", 0.0)
        cpt_codes  = ", ".join(err.get("affected_cpt_codes") or []) or "—"
        err_type   = (err.get("error_type") or "other").replace("_", " ").title()

        # Truncate description to keep table readable
        desc_raw = err.get("description", "")
        desc = desc_raw if len(desc_raw) <= 140 else desc_raw[:137] + "…"

        row = [
            Paragraph(err_type,          S["td"]),
            Paragraph(cpt_codes,         S["td_c"]),
            Paragraph(_fmt_usd(billed_amt), S["td_r"]),
            Paragraph(_fmt_usd(expected),   S["td_r"]),
            Paragraph(_fmt_usd(recovery),   S["td_r"]),
            Paragraph(_fmt_pct(conf),       S["td_c"]),
            Paragraph(desc,              S["td"]),
        ]
        rows.append(row)

        # Tint the confidence cell
        conf_col = _conf_color(conf)
        row_colors.append(("BACKGROUND", (5, i), (5, i), conf_col))
        row_colors.append(("TEXTCOLOR",  (5, i), (5, i), colors.white))

    tbl = Table(rows, colWidths=col_w, repeatRows=1)

    base_style = [
        # Header row
        ("BACKGROUND",  (0, 0), (-1, 0), _C_BLUE),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
        ("ALIGN",       (0, 0), (-1, 0), "CENTER"),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        # Zebra rows
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _C_BLUE_LIGHT]),
        # Grid
        ("BOX",         (0, 0), (-1, -1), 0.5, _C_DIVIDER),
        ("INNERGRID",   (0, 0), (-1, -1), 0.25, _C_DIVIDER),
        # Padding
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("BOTPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",(0, 0), (-1, -1), 5),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
    ]
    tbl.setStyle(TableStyle(base_style + row_colors))
    story.append(tbl)

    # ── Legend ───────────────────────────────────────────────────────────
    legend_items = [
        (_C_CONF_HIGH,  "≥ 90% — near-certain error"),
        (_C_CONF_MED,   "70–89% — strong evidence"),
        (_C_CONF_LOW,   "50–69% — requires clarification"),
        (_C_CONF_FAINT, "< 50% — investigate if high value"),
    ]
    legend_cells = []
    for colour, label in legend_items:
        swatch = Table([["  "]], colWidths=[0.18 * inch], rowHeights=[0.15 * inch])
        swatch.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), colour)]))
        legend_cells.append([swatch, Paragraph(label, S["caption"])])
    legend_table = Table(legend_cells, colWidths=[0.25 * inch, 1.8 * inch])
    legend_table.setStyle(TableStyle([
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Confidence legend:", S["caption"]))
    story.append(legend_table)
    return story


def _build_dispute_letters(
    disputes: dict[str, Any] | None,
    S: dict,
) -> list:
    """Return flowables for the dispute letters section."""
    story: list = []
    story.append(Paragraph("Dispute Letters", S["h1"]))
    story.append(_Rule(color=_C_BLUE, thickness=2))

    if not disputes or not disputes.get("letters"):
        story.append(Paragraph(
            "No dispute letters have been generated for this bill. "
            "Run the dispute endpoint to produce letters.", S["body"]
        ))
        return story

    # Cover note
    cover_note = disputes.get("cover_note", "")
    if cover_note:
        story.append(Paragraph("Package Cover Note", S["h2"]))
        for line in cover_note.split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(line, S["body_j"]))
        story.append(Spacer(1, 6))

    # Priority list
    priority = disputes.get("priority_order") or []
    if priority:
        story.append(Paragraph("Recommended Send Order", S["h2"]))
        for idx, subject in enumerate(priority, 1):
            story.append(Paragraph(f"{idx}. {subject}", S["body"]))
        story.append(Spacer(1, 8))

    letters = disputes.get("letters", [])
    for i, letter in enumerate(letters):
        story.append(PageBreak())
        story.append(Paragraph(f"Letter {i + 1} of {len(letters)}", S["caption"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(letter.get("subject_line", "Dispute Letter"), S["letter_h1"]))
        story.append(_Rule(color=_C_DIVIDER))

        # Meta strip
        meta_rows = [
            ("Send To",  letter.get("send_to", "—").replace("_", " ").title()),
            ("Deadline", letter.get("deadline_recommendation", "—")),
        ]
        for label, value in meta_rows:
            story.append(Paragraph(f"<b>{label}:</b>  {value}", S["letter_meta"]))
        story.append(Spacer(1, 6))

        # Letter body — render each line as its own paragraph to preserve spacing
        letter_text = letter.get("letter_text", "")
        for line in letter_text.splitlines():
            # Blank lines become small spacers
            if not line.strip():
                story.append(Spacer(1, 5))
            else:
                # Escape special chars for reportlab XML parser
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(safe, S["letter_body"]))

        # Enclosures
        enclosures = letter.get("enclosures") or []
        if enclosures:
            story.append(Spacer(1, 8))
            story.append(Paragraph("<b>Documents to Enclose:</b>", S["letter_meta"]))
            for enc in enclosures:
                story.append(Paragraph(f"• {enc}", S["letter_enc_item"]))

        # Escalation
        escalation = letter.get("escalation_path", "")
        if escalation:
            story.append(Spacer(1, 8))
            story.append(Paragraph(
                f"<b>If this dispute is denied:</b> {escalation}",
                S["letter_escalation"],
            ))

    return story


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────
def _load_bill_data(bill_id: str, db_path: str) -> dict[str, Any]:
    """
    Load bill, most recent analysis, and most recent dispute from SQLite.

    Returns a dict with keys: extraction, analysis, disputes, meta.
    Raises FileNotFoundError if bill_id not found.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        bill_row = conn.execute(
            "SELECT filename, file_type, extracted_json, created_at "
            "FROM bills WHERE id = ?",
            (bill_id,),
        ).fetchone()
        if bill_row is None:
            raise FileNotFoundError(f"Bill '{bill_id}' not found in {db_path}")

        err_row = conn.execute(
            "SELECT analysis_json, total_estimated_recovery, error_count, "
            "eob_provided, created_at "
            "FROM errors WHERE bill_id = ? ORDER BY created_at DESC LIMIT 1",
            (bill_id,),
        ).fetchone()

        dispute_row = conn.execute(
            "SELECT dispute_json, created_at "
            "FROM disputes WHERE bill_id = ? ORDER BY created_at DESC LIMIT 1",
            (bill_id,),
        ).fetchone()
    finally:
        conn.close()

    extraction = json.loads(bill_row["extracted_json"])

    analysis: dict[str, Any] | None = None
    if err_row:
        analysis = json.loads(err_row["analysis_json"])
        analysis["total_estimated_recovery"] = err_row["total_estimated_recovery"]
        analysis["error_count"] = err_row["error_count"]
        analysis["eob_comparison_possible"] = bool(err_row["eob_provided"])
        analysis["analyzed_at"] = err_row["created_at"]

    disputes: dict[str, Any] | None = None
    if dispute_row:
        disputes = json.loads(dispute_row["dispute_json"])
        disputes["generated_at"] = dispute_row["created_at"]

    return {
        "extraction": extraction,
        "analysis": analysis,
        "disputes": disputes,
        "meta": {
            "filename": bill_row["filename"],
            "file_type": bill_row["file_type"],
            "uploaded_at": bill_row["created_at"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def generate_audit_pdf(bill_id: str, db_path: str) -> str:
    """
    Generate a branded PDF audit report and save it to the outputs directory.

    The outputs directory is derived from db_path's parent:
        <db_path parent>/outputs/<bill_id>_audit.pdf

    Args:
        bill_id:  UUID of the bill (returned by POST /upload).
        db_path:  Absolute path to medbill.db.

    Returns:
        Absolute path to the saved PDF file.

    Raises:
        FileNotFoundError: If db_path doesn't exist or bill_id is unknown.
    """
    data = _load_bill_data(bill_id, db_path)
    extraction = data["extraction"]
    analysis   = data["analysis"]
    disputes   = data["disputes"]

    # ── Output path ──────────────────────────────────────────────────────
    outputs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    out_path = os.path.join(outputs_dir, f"{bill_id}_audit.pdf")

    audit_date = datetime.now().strftime("%B %d, %Y")
    S = _styles()

    # ── Document template ────────────────────────────────────────────────
    content_frame = Frame(
        _MARGIN, _MARGIN,
        _PAGE_W - 2 * _MARGIN, _PAGE_H - 2 * _MARGIN,
        id="content_frame",
    )
    cover_frame = Frame(
        _MARGIN, _MARGIN * 1.5,
        _PAGE_W - 2 * _MARGIN, _PAGE_H - 3 * _MARGIN,
        id="cover_frame",
    )

    doc = BaseDocTemplate(
        out_path,
        pagesize=letter,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title="Medical Billing Audit Report",
        author="MedBill AI — KamSAI Systems",
        subject=f"Audit for bill {bill_id[:8].upper()}",
    )
    doc.addPageTemplates([
        PageTemplate(id="cover",   frames=[cover_frame],   onPage=_draw_cover_bg),
        PageTemplate(id="content", frames=[content_frame], onPage=_draw_content_footer),
    ])

    # ── Build story ──────────────────────────────────────────────────────
    story: list = []

    # 1. Cover page
    story += _build_cover(bill_id, extraction, analysis, audit_date, S)

    # 2. Executive summary
    story += _build_exec_summary(extraction, analysis, S)
    story.append(PageBreak())

    # 3. Error detail table
    story += _build_error_table(analysis, S)
    story.append(PageBreak())

    # 4. Dispute letters
    story += _build_dispute_letters(disputes, S)

    # ── Render ───────────────────────────────────────────────────────────
    doc.build(story)
    return out_path
