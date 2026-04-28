"""
prompts/extract.py

Stage 1 — Structured extraction from raw medical bill text.

Input:  raw text (from pdfplumber or OCR) of a medical bill / UB-04 / CMS-1500
Output: validated BillExtraction Pydantic object (also serialisable to JSON)

Usage:
    from prompts.extract import extract_bill

    with open("bill.pdf", "rb") as f:
        raw_text = extract_text_from_pdf(f)

    result = extract_bill(raw_text)
    print(result.model_dump_json(indent=2))
"""

from __future__ import annotations

import json
import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ServiceLine(BaseModel):
    date_of_service: Optional[str] = Field(None, description="MM/DD/YYYY or ISO date")
    cpt_code: Optional[str] = Field(None, description="5-character CPT or HCPCS code")
    description: Optional[str] = Field(None, description="Procedure / service description")
    units: Optional[int] = Field(None, description="Number of units billed")
    billed_amount: Optional[float] = Field(None, description="Amount billed by provider (USD)")
    allowed_amount: Optional[float] = Field(None, description="Payer-allowed amount, if present")
    insurance_payment: Optional[float] = Field(None, description="Amount paid by insurance")
    patient_responsibility: Optional[float] = Field(None, description="Copay + coinsurance + deductible")
    adjustment_amount: Optional[float] = Field(None, description="Contractual or other adjustments")
    denial_code: Optional[str] = Field(None, description="Denial or remark code, if applicable")

    @field_validator("description", "denial_code", "cpt_code", "date_of_service", mode="before")
    @classmethod
    def coerce_list_to_str(cls, v):
        if isinstance(v, list):
            return "\n".join(str(item) for item in v if item is not None)
        return v


class BillExtraction(BaseModel):
    provider_name: Optional[str] = Field(None, description="Full name of provider / facility")
    provider_npi: Optional[str] = Field(None, description="10-digit NPI number")
    provider_address: Optional[str] = Field(None, description="Street, city, state, ZIP")
    patient_name: Optional[str] = Field(None, description="Patient full name")
    patient_dob: Optional[str] = Field(None, description="Date of birth MM/DD/YYYY")
    patient_account_number: Optional[str] = Field(None, description="Internal account / MRN")
    insurance_id: Optional[str] = Field(None, description="Member ID / policy number")
    insurance_plan: Optional[str] = Field(None, description="Plan name or payer name")
    claim_number: Optional[str] = Field(None, description="Claim reference number")
    admission_date: Optional[str] = Field(None, description="Hospital admission date if inpatient")
    discharge_date: Optional[str] = Field(None, description="Discharge date if inpatient")
    service_lines: list[ServiceLine] = Field(default_factory=list)
    total_billed: Optional[float] = Field(None, description="Total charges on the bill")
    total_insurance_paid: Optional[float] = Field(None, description="Total paid by insurance")
    total_patient_balance: Optional[float] = Field(None, description="Balance owed by patient")
    diagnosis_codes: list[str] = Field(
        default_factory=list,
        description="ICD-10 diagnosis codes present on the bill",
    )
    extraction_notes: Optional[str] = Field(
        None,
        description="Flags for ambiguous or missing data the analyst should review",
    )

    @field_validator("service_lines", "diagnosis_codes", mode="before")
    @classmethod
    def coerce_null_to_list(cls, v):
        return [] if v is None else v

    @field_validator(
        "provider_name", "provider_npi", "provider_address",
        "patient_name", "patient_dob", "patient_account_number",
        "insurance_id", "insurance_plan", "claim_number",
        "admission_date", "discharge_date", "extraction_notes",
        mode="before",
    )
    @classmethod
    def coerce_list_to_str(cls, v):
        if isinstance(v, list):
            return "\n".join(str(item) for item in v if item is not None)
        return v


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a medical billing extraction specialist with expertise in CMS-1500, \
UB-04, and Explanation of Benefits (EOB) documents.

Your task is to extract every billable detail from the raw text of a medical bill and return it \
as structured JSON matching the schema provided.

EXTRACTION RULES
────────────────
1. **Completeness first.** Extract every service line visible in the document, including lines \
   with $0 billed, denied lines, and lines showing only adjustments.

2. **Currency.** All monetary values must be floating-point USD. Strip $ signs, commas, and \
   parentheses (parentheses denote negative / credit amounts — store as negative floats).

