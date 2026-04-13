"""
prompts/dispute.py

Stage 3 — Professional dispute letter generation.

Input:
    errors       — List[BillingError] from prompts/analyze.py
    patient_info — PatientInfo dataclass with contact details
    bill_data    — Optional BillExtraction for header context

Output:
    List[DisputeLetter] — one letter per error, each as a formatted string
    ready to print, e-mail, or send via certified mail

Usage:
    from prompts.analyze import analyze_bill, BillingError
    from prompts.dispute import generate_dispute_letters, PatientInfo

    errors = analyze_bill(extraction, eob_text=eob)
    letters = generate_dispute_letters(
        errors.errors,
        patient_info=PatientInfo(
            full_name="Jane Doe",
            address="123 Main St, Springfield, IL 60001",
            phone="(555) 123-4567",
            email="jane.doe@email.com",
            date_of_birth="01/15/1975",
            insurance_id="XYZ-123456789",
        ),
        bill_data=extraction,
    )
    for letter in letters:
        print(letter.letter_text)
        print("=" * 80)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from prompts.analyze import BillingError, ErrorType
from prompts.extract import BillExtraction

# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------

@dataclass
class PatientInfo:
    full_name: str
    address: str
    phone: str
    insurance_id: str
    date_of_birth: str = ""
    email: str = ""
    policy_group_number: str = ""


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class DisputeLetter(BaseModel):
    error_type: str = Field(description="ErrorType value of the error being disputed.")
    send_to: str = Field(
        description=(
            "Recommended recipient: 'insurance_appeals', 'provider_billing', "
            "'state_insurance_commissioner', or 'cms_ombudsman'."
        )
    )
    subject_line: str = Field(description="Subject line for the letter.")
    deadline_recommendation: str = Field(
        description=(
            "Plain-English deadline guidance, e.g. "
            "'Send within 180 days of the EOB date to preserve appeal rights.'"
        )
    )
    letter_text: str = Field(
        description=(
            "Full text of the dispute letter, including all headers, body paragraphs, "
            "and signature block. Ready to print without further editing."
        )
    )
    enclosures: list[str] = Field(
        default_factory=list,
        description="List of documents the patient should attach (e.g. 'Copy of EOB page 2').",
    )
    escalation_path: str = Field(
        description=(
            "If the first dispute fails, this is the next step "
            "(e.g. 'File external review with state DOI within 4 months')."
        )
    )


class DisputePackage(BaseModel):
    letters: list[DisputeLetter] = Field(default_factory=list)
    priority_order: list[str] = Field(
        default_factory=list,
        description=(
            "Subject lines sorted by recommended send order "
            "(highest-value / fastest-deadline letters first)."
        )
    )
    cover_note: str = Field(
        description=(
            "Short cover note (3–5 sentences) for the patient explaining "
            "the dispute package, what to send first, and the total potential recovery."
        )
    )


# ---------------------------------------------------------------------------
# Regulatory context map (injected into system prompt)
# ---------------------------------------------------------------------------

_REGULATORY_CONTEXT = {
    ErrorType.DUPLICATE_CHARGE: (
        "42 CFR §489.20 prohibits duplicate billing under Medicare. "
        "Most state insurance codes have equivalent provisions. "
        "CMS Form CMS-1450 instructions require each service to appear once per claim."
    ),
    ErrorType.UPCODING: (
        "Upcoding violates the False Claims Act (31 U.S.C. §3729) when involving "
        "government payers. For commercial plans, cite the payer's plan document and "
        "AMA CPT coding guidelines. Request itemised bill + medical records."
    ),
    ErrorType.UNBUNDLING: (
        "CMS National Correct Coding Initiative (NCCI) edits define bundled procedure "
        "pairs. Cite the specific NCCI edit number. "
        "For commercial payers cite AMA CPT bundling guidelines."
    ),
    ErrorType.DENIED_SHOULD_BE_COVERED: (
        "ERISA §502(a)(1)(B) grants the right to appeal benefit denials for employer plans. "
        "ACA §2719 requires internal and external appeal processes. "
        "State prompt-payment laws may impose penalties for unreasonable denials."
    ),
    ErrorType.BALANCE_BILLING: (
        "The No Surprises Act (NSA, effective Jan 1 2022, 45 CFR Part 149) prohibits "
        "surprise billing for emergency and certain non-emergency out-of-network services. "
        "Many states have additional balance-billing prohibitions for in-network providers."
    ),
    ErrorType.INCORRECT_QUANTITY: (
        "Request an itemised bill under the Hospital Patients' Bill of Rights. "
        "Compare units billed against the AMA CPT descriptor for time-based codes."
    ),
    ErrorType.CANCELLED_PROCEDURE: (
        "Billing for an uncompleted or cancelled procedure may constitute fraud. "
        "Request medical records to confirm the service was rendered."
    ),
    ErrorType.COORDINATION_OF_BENEFITS: (
        "COB rules are governed by state insurance regulations and the plan's COB "
        "provisions. The primary payer must adjudicate first; secondary payer covers "
        "remaining patient responsibility up to its own liability."
    ),
    ErrorType.WRONG_PATIENT: (
        "Request correction under HIPAA §164.526 (right to amend). "
        "Incorrect patient identifiers on a claim may constitute a billing error "
        "correctable via the provider's billing department."
    ),
    ErrorType.MEDICAL_NECESSITY_DISPUTE: (
        "Request a peer-to-peer review between your physician and the plan's medical "
        "director. Under ERISA and state laws, the plan must provide the criteria it used "
        "to determine medical necessity and allow submission of supporting clinical records."
    ),
    ErrorType.OTHER: (
        "Cite the plan's Summary Plan Description (SPD) or Evidence of Coverage (EOC) "
        "and any applicable state insurance regulations."
    ),
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(regulatory_map: dict) -> str:
    reg_lines = "\n".join(
        f"  {k.value}: {v}" for k, v in regulatory_map.items()
    )
    return f"""You are a patient billing advocate and paralegal specialising in healthcare \
