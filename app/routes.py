"""
app/routes.py

Four core endpoints for the medical billing error detection service.

POST /upload               — Accept PDF or .txt, extract bill data via Claude
POST /analyze/<bill_id>    — Audit extracted bill, detect errors
POST /dispute/<bill_id>    — Generate dispute letters for detected errors
GET  /report/<bill_id>     — Full audit report: bill summary + errors + recovery total
GET  /health               — Liveness check
"""

from __future__ import annotations

import html
import io
import json
import os
import uuid
from typing import Any

import pdfplumber
from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.db import get_db
from app.report import generate_audit_pdf
from prompts.analyze import AnalysisResult, analyze_bill
from prompts.dispute import DisputePackage, PatientInfo, generate_dispute_letters
from prompts.extract import BillExtraction, extract_bill

main = Blueprint("main", __name__)

_ALLOWED_EXTENSIONS = {"pdf", "txt"}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


def _err(msg: str, status: int = 400) -> tuple[Response, int]:
    return jsonify({"error": msg}), status


def _ocr_pdf(data: bytes) -> str:
    """
    OCR fallback for image-based PDFs.
    Converts each page to an image via pdf2image, then runs pytesseract.
    Returns extracted text or empty string on failure.
    """
    try:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(data, dpi=300)
        pages_text = []
        for image in images:
            page_text = pytesseract.image_to_string(image, lang="eng")
            if page_text.strip():
                pages_text.append(page_text)
        return "\n\n".join(pages_text)
    except Exception as exc:
        current_app.logger.warning("OCR fallback failed: %s", exc)
        return ""


def _extract_text(file: FileStorage) -> tuple[str, str]:
    """
    Return (raw_text, file_type) from an uploaded FileStorage.

    file_type is 'pdf' or 'txt'.

    Strategy for PDFs:
      1. Try pdfplumber (text-based PDFs — fast, accurate)
      2. If no text extracted, fall back to OCR via pytesseract (scanned/image PDFs)
      3. If OCR also yields nothing, raise ValueError

    Raises ValueError if no text can be extracted at all.
    """
    filename = secure_filename(file.filename or "upload")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if ext == "pdf":
        data = file.read()

        # --- Step 1: Try pdfplumber (native text extraction) ----------------
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            raw = "\n\n".join(p for p in pages if p.strip())
        except Exception:
            raw = ""

        # --- Step 2: OCR fallback for image-based PDFs ----------------------
        if not raw.strip():
            current_app.logger.info(
                "No extractable text in PDF — attempting OCR fallback"
            )
            raw = _ocr_pdf(data)

        # --- Step 3: Give up if still empty ---------------------------------
        if not raw.strip():
            raise ValueError(
                "Could not extract text from this PDF. "
                "The file may be corrupted, encrypted, or use an unsupported format."
            )

        return raw, "pdf"

    # Plain text (utf-8; replace undecodable bytes rather than crash)
    raw = file.read().decode("utf-8", errors="replace")
    return raw, "txt"


# ---------------------------------------------------------------------------
# Email helpers for POST /send-report
# ---------------------------------------------------------------------------


def _esc(s: Any) -> str:
    return html.escape(str(s or ""))


