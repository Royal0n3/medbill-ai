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

import io
import json
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


def _extract_text(file: FileStorage) -> tuple[str, str]:
    """
    Return (raw_text, file_type) from an uploaded FileStorage.

    file_type is 'pdf' or 'txt'.
    Raises ValueError if the PDF contains no extractable text.
    """
    filename = secure_filename(file.filename or "upload")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if ext == "pdf":
        data = file.read()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        raw = "\n\n".join(p for p in pages if p.strip())
        if not raw.strip():
            raise ValueError("PDF contains no extractable text (may be scanned image).")
        return raw, "pdf"

    # Plain text (utf-8; replace undecodable bytes rather than crash)
    raw = file.read().decode("utf-8", errors="replace")
    return raw, "txt"


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
    try:
        package: DisputePackage = generate_dispute_letters(
            analysis.errors,
            patient_info=patient_info,
            bill_data=bill_data,
        )
    except Exception:
        current_app.logger.exception("Claude dispute generation failed")
        return _err("Dispute generation service unavailable. Try again shortly.", 502)

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