dispute resolution. You write clear, firm, professional dispute letters on behalf of patients \
that have been incorrectly billed.

LETTER WRITING PRINCIPLES
──────────────────────────
1. **Professional tone.** Assertive but not hostile. No accusatory language.
2. **Specificity.** Every factual claim must cite a date, CPT code, amount, or line item from \
   the bill or EOB. Vague complaints are easier to dismiss.
3. **Regulatory grounding.** Ground each dispute in the correct regulation, payer contract \
   provision, or CMS rule. Generic "this is wrong" letters are ignored.
4. **Clear ask.** State exactly what correction is requested: credit, refund, re-adjudication, \
   or corrected EOB.
5. **Deadline and rights.** Include applicable appeal deadlines and the patient's rights if the \
   dispute is denied.
6. **Enclosures.** List every document the patient should attach to strengthen the dispute.

LETTER STRUCTURE (mandatory)
─────────────────────────────
  [Patient name and address block]
  [Date]
  [Recipient name / department / address]

  Re: [Subject line — include patient name, member ID, claim/account number, service date]

  Dear [Recipient title]:

  Paragraph 1 — Identity + purpose (one sentence)
  Paragraph 2 — Specific error description with supporting data
  Paragraph 3 — Regulatory / contractual basis for the dispute
  Paragraph 4 — Requested correction with specific dollar amount or action
  Paragraph 5 — Response deadline and next steps if unresolved

  Sincerely,
  [Patient signature block]

  Enclosures: [list]

REGULATORY REFERENCE BY ERROR TYPE
────────────────────────────────────
{reg_lines}

SEND-TO ROUTING GUIDE
──────────────────────
  • Duplicate / unbundling / upcoding / quantity errors → provider_billing (billing dept)
  • Denied claims / medical necessity / COB → insurance_appeals (plan appeals dept)
  • Balance billing violations → may require both provider_billing AND state_insurance_commissioner
  • Wrong patient → provider_billing for correction + insurance_appeals for re-adjudication