def _build_audit_email_html(
    patient_name: str,
    error_count: int,
    total_recovery: float,
    errors: list[dict[str, Any]],
    dispute_letters: list[dict[str, Any]],
) -> str:
    recovery_fmt = f"{total_recovery:,.2f}"
    plural = "s" if error_count != 1 else ""

    # Errors table rows
    if errors:
        rows_html = ""
        for err in errors:
            cpt = _esc(err.get("cpt_code") or "—")
            etype = _esc(err.get("error_type") or err.get("category") or "Unknown")
            desc = _esc((err.get("description") or err.get("explanation") or "")[:140])
            rec = float(err.get("estimated_recovery") or 0)
            rec_str = f"${rec:,.2f}" if rec else "—"
            rows_html += (
                f'<tr style="border-bottom:1px solid #EEF2F7">'
                f'<td style="padding:10px 14px;font-family:Courier New,monospace;font-size:12px;color:#0C3B6E;white-space:nowrap">{cpt}</td>'
                f'<td style="padding:10px 14px;font-weight:600;color:#0D1B2A;font-size:13.5px">{etype}</td>'
                f'<td style="padding:10px 14px;color:#4A5568;font-size:13px">{desc}</td>'
                f'<td style="padding:10px 14px;text-align:right;font-weight:700;color:#10B981;white-space:nowrap">{_esc(rec_str)}</td>'
                f'</tr>'
            )
    else:
        rows_html = (
            '<tr><td colspan="4" style="padding:16px;color:#9BADC4;'
            'text-align:center;font-size:13px">No errors recorded.</td></tr>'
        )

    errors_table = (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #DDE3EE;border-radius:8px;margin-bottom:28px">'
        '<thead><tr style="background:#F0F4F9">'
        '<th style="padding:10px 14px;text-align:left;color:#6B7C93;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">CPT</th>'
        '<th style="padding:10px 14px;text-align:left;color:#6B7C93;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Error Type</th>'
        '<th style="padding:10px 14px;text-align:left;color:#6B7C93;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Description</th>'
        '<th style="padding:10px 14px;text-align:right;color:#6B7C93;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Est. Recovery</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )

    # Dispute letter sections
    letters_parts: list[str] = []
    for i, letter in enumerate(dispute_letters, 1):
        subject = _esc(letter.get("subject_line") or f"Dispute Letter {i}")
        send_to = _esc(letter.get("send_to") or "See audit report")
        deadline = _esc(letter.get("deadline_recommendation") or "As soon as possible")
        text_esc = _esc(letter.get("letter_text") or "")
        escalation = _esc(letter.get("escalation_path") or "Contact your state insurance commissioner.")
        enc_items = "".join(
            f'<li style="margin-bottom:4px">{_esc(enc)}</li>'
            for enc in (letter.get("enclosures") or [])
        ) or "<li>See audit report for documentation requirements.</li>"

        letters_parts.append(
            f'<div style="border:1px solid #DDE3EE;border-radius:10px;margin-bottom:24px;overflow:hidden">'
            f'<div style="background:#0C3B6E;padding:14px 22px">'
            f'<div style="color:#FFFFFF;font-size:14px;font-weight:700">Dispute Letter {i} — {subject}</div>'
            f'</div><div style="padding:22px">'
            f'<table cellpadding="0" cellspacing="0" style="margin-bottom:16px;font-size:13.5px">'
            f'<tr><td style="color:#6B7C93;padding-right:16px;padding-bottom:8px;white-space:nowrap;vertical-align:top">Send&nbsp;to:</td>'
            f'<td style="color:#0D1B2A;font-weight:600;padding-bottom:8px">{send_to}</td></tr>'
            f'<tr><td style="color:#6B7C93;padding-right:16px;vertical-align:top">Deadline:</td>'
            f'<td style="color:#EF4444;font-weight:600">{deadline}</td></tr>'
            f'</table>'
            f'<div style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#6B7C93;margin-bottom:8px">Letter Text</div>'
            f'<div style="background:#F7F9FC;border-radius:6px;padding:16px 18px;font-family:Courier New,monospace;'
            f'font-size:12px;color:#0D1B2A;white-space:pre-wrap;line-height:1.7;border:1px solid #EEF2F7">{text_esc}</div>'
            f'<div style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#6B7C93;margin:16px 0 8px">Documents to Include</div>'
            f'<ul style="margin:0;padding-left:20px;font-size:13.5px;color:#4A5568;line-height:1.7">{enc_items}</ul>'
            f'<p style="font-size:13px;color:#6B7C93;margin:14px 0 0;line-height:1.5">'
            f'<strong style="color:#0D1B2A">If denied:</strong> {escalation}</p>'
            f'</div></div>'
        )

    letters_html = "".join(letters_parts) or (
        '<p style="color:#9BADC4;font-size:14px">No dispute letters generated.</p>'
    )

    return (
        '<!DOCTYPE html><html><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head><body style="margin:0;padding:0;background:#EEF2F7;font-family:Helvetica,Arial,sans-serif">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#EEF2F7;padding:32px 16px">'
        '<tr><td align="center">'
        '<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%">'
        '<tr><td style="background:#0C3B6E;padding:28px 40px;border-radius:12px 12px 0 0">'
        '<div style="color:#FFFFFF;font-size:20px;font-weight:700">MedBill AI</div>'
        '<div style="color:rgba(255,255,255,0.5);font-size:11px;margin-top:3px;letter-spacing:0.08em;text-transform:uppercase">by KamSAI Systems</div>'
        '</td></tr>'
        '<tr><td style="background:#FFFFFF;padding:40px;border-radius:0 0 12px 12px">'
        f'<h1 style="color:#0D1B2A;font-size:24px;font-weight:700;margin:0 0 8px">Your Medical Bill Audit Results</h1>'
        f'<p style="color:#6B7C93;font-size:15px;margin:0 0 32px;line-height:1.6">Hi {_esc(patient_name)}, here are the findings from your medical bill audit.</p>'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#F0F4F9;border-radius:10px;margin-bottom:32px"><tr>'
        f'<td width="50%" style="padding:24px 20px;text-align:center;border-right:1px solid #DDE3EE">'
        f'<div style="font-size:40px;font-weight:700;color:#EF4444">{error_count}</div>'
        f'<div style="font-size:12px;color:#6B7C93;margin-top:4px;text-transform:uppercase;letter-spacing:0.06em">Billing Error{plural} Found</div>'
        f'</td>'
        f'<td width="50%" style="padding:24px 20px;text-align:center">'
        f'<div style="font-size:40px;font-weight:700;color:#00C9A7">${recovery_fmt}</div>'
        f'<div style="font-size:12px;color:#6B7C93;margin-top:4px;text-transform:uppercase;letter-spacing:0.06em">Total Recoverable</div>'
        f'</td></tr></table>'
        '<h2 style="font-size:16px;font-weight:700;color:#0D1B2A;margin:0 0 14px">Errors Identified</h2>'
        f'{errors_table}'
        '<div style="background:#F0F4F9;border-left:4px solid #0C3B6E;padding:16px 20px;border-radius:0 6px 6px 0;margin:0 0 32px">'
        '<div style="font-size:14px;font-weight:700;color:#0C3B6E;margin-bottom:6px">What Happens Next</div>'
        '<p style="font-size:13.5px;color:#4A5568;margin:0;line-height:1.6">Each error has a ready-to-send dispute letter below. '
        'Send them in order, certified mail when possible, and keep copies of everything you submit.</p>'
        '</div>'
        '<h2 style="font-size:16px;font-weight:700;color:#0D1B2A;margin:0 0 16px">Your Dispute Letters</h2>'
        f'{letters_html}'
        '<div style="border-top:1px solid #EEF2F7;padding-top:20px;margin-top:8px;text-align:center">'
        '<p style="font-size:12px;color:#9BADC4;margin:0">Generated by MedBill AI by KamSAI Systems · '
        '<a href="https://mdbillify.netlify.app" style="color:#0C3B6E;text-decoration:none">mdbillify.netlify.app</a></p>'
        '</div>'
        '</td></tr></table></td></tr></table></body></html>'
    )


def _build_internal_email_text(
    bill_id: str,
    email: str,
    error_count: int,
    total_recovery: float,
    errors: list[dict[str, Any]],
) -> str:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"New audit completed — MBI-{bill_id[:8].upper()}",
        f"Timestamp:      {ts}",
        f"Bill ID:        {bill_id}",
        f"Submitted by:   {email}",
        f"Errors found:   {error_count}",
        f"Total recovery: ${total_recovery:,.2f}",
        "",
        "Errors:",
    ]
    for i, err in enumerate(errors, 1):
        etype = err.get("error_type") or err.get("category") or "Unknown"
        cpt = err.get("cpt_code") or "—"
        rec = float(err.get("estimated_recovery") or 0)
        lines.append(f"  {i}. [{cpt}] {etype} — ${rec:,.2f}")
    return "\n".join(lines)


