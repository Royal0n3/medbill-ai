"""
outreach/sequence.py

4-email cold outreach sequence for independent medical practice owners/managers.

Each EmailStep defines:
  - day:      how many days after enrollment this email should go out
  - subject:  email subject line
  - body:     plain-text body; supports {name} and {practice} placeholders

Usage
-----
    from outreach.sequence import SEQUENCE, send_email
    import sib_api_v3_sdk

    config = sib_api_v3_sdk.Configuration()
    config.api_key["api-key"] = os.environ["BREVO_API_KEY"]
    client = sib_api_v3_sdk.ApiClient(config)

    send_email(client, "dr@example.com", "Dr. Smith", "Lakeside Family Practice", step_index=0)

Configuration
-------------
Set SENDER_EMAIL and SENDER_NAME below, or override via environment variables
OUTREACH_SENDER_EMAIL and OUTREACH_SENDER_NAME.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# ---------------------------------------------------------------------------
# Sender identity — override with env vars in production
# ---------------------------------------------------------------------------

SENDER_EMAIL: str = os.environ.get("OUTREACH_SENDER_EMAIL", "kameron@kamsaisystems.com")
SENDER_NAME: str = os.environ.get("OUTREACH_SENDER_NAME", "Kameron")


# ---------------------------------------------------------------------------
# Email step definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmailStep:
    day: int
    subject: str
    body: str


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------

SEQUENCE: list[EmailStep] = [
    # ------------------------------------------------------------------
    # Email 1 — Day 0 — The hook
    # ------------------------------------------------------------------
    EmailStep(
        day=0,
        subject="Quick question about your billing denials",
        body="""\
Hi {name},

I work with independent practices to find money that's quietly disappearing in the \
billing process — denied claims, underpayments, and contractual adjustments that \
never get appealed.

Most practice owners I talk to haven't looked closely at their EOBs in months, \
and it's usually costing them more than they'd expect.

Quick question: when did you last sit down and audit your EOBs line by line?

If it's been a while — or if you're not sure — I'd love to chat for 15 minutes. \
Happy to answer questions with no pitch attached.

Reply here or grab a time: https://calendly.com/YOURCALENDAR

{name_first},
{sender_name}\
""",
    ),

    # ------------------------------------------------------------------
    # Email 2 — Day 3 — The proof
    # ------------------------------------------------------------------
    EmailStep(
        day=3,
        subject="Found $4,200 in errors for a practice like yours",
        body="""\
Hi {name},

Following up on my note from a few days ago.

I recently audited the EOBs for a three-physician family practice — similar size \
to {practice}. In 90 days of claims we found:

  • $1,800 in duplicate charges that were never flagged by the payer
  • $1,400 in underpayments where the contracted rate wasn't applied correctly
  • $1,000 in denied claims still within the dispute window

Total recoverable: $4,200. Audit took about two hours of their time.

I offer a flat-rate EOB audit for $300. I go through 90 days of your claims, \
document every error I find, and give you a dispute-ready report. If I find \
nothing, you pay nothing.

Want me to run one for {practice}?

Just reply and we'll get it scheduled.

{sender_name}\
""",
    ),

    # ------------------------------------------------------------------
    # Email 3 — Day 7 — The urgency
    # ------------------------------------------------------------------
    EmailStep(
        day=7,
        subject="Most billing errors have a 12-month dispute window",
        body="""\
Hi {name},

One thing most practice managers don't realize: payers impose strict timely filing \
limits on claim disputes — typically 90 to 180 days from the remittance date, \
though some plans allow up to 12 months.

Once that window closes, the money is gone permanently. No appeal, no exception.

If {practice} has underpayments or wrongful denials from the last 6–12 months, \
the clock is already running.

A 90-day EOB audit takes less than a week to complete. If there's recoverable \
money, we find it before the deadline.

If this is something you want to look into, now is the right time.

Book a call: https://calendly.com/YOURCALENDAR
Or just reply — I'll walk you through the process.

{sender_name}\
""",
    ),

    # ------------------------------------------------------------------
    # Email 4 — Day 12 — The breakup
    # ------------------------------------------------------------------
    EmailStep(
        day=12,
        subject="Last note from me",
        body="""\
Hi {name},

I don't want to keep filling your inbox if the timing isn't right.

This is my last note.

If billing errors aren't a priority for {practice} right now, I completely \
understand — just reply with NO and I'll remove you from my list.

If you do want me to run that EOB audit, reply with YES and I'll send over \
the details. $300 flat, no-find no-fee.

Either way, I appreciate your time.

{sender_name}\
""",
    ),
]


# ---------------------------------------------------------------------------
# Send helper
# ---------------------------------------------------------------------------

def send_email(
    client: sib_api_v3_sdk.ApiClient,
    to_email: str,
    to_name: str,
    to_practice: str,
    step_index: int,
) -> None:
    """
    Send a single step of the sequence to one recipient.

    Args:
        client:      Configured sib_api_v3_sdk.ApiClient.
        to_email:    Recipient email address.
        to_name:     Recipient full name (used in {name} placeholder).
        to_practice: Practice name (used in {practice} placeholder).
        step_index:  0-based index into SEQUENCE (0 = Day 0 email).

    Raises:
        IndexError:    If step_index is out of range.
        ApiException:  On Brevo API errors.
    """
    step = SEQUENCE[step_index]

    name_first = to_name.split()[0] if to_name else to_name

    body = step.body.format(
        name=to_name,
        name_first=name_first,
        practice=to_practice or "your practice",
        sender_name=SENDER_NAME,
    )

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        sender={"name": SENDER_NAME, "email": SENDER_EMAIL},
        to=[{"name": to_name, "email": to_email}],
        subject=step.subject,
        text_content=body,
    )

    api = sib_api_v3_sdk.TransactionalEmailsApi(client)
    try:
        api.send_transac_email(send_smtp_email)
    except ApiException as exc:
        raise ApiException(
            f"Brevo API error sending step {step_index} to {to_email}: {exc}"
        ) from exc


def make_client(api_key: str) -> sib_api_v3_sdk.ApiClient:
    """Construct a configured Brevo API client from an API key."""
    config = sib_api_v3_sdk.Configuration()
    config.api_key["api-key"] = api_key
    return sib_api_v3_sdk.ApiClient(config)
