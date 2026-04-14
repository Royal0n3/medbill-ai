"""
prompts/analyze.py

Stage 2 — Error detection on extracted bill data vs. EOB.

Input:
    bill_data  — BillExtraction (or dict) produced by prompts/extract.py
    eob_text   — Raw text of the patient's Explanation of Benefits document
                 (can be empty string "" if unavailable)

Output:
    List[BillingError] — each error carries type, description,
    estimated_recovery_amount, and confidence_score

Usage:
    from prompts.extract import extract_bill
    from prompts.analyze import analyze_bill

    extraction = extract_bill(raw_bill_text)
    errors = analyze_bill(extraction, eob_text=raw_eob_text)
    for e in errors:
        print(e.model_dump_json(indent=2))
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from prompts.extract import BillExtraction

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ErrorType(str, Enum):
    DUPLICATE_CHARGE          = "duplicate_charge"
    UPCODING                  = "upcoding"
    UNBUNDLING                = "unbundling"
    DENIED_SHOULD_BE_COVERED  = "denied_should_be_covered"
    BALANCE_BILLING           = "balance_billing"
    INCORRECT_QUANTITY        = "incorrect_quantity"
    CANCELLED_PROCEDURE       = "cancelled_procedure"
    WRONG_PATIENT             = "wrong_patient"
    COORDINATION_OF_BENEFITS  = "coordination_of_benefits"
    MEDICAL_NECESSITY_DISPUTE = "medical_necessity_dispute"
    OTHER                     = "other"


class BillingError(BaseModel):
    error_type: ErrorType = Field(
        description="Classification of the billing error."
    )
    description: str = Field(
        description=(
            "Plain-English explanation of the error. Include the specific CPT codes, "
            "dates, amounts, or policy language that support the finding."
        )
    )
    affected_cpt_codes: list[str] = Field(
        default_factory=list,
        description="CPT/HCPCS codes directly implicated in this error.",
    )
    affected_dates: list[str] = Field(
        default_factory=list,
        description="Dates of service relevant to this error.",
    )
    billed_amount: Optional[float] = Field(
        None, description="Amount billed for the disputed line(s) (USD)."
    )
    estimated_recovery_amount: float = Field(
        description=(
            "Conservative estimate of the dollar amount the patient may recover or "
            "avoid paying if this error is corrected (USD). Use 0 if undeterminable."
        )
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "0.0–1.0 confidence that this is a genuine billing error. "
            "1.0 = near-certain (e.g. exact duplicate); "
            "0.5 = plausible but requires provider clarification."
        ),
    )
    regulatory_basis: Optional[str] = Field(
        None,
        description=(
            "Applicable regulation, CMS rule, or payer contract clause that supports "
            "this finding (e.g. 'CMS NCCI edit 99213+99214 same day', "
            "'ERISA §502(a)', 'ACA §2719A balance billing prohibition')."
        ),
    )
    supporting_evidence: str = Field(
        description=(
            "Specific data from the bill or EOB that supports this finding. "
            "Quote line-item amounts, denial codes, or EOB remarks directly."
        )
    )


class AnalysisResult(BaseModel):
    errors: list[BillingError] = Field(default_factory=list)
    total_estimated_recovery: float = Field(
        description="Sum of estimated_recovery_amount across all errors (USD)."
    )
    analysis_summary: str = Field(
        description=(
            "2–4 sentence plain-English summary of findings suitable for the patient. "
            "Do not use medical jargon."
        )
    )
    eob_comparison_possible: bool = Field(
        description="True if an EOB was provided and line-level comparison was performed."
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior medical billing auditor and patient advocate with 15 years \
of experience auditing hospital and physician billing under Medicare, Medicaid, and commercial \
insurance payer contracts. You have deep knowledge of:

  • CPT and HCPCS coding rules (AMA CPT manual, CMS NCCI edits)
  • ICD-10-CM/PCS diagnosis and procedure coding
  • UB-04 revenue codes and CMS-1500 claim form requirements
  • ERISA, ACA, and state balance-billing prohibitions
  • Explanation of Benefits (EOB) interpretation
  • Common fraud, waste, and abuse patterns (OIG Work Plan categories)

Your task is to perform a comprehensive billing audit by comparing the extracted bill data \
against the EOB (if provided) and your coding knowledge.

AUDIT CATEGORIES TO CHECK
──────────────────────────
1. DUPLICATE CHARGES
   • Identical CPT code + same date of service appearing more than once
   • Same procedure billed under two different revenue codes on a UB-04
   • Charges that match an already-paid line on the EOB

2. UPCODING
   • E/M level (99202–99215) inconsistent with documented diagnosis complexity
   • Facility bill for higher-acuity setting than the procedure requires
   • Modifier abuse (e.g. modifier 22 on a routine procedure)

3. UNBUNDLING VIOLATIONS
   • Separate billing of components that CMS NCCI bundles into a comprehensive code
   • Bilateral procedure billed twice instead of using modifier 50
   • Pre/post-operative services billed separately during the global period

4. DENIED CLAIMS THAT SHOULD BE COVERED
   • Denial codes CO-4, CO-11, CO-97 where the clinical documentation supports coverage
   • Prior-authorisation denials where the plan's own criteria appear to be met
   • Timely-filing denials where the error was the payer's, not the provider's

5. BALANCE BILLING ERRORS
   • In-network provider billing over the contracted rate
   • Facility fees billed to the patient when the payer already reimbursed at 100%
   • Surprise billing violations (No Surprises Act / state equivalents)

6. QUANTITY / UNIT ERRORS
   • Units billed that exceed clinical norms (e.g. 10 units of a single-use supply)
   • Time-based codes billed for durations inconsistent with a realistic visit

7. OTHER COMMON ERRORS
   • Charges for services the patient can document were not rendered
   • Wrong patient / wrong date (data entry errors)
   • Coordination-of-benefits errors when the patient has dual coverage

CONFIDENCE SCORING GUIDE
─────────────────────────
  0.9–1.0  Objective rule violation (NCCI edit, exact duplicate, payer already paid 100%)
  0.7–0.89 Strong evidence in EOB/bill but requires provider records to confirm
  0.5–0.69 Plausible error; clinical documentation or clarification needed
  < 0.5    Flag only if recovery potential is high enough to warrant investigation

OUTPUT REQUIREMENTS
───────────────────
• Return ONLY valid JSON matching the AnalysisResult schema.
• Do not include errors with confidence < 0.5 unless estimated_recovery > $500.
• Sort errors by estimated_recovery_amount descending.
• Every error MUST include supporting_evidence quoting specific data from the input.
• total_estimated_recovery must equal the sum of all errors' estimated_recovery_amount.
• No hallucination: do not invent errors that are not supported by the provided data.
"""

# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def analyze_bill(
    bill_data: BillExtraction | dict,
    eob_text: str = "",
    *,
    client: anthropic.Anthropic | None = None,
) -> AnalysisResult:
    """
    Audit an extracted bill against the EOB and CPT coding rules.

    Args:
        bill_data: BillExtraction instance (or dict) from extract.py.
        eob_text:  Raw EOB text. Pass "" if unavailable.
        client:    Optional pre-initialised Anthropic client.

    Returns:
        AnalysisResult with a list of BillingError findings.
    """
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        client = anthropic.Anthropic(api_key=api_key)

    if isinstance(bill_data, BillExtraction):
        bill_json = bill_data.model_dump_json(indent=2)
    else:
        bill_json = json.dumps(bill_data, indent=2)

    eob_section = (
        f"--- EOB TEXT ---\n{eob_text}\n--- END EOB TEXT ---"
        if eob_text.strip()
        else "No EOB provided. Audit based on bill data and coding rules alone."
    )

    user_message = (
        "Perform a full billing audit on the following extracted bill data and EOB.\n\n"
        f"--- EXTRACTED BILL DATA (JSON) ---\n{bill_json}\n"
        "--- END BILL DATA ---\n\n"
        f"{eob_section}"
    )

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_json = response.content[0].text.strip()
    # Strip markdown code fences if the model wrapped the JSON
    if raw_json.startswith("```"):
        raw_json = raw_json.split("\n", 1)[-1]
        raw_json = raw_json.rsplit("```", 1)[0].strip()
    return AnalysisResult.model_validate_json(raw_json)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python -m prompts.analyze bill_extraction.json [eob.txt]",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(sys.argv[1]) as f:
        bill = BillExtraction.model_validate_json(f.read())

    eob = ""
    if len(sys.argv) >= 3:
        with open(sys.argv[2]) as f:
            eob = f.read()

    result = analyze_bill(bill, eob_text=eob)
    print(result.model_dump_json(indent=2))