def _send_audit_emails(
    bill_id: str,
    email: str,
    patient_name: str,
    error_count: int,
    total_recovery: float,
    errors: list[dict[str, Any]],
    dispute_letters: list[dict[str, Any]],
) -> None:
    import sib_api_v3_sdk

    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY not configured")

    config = sib_api_v3_sdk.Configuration()
    config.api_key["api-key"] = api_key
    brevo = sib_api_v3_sdk.ApiClient(config)
    api = sib_api_v3_sdk.TransactionalEmailsApi(brevo)
    sender = {"email": "kameron@kamsaisystems.com", "name": "MedBill AI"}
    plural = "s" if error_count != 1 else ""

    # Email 1: full audit results to the submitting user
    subject1 = (
        f"Your MedBill AI Audit — {error_count} error{plural} found, "
        f"${total_recovery:,.0f} recoverable"
    )
    api.send_transac_email(sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": email, "name": patient_name}],
        sender=sender,
        subject=subject1,
        html_content=_build_audit_email_html(
            patient_name, error_count, total_recovery, errors, dispute_letters
        ),
    ))

    # Email 2: internal notification
    short_id = bill_id[:8].upper()
    subject2 = (
        f"New audit completed — MBI-{short_id} | "
        f"{error_count} error{plural} | ${total_recovery:,.0f} recovery"
    )
    api.send_transac_email(sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": "kameron@kamsaisystems.com", "name": "Kameron"}],
        sender=sender,
        subject=subject2,
        text_content=_build_internal_email_text(
            bill_id, email, error_count, total_recovery, errors
        ),
    ))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@main.get("/")