3. **Dates.** Normalise all dates to MM/DD/YYYY. If a year is implied from context, include it.

4. **CPT / HCPCS codes.** Preserve the exact 5-character alphanumeric code. Do not strip or \
   expand modifiers (e.g. "99213-25" → cpt_code = "99213", description should note modifier 25).

5. **Missing fields.** If a field is genuinely absent from the document, return null — do NOT \
   invent or estimate values.

6. **Ambiguity.** If a value appears more than once with different amounts, extract the most \
   recent (or highest-level summary) and note the discrepancy in extraction_notes.

7. **Diagnosis codes.** List all ICD-10 codes found; they are usually in a "Diagnosis" box or \
   listed as "Dx:" near service lines.

8. **Payment summary blocks.** Bills often contain a separate summary section (variously labelled \
   "Payment Summary", "Account Summary", "Statement Summary", "Amount Due", or similar) that is \
   NOT part of the line-item table. Always scan the full document for this block and map its \
   values as follows:
   - "Total Charges" / "Total Billed" / "Gross Charges"  → total_billed
   - "Insurance Paid" / "Insurance Payment" / "Plan Paid" → total_insurance_paid
   - "Adjustments" / "Contractual Adjustment"             → note in extraction_notes (no schema field)
   - "Balance Due" / "Patient Balance" / "Amount Due" / "You Owe" → total_patient_balance

   These values may appear as a small box, a footer table, a bulleted list, or a paragraph — \
   look for them everywhere, not just in the service-line table.

   EXAMPLE A — tabular summary block:
   Document text:
     Payment Summary
     Total Charges       $1,010.00
     Insurance Payment     $450.00
     Adjustments           $150.00
     Balance Due           $410.00
   Correct extraction:
     "total_billed": 1010.00,
     "total_insurance_paid": 450.00,
     "total_patient_balance": 410.00,
     "extraction_notes": "Contractual adjustment of $150.00 noted in Payment Summary."

   EXAMPLE B — inline / paragraph summary:
   Document text:
     Your total charges were $3,240.00. Your insurance paid $2,100.00. After a contractual
     adjustment of $640.00, your balance due is $500.00.
   Correct extraction:
     "total_billed": 3240.00,
     "total_insurance_paid": 2100.00,
     "total_patient_balance": 500.00,
     "extraction_notes": "Contractual adjustment of $640.00 noted inline in summary paragraph."

9. **Totals cross-check.** After extracting line items, verify that the sum of \
   billed_amounts equals total_billed. If there is a mismatch, note it in extraction_notes.

10. **No hallucination.** Every value you return must be traceable to the input text. If the \
    document is unclear, say so in extraction_notes rather than guess.

11. **Output.** Return ONLY the JSON object conforming to the schema. No prose, no markdown \
    fences, no commentary outside the JSON.
"""

# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def extract_bill(raw_text: str, *, client: anthropic.Anthropic | None = None) -> BillExtraction:
    """
    Extract structured billing data from raw medical bill text.

    Args:
        raw_text: Plain text of the bill (from pdfplumber or OCR).
        client:   Optional pre-initialised Anthropic client (useful for DI / testing).

    Returns:
        A validated BillExtraction instance.

    Raises:
        anthropic.APIError:       On API-level failures.
        pydantic.ValidationError: If the model returns malformed JSON.
    """
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract all billing information from the following medical bill text.\n\n"
                    "--- BEGIN BILL TEXT ---\n"
                    f"{raw_text}\n"
                    "--- END BILL TEXT ---"
                ),
            }
        ],
    )

    raw_json = response.content[0].text.strip()
    # Strip markdown code fences if the model wrapped the JSON
    if raw_json.startswith("```"):
        raw_json = raw_json.split("\n", 1)[-1]
        raw_json = raw_json.rsplit("```", 1)[0].strip()
    return BillExtraction.model_validate_json(raw_json)


# ---------------------------------------------------------------------------
# CLI helper (python -m prompts.extract < bill.txt)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    raw = sys.stdin.read()
    if not raw.strip():
        print("Usage: python -m prompts.extract < bill.txt", file=sys.stderr)
        sys.exit(1)

    result = extract_bill(raw)
    print(result.model_dump_json(indent=2))