OUTPUT REQUIREMENTS
───────────────────
Return ONLY valid JSON matching the DisputePackage schema.
The letter_text field must be complete — no "[insert X here]" placeholders.
Letters must be ordered in priority_order by (estimated_recovery DESC, deadline urgency DESC).
"""


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def generate_dispute_letters(
    errors: list[BillingError],
    patient_info: PatientInfo,
    bill_data: BillExtraction | None = None,
    *,
    client: anthropic.Anthropic | None = None,
) -> DisputePackage:
    """
    Generate professional dispute letters for each billing error.

    Args:
        errors:       List of BillingError from analyze_bill().
        patient_info: Patient contact and insurance details.
        bill_data:    Optional BillExtraction for provider address / claim numbers.
        client:       Optional pre-initialised Anthropic client.

    Returns:
        DisputePackage containing one DisputeLetter per error, a priority order,
        and a patient-facing cover note.
    """
    if not errors:
        return DisputePackage(
            letters=[],
            priority_order=[],
            cover_note="No billing errors were identified. No dispute letters are needed.",
        )

    if client is None:
        client = anthropic.Anthropic()

    system_prompt = _build_system_prompt(_REGULATORY_CONTEXT)

    # Build context block
    today = date.today().strftime("%B %d, %Y")
    patient_block = (
        f"Patient: {patient_info.full_name}\n"
        f"Address: {patient_info.address}\n"
        f"Phone: {patient_info.phone}\n"
        f"Email: {patient_info.email or 'Not provided'}\n"
        f"Date of Birth: {patient_info.date_of_birth or 'Not provided'}\n"
        f"Insurance Member ID: {patient_info.insurance_id}\n"
        f"Group Number: {patient_info.policy_group_number or 'Not provided'}\n"
        f"Letter Date: {today}"
    )

    bill_block = ""
    if bill_data:
        bill_block = (
            f"\nProvider: {bill_data.provider_name or 'Unknown'}\n"
            f"Provider Address: {bill_data.provider_address or 'Unknown'}\n"
            f"Claim/Account Number: {bill_data.claim_number or bill_data.patient_account_number or 'Unknown'}\n"
            f"Insurance Plan: {bill_data.insurance_plan or 'Unknown'}\n"
            f"Total Billed: ${bill_data.total_billed or 0:,.2f}\n"
            f"Patient Balance: ${bill_data.total_patient_balance or 0:,.2f}"
        )

    errors_json = json.dumps(
        [e.model_dump() for e in errors],
        indent=2,
        default=str,
    )

    user_message = (
        "Generate professional dispute letters for each of the following billing errors.\n\n"
        f"--- PATIENT INFORMATION ---\n{patient_block}{bill_block}\n\n"
        f"--- BILLING ERRORS (JSON) ---\n{errors_json}\n--- END ERRORS ---\n\n"
        "Write one complete, ready-to-send dispute letter per error. "
        "Use the patient's real name, address, and insurance ID in each letter. "
        "Do not use any placeholders — every field must be populated."
    )

    # Use streaming for potentially long letter generation
    full_text_parts: list[str] = []
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": DisputePackage.model_json_schema(),
            }
        },
    ) as stream:
        for chunk in stream.text_stream:
            full_text_parts.append(chunk)

    raw_json = "".join(full_text_parts)
    return DisputePackage.model_validate_json(raw_json)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(
            "Usage: python -m prompts.dispute analysis.json patient.json [bill.json]",
            file=sys.stderr,
        )
        sys.exit(1)

    import json as _json

    with open(sys.argv[1]) as f:
        from prompts.analyze import AnalysisResult
        analysis = AnalysisResult.model_validate_json(f.read())

    with open(sys.argv[2]) as f:
        pi_raw = _json.load(f)
        pi = PatientInfo(**pi_raw)

    bill = None
    if len(sys.argv) >= 4:
        with open(sys.argv[3]) as f:
            bill = BillExtraction.model_validate_json(f.read())

    package = generate_dispute_letters(analysis.errors, pi, bill)
    print(package.model_dump_json(indent=2))