def serve_frontend():
    return send_from_directory('../app/static', 'index.html')


@main.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------


@main.get("/stats")
def stats() -> Response:
    """
    Aggregate metrics for the ops dashboard.

    Response 200
    ------------
    {
      "audits_last_7_days":       12,
      "total_errors_found":       47,
      "total_recovery_estimated": 18250.00,
      "dispute_letters_generated": 23
    }
    """
    db = get_db()
    row = db.execute("""
        SELECT
            (SELECT COUNT(*) FROM bills
             WHERE created_at >= datetime('now', '-7 days'))          AS audits_last_7_days,
            (SELECT COALESCE(SUM(error_count), 0) FROM errors)        AS total_errors_found,
            (SELECT COALESCE(SUM(total_estimated_recovery), 0.0)
             FROM errors)                                              AS total_recovery_estimated,
            (SELECT COALESCE(SUM(letter_count), 0) FROM disputes)     AS dispute_letters_generated
    """).fetchone()

    return jsonify({
        "audits_last_7_days":        row["audits_last_7_days"],
        "total_errors_found":        row["total_errors_found"],
        "total_recovery_estimated":  round(row["total_recovery_estimated"], 2),
        "dispute_letters_generated": row["dispute_letters_generated"],
    })


# ---------------------------------------------------------------------------
# GET /audits
# ---------------------------------------------------------------------------


@main.get("/audits")
def audits_list() -> Response:
    """
    Recent audits for the ops dashboard table (latest 50).

    Response 200
    ------------
    {
      "audits": [
        {
          "bill_id":            "<uuid>",
          "practice_name":      "Lakeside Family Practice",
          "submitted_date":     "2024-01-15 10:30:00",
          "errors_found":       3,
          "recovery_estimated": 1250.00,
          "status":             "disputed" | "analyzed" | "pending"
        },
        ...
      ]
    }
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            b.id,
            b.extracted_json,
            b.created_at,
            e.error_count,
            e.total_estimated_recovery,
            CASE
                WHEN d.id IS NOT NULL THEN 'disputed'
                WHEN e.id IS NOT NULL THEN 'analyzed'
                ELSE 'pending'
            END AS status
        FROM bills b
        LEFT JOIN (
            SELECT e1.bill_id, e1.id, e1.error_count, e1.total_estimated_recovery
            FROM errors e1
            WHERE e1.created_at = (
                SELECT MAX(e2.created_at) FROM errors e2 WHERE e2.bill_id = e1.bill_id
            )
        ) e ON e.bill_id = b.id
        LEFT JOIN (
            SELECT d1.bill_id, d1.id
            FROM disputes d1
            WHERE d1.created_at = (
                SELECT MAX(d2.created_at) FROM disputes d2 WHERE d2.bill_id = d1.bill_id
            )
        ) d ON d.bill_id = b.id
        ORDER BY b.created_at DESC
        LIMIT 50
    """).fetchall()

    result = []
    for row in rows:
        extraction: dict[str, Any] = json.loads(row["extracted_json"])
        result.append({
            "bill_id":            row["id"],
            "practice_name":      extraction.get("provider_name") or "Unknown Practice",
            "submitted_date":     row["created_at"],
            "errors_found":       row["error_count"] or 0,
            "recovery_estimated": round(row["total_estimated_recovery"] or 0.0, 2),
            "status":             row["status"],
        })

    return jsonify({"audits": result})


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------


@main.post("/upload")
def upload() -> tuple[Response, int]:
    """
    Accept a PDF or plain-text medical bill, extract structured data via Claude.

    Form fields
    -----------
    file  (required) — the bill file (PDF or .txt)

    Response 201
    ------------
    {
      "bill_id":    "<uuid>",
      "filename":   "bill.pdf",
      "extraction": { ...BillExtraction fields... }
    }
    """
    if "file" not in request.files:
        return _err("Request must include a 'file' field in multipart/form-data.")

    file = request.files["file"]
    if not file.filename:
        return _err("File field is present but has no filename.")

    if not _allowed(file.filename):
        return _err(
            f"Unsupported file type '{file.filename}'. "
            "Only PDF (.pdf) and plain-text (.txt) files are accepted."
        )

    # --- Extract raw text -----------------------------------------------
    try:
        raw_text, file_type = _extract_text(file)
    except ValueError as exc:
        return _err(str(exc), 422)
    except Exception:
        current_app.logger.exception("Text extraction failed")
        return _err("Could not extract text from the uploaded file.", 422)

    if not raw_text.strip():
        return _err("The uploaded file appears to be empty.", 422)

    # --- Call Claude --------------------------------------------------------
    try:
        extraction: BillExtraction = extract_bill(raw_text)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        current_app.logger.exception("Claude extraction failed")
        return _err(f"Extraction service unavailable: {exc}\n\n{tb}", 502)

    # --- Persist ------------------------------------------------------------
    bill_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    extracted_json = extraction.model_dump_json()

    db = get_db()
    db.execute(
        "INSERT INTO bills (id, filename, file_type, raw_text, extracted_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (bill_id, filename, file_type, raw_text, extracted_json),
    )
    db.commit()

    return (
        jsonify(
            {
                "bill_id": bill_id,
                "filename": filename,
                "extraction": json.loads(extracted_json),
            }
        ),
        201,
    )


# ---------------------------------------------------------------------------
# POST /analyze/<bill_id>
# ---------------------------------------------------------------------------


@main.post("/analyze/<bill_id>")
def analyze(bill_id: str) -> tuple[Response, int]:
    """
    Audit an uploaded bill for billing errors.

    URL parameter
    -------------
    bill_id — UUID returned by POST /upload

    JSON body (optional)
    --------------------
    {
      "eob_text": "<raw text of the Explanation of Benefits document>"
    }

    Response 200
    ------------
    {
      "analysis_id":              "<uuid>",
      "bill_id":                  "<uuid>",
      "error_count":              3,
      "total_estimated_recovery": 412.50,
      "analysis_summary":         "...",
      "eob_comparison_possible":  true,
      "errors":                   [ ...BillingError objects... ]
    }
    """
    db = get_db()

    row = db.execute(
        "SELECT extracted_json FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    if row is None:
        return _err(f"Bill '{bill_id}' not found.", 404)

    body: dict[str, Any] = request.get_json(silent=True) or {}
    eob_text: str = body.get("eob_text", "")

    bill_data = BillExtraction.model_validate_json(row["extracted_json"])

    # --- Call Claude --------------------------------------------------------
    try:
        result: AnalysisResult = analyze_bill(bill_data, eob_text=eob_text)
    except Exception:
        current_app.logger.exception("Claude analysis failed")
        return _err("Analysis service unavailable. Try again shortly.", 502)

    # --- Persist ------------------------------------------------------------
    analysis_id = str(uuid.uuid4())
    analysis_json = result.model_dump_json()

    db.execute(
        "INSERT INTO errors "
        "(id, bill_id, eob_provided, analysis_json, total_estimated_recovery, error_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            analysis_id,
            bill_id,
            1 if eob_text.strip() else 0,
            analysis_json,
            result.total_estimated_recovery,
            len(result.errors),
        ),
    )
    db.commit()

    return (
        jsonify(
            {
                "analysis_id": analysis_id,
                "bill_id": bill_id,
                "error_count": len(result.errors),
                "total_estimated_recovery": result.total_estimated_recovery,
                "analysis_summary": result.analysis_summary,
                "eob_comparison_possible": result.eob_comparison_possible,
                "errors": json.loads(analysis_json)["errors"],
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# POST /dispute/<bill_id>
# ---------------------------------------------------------------------------


@main.post("/dispute/<bill_id>")
def dispute(bill_id: str) -> tuple[Response, int] | Response:
    """
    Generate dispute letters for every error found in the most recent analysis.

    URL parameter
    -------------
    bill_id — UUID returned by POST /upload

    Query parameters
    ----------------
    format  — 'json' (default) | 'txt'
              'txt' returns a plain-text attachment; 'json' returns the full
              DisputePackage object.

    JSON body (required)
    --------------------
    {
      "patient_info": {
        "full_name":           "Jane Doe",       // required
        "address":             "123 Main St ...", // required
        "phone":               "(555) 123-4567",  // required
        "insurance_id":        "XYZ-123456789",   // required
        "date_of_birth":       "01/15/1975",      // optional
        "email":               "jane@email.com",  // optional
        "policy_group_number": "GRP-001"          // optional
      }
    }

    Response 200 — JSON
    -------------------
    {
      "dispute_id":     "<uuid>",
      "bill_id":        "<uuid>",
      "letter_count":   3,
      "cover_note":     "...",
      "priority_order": ["Re: Duplicate charge ...", ...],
      "letters":        [ ...DisputeLetter objects... ]
    }

    Response 200 — TXT (format=txt)
    --------------------------------
    Content-Disposition: attachment; filename="dispute_<bill_id>.txt"
    Plain-text file containing all letters separated by dividers.
    """
    db = get_db()

    # --- Fetch bill ---------------------------------------------------------
    bill_row = db.execute(
        "SELECT extracted_json FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    if bill_row is None:
        return _err(f"Bill '{bill_id}' not found.", 404)

    # --- Fetch most recent analysis -----------------------------------------
    err_row = db.execute(
        "SELECT analysis_json FROM errors WHERE bill_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (bill_id,),
    ).fetchone()
    if err_row is None:
        return _err(
            f"No analysis found for bill '{bill_id}'. "
            f"Run POST /analyze/{bill_id} first.",
            422,
        )

    # --- Validate patient_info ----------------------------------------------
    body: dict[str, Any] = request.get_json(silent=True) or {}
    pi_raw: dict[str, str] = body.get("patient_info", {})

    _required_pi_fields = ("full_name", "address", "phone", "insurance_id")
    missing = [f for f in _required_pi_fields if not pi_raw.get(f)]
    if missing:
        return _err(
            f"patient_info is missing required fields: {', '.join(missing)}. "
            "Required: full_name, address, phone, insurance_id."
        )

    patient_info = PatientInfo(
        full_name=pi_raw["full_name"],
        address=pi_raw["address"],
        phone=pi_raw["phone"],
        insurance_id=pi_raw["insurance_id"],
        date_of_birth=pi_raw.get("date_of_birth", ""),
        email=pi_raw.get("email", ""),
        policy_group_number=pi_raw.get("policy_group_number", ""),
    )

    # --- Reconstruct objects ------------------------------------------------
    bill_data = BillExtraction.model_validate_json(bill_row["extracted_json"])
    analysis = AnalysisResult.model_validate_json(err_row["analysis_json"])

    # --- Call Claude --------------------------------------------------------
    current_app.logger.info(
        "Generating dispute letters for bill %s: %d error(s) to dispute",
        bill_id, len(analysis.errors),
    )
    try:
        package: DisputePackage = generate_dispute_letters(
            analysis.errors,
            patient_info=patient_info,
            bill_data=bill_data,
        )
    except Exception:
        current_app.logger.exception("Claude dispute generation failed")
        return _err("Dispute generation service unavailable. Try again shortly.", 502)
    current_app.logger.info(
        "Dispute generation complete for bill %s: %d letter(s) generated",
        bill_id, len(package.letters),
    )

    # --- Persist ------------------------------------------------------------
    dispute_id = str(uuid.uuid4())
    dispute_json = package.model_dump_json()

    db.execute(
        "INSERT INTO disputes (id, bill_id, dispute_json, letter_count) VALUES (?, ?, ?, ?)",
        (dispute_id, bill_id, dispute_json, len(package.letters)),
    )
    db.commit()

    # --- Format response ----------------------------------------------------
    fmt = request.args.get("format", "json").lower()

    if fmt == "txt":
        div = "\n" + "=" * 80 + "\n"
        parts = [
            "MEDICAL BILLING DISPUTE PACKAGE",
            div,
            "COVER NOTE",
            "-" * 40,
            package.cover_note,
            div,
            "SEND IN THIS ORDER:",
        ]
        for i, subject in enumerate(package.priority_order, 1):
            parts.append(f"  {i}. {subject}")
        parts.append(div)

        for letter in package.letters:
            parts += [
                f"LETTER: {letter.subject_line}",
                f"Send to:  {letter.send_to}",
                f"Deadline: {letter.deadline_recommendation}",
                "",
                letter.letter_text,
                "",
                "Documents to enclose:",
                *[f"  - {enc}" for enc in letter.enclosures],
                "",
                f"If this dispute is denied: {letter.escalation_path}",
                div,
            ]

        return Response(
            "\n".join(parts),
            mimetype="text/plain",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="dispute_{bill_id[:8]}.txt"'
                )
            },
        )

    return (
        jsonify(
            {
                "dispute_id": dispute_id,
                "bill_id": bill_id,
                "letter_count": len(package.letters),
                "cover_note": package.cover_note,
                "priority_order": package.priority_order,
                "letters": json.loads(dispute_json)["letters"],
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# POST /send-report
# ---------------------------------------------------------------------------


@main.post("/send-report")
def send_report() -> tuple[Response, int]:
    """
    Send two emails via Brevo after the full pipeline completes.

    Email 1 → submitting user: full audit results + dispute letters.
    Email 2 → kameron@kamsaisystems.com: internal audit notification.

    Always returns 200 — Brevo failures are logged but never surfaced to the
    frontend so the confirmation screen is never blocked.

    JSON body
    ---------
    {
      "bill_id":         "<uuid>",
      "email":           "patient@example.com",
      "patient_name":    "Jane Doe",
      "error_count":     3,
      "total_recovery":  412.50,
      "errors":          [ ...BillingError objects... ],
      "dispute_letters": [ ...DisputeLetter objects... ]
    }
    """
    body: dict[str, Any] = request.get_json(silent=True) or {}
    bill_id: str = body.get("bill_id", "")
    email: str = body.get("email", "")

    if not bill_id or not email:
        return _err("bill_id and email are required.", 400)

    try:
        _send_audit_emails(
            bill_id=bill_id,
            email=email,
            patient_name=str(body.get("patient_name") or "Valued Patient"),
            error_count=int(body.get("error_count") or 0),
            total_recovery=float(body.get("total_recovery") or 0.0),
            errors=list(body.get("errors") or []),
            dispute_letters=list(body.get("dispute_letters") or []),
        )
    except Exception:
        current_app.logger.exception("send-report: Brevo delivery failed")

    return jsonify({"sent": True}), 200


# ---------------------------------------------------------------------------
# GET /report/<bill_id>
# ---------------------------------------------------------------------------


@main.get("/report/<bill_id>")
def report(bill_id: str) -> tuple[Response, int]:
    """
    Return a full audit report for a bill.

    Combines the extracted bill summary with the most recent error analysis.
    If no analysis has been run, the 'analysis' key is null.

    Response 200
    ------------
    {
      "bill_id":      "<uuid>",
      "filename":     "bill.pdf",
      "file_type":    "pdf",
      "uploaded_at":  "2024-01-15 10:30:00",
      "bill_summary": {
        "provider_name":       "...",
        "patient_name":        "...",
        "insurance_plan":      "...",
        "total_billed":        1200.00,
        "total_insurance_paid": 800.00,
        "total_patient_balance": 400.00,
        "service_line_count":  5,
        "diagnosis_codes":     ["Z00.00"]
      },
      "analysis": {                         // null if not yet analyzed
        "analyzed_at":              "...",
        "eob_provided":             false,
        "error_count":              2,
        "total_estimated_recovery": 350.00,
        "analysis_summary":         "...",
        "errors":                   [ ...BillingError objects... ]
      }
    }
    """
    db = get_db()

    bill_row = db.execute(
        "SELECT filename, file_type, extracted_json, created_at "
        "FROM bills WHERE id = ?",
        (bill_id,),
    ).fetchone()
    if bill_row is None:
        return _err(f"Bill '{bill_id}' not found.", 404)

    err_row = db.execute(
        "SELECT analysis_json, total_estimated_recovery, error_count, "
        "eob_provided, created_at "
        "FROM errors WHERE bill_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (bill_id,),
    ).fetchone()

    extraction: dict[str, Any] = json.loads(bill_row["extracted_json"])

    result: dict[str, Any] = {
        "bill_id": bill_id,
        "filename": bill_row["filename"],
        "file_type": bill_row["file_type"],
        "uploaded_at": bill_row["created_at"],
        "bill_summary": {
            "provider_name": extraction.get("provider_name"),
            "patient_name": extraction.get("patient_name"),
            "insurance_plan": extraction.get("insurance_plan"),
            "total_billed": extraction.get("total_billed"),
            "total_insurance_paid": extraction.get("total_insurance_paid"),
            "total_patient_balance": extraction.get("total_patient_balance"),
            "service_line_count": len(extraction.get("service_lines", [])),
            "diagnosis_codes": extraction.get("diagnosis_codes", []),
        },
        "analysis": None,
    }

    if err_row:
        analysis_data: dict[str, Any] = json.loads(err_row["analysis_json"])
        result["analysis"] = {
            "analyzed_at": err_row["created_at"],
            "eob_provided": bool(err_row["eob_provided"]),
            "error_count": err_row["error_count"],
            "total_estimated_recovery": err_row["total_estimated_recovery"],
            "analysis_summary": analysis_data.get("analysis_summary"),
            "errors": analysis_data.get("errors", []),
        }

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# GET /report/<bill_id>/pdf
# ---------------------------------------------------------------------------


@main.get("/report/<bill_id>/pdf")
def report_pdf(bill_id: str) -> tuple[Response, int] | Response:
    """
    Generate and stream the PDF audit report for a bill.

    The PDF is saved to outputs/<bill_id>_audit.pdf and returned as an
    inline/download attachment.

    Query parameters
    ----------------
    download  — 'true' sets Content-Disposition: attachment (default: inline)
    """
    db = get_db()
    row = db.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if row is None:
        return _err(f"Bill '{bill_id}' not found.", 404)

    try:
        pdf_path = generate_audit_pdf(
            bill_id=bill_id,
            db_path=current_app.config["DATABASE"],
        )
    except Exception:
        current_app.logger.exception("PDF generation failed")
        return _err("PDF generation failed. Check server logs.", 502)

    disposition = (
        "attachment" if request.args.get("download", "").lower() == "true" else "inline"
    )
    filename = f"medbill_audit_{bill_id[:8]}.pdf"

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )
