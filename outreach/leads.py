"""
outreach/leads.py

Lead management for the cold-outreach sequence.

Functions
---------
load_leads(csv_path)        — Read CSV → list of lead dicts
enroll_lead(email, name, practice)
                            — Insert into outreach.db, send Email 1 immediately
run_enrollment(csv_path)    — Batch-enroll every lead in the CSV
run_followups()             — Send any due follow-up emails (Day 3 / 7 / 12)

CSV format (header row required)
---------------------------------
name,practice,email,phone

CLI
---
    # Enroll leads from a CSV file
    python -m outreach.leads enroll path/to/leads.csv

    # Send any follow-ups that are due today (run daily via cron)
    python -m outreach.leads followup

Environment variables required
-------------------------------
    BREVO_API_KEY           — Brevo secret key (xkeysib-...)
    OUTREACH_SENDER_EMAIL   — From address verified in Brevo
    OUTREACH_SENDER_NAME    — Display name for outgoing emails
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from outreach.db import get_db
from outreach.sequence import SEQUENCE, make_client, send_email

load_dotenv()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_leads(csv_path: str | Path) -> list[dict[str, str]]:
    """
    Read a CSV file and return a list of lead dicts.

    Expected columns: name, practice, email, phone
    Extra columns are preserved but ignored by the enrollment functions.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        List of dicts, one per row.

    Raises:
        FileNotFoundError: If the file does not exist.
        KeyError:          If required columns are missing.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    leads: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        required = {"name", "email"}
        if reader.fieldnames is None:
            raise KeyError("CSV appears to be empty.")
        missing = required - {c.strip().lower() for c in reader.fieldnames}
        if missing:
            raise KeyError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            # Normalise keys to lowercase and strip whitespace
            leads.append({k.strip().lower(): v.strip() for k, v in row.items()})

    return leads


def enroll_lead(
    email: str,
    name: str,
    practice: str = "",
    *,
    api_key: str | None = None,
) -> bool:
    """
    Enroll a single lead into the sequence.

    Inserts a row into outreach.db (skipped if email already enrolled),
    then immediately sends Email 1 (step 0) via Brevo.

    Args:
        email:    Lead's email address.
        name:     Lead's full name.
        practice: Practice or company name (optional).
        api_key:  Brevo API key. Defaults to BREVO_API_KEY env var.

    Returns:
        True if the lead was newly enrolled and Email 1 was sent.
        False if the lead was already in the database (skipped).

    Raises:
        ValueError:    If BREVO_API_KEY is not set.
        ApiException:  On Brevo send failure.
    """
    key = api_key or os.environ.get("BREVO_API_KEY", "").strip()
    if not key:
        raise ValueError("BREVO_API_KEY is not set.")

    db = get_db()
    try:
        existing = db.execute(
            "SELECT email FROM enrollments WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            return False

        enrolled_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO enrollments (email, name, practice, enrolled_at, last_step_sent, completed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email, name, practice, enrolled_at, 0, 0),
        )
        db.commit()

        client = make_client(key)
        send_email(client, email, name, practice, step_index=0)

        return True
    finally:
        db.close()


def run_enrollment(csv_path: str | Path, *, api_key: str | None = None) -> dict[str, int]:
    """
    Batch-enroll all leads from a CSV file.

    Skips leads that are already enrolled. Continues past individual send
    failures, printing errors to stderr.

    Args:
        csv_path: Path to the CSV file.
        api_key:  Brevo API key. Defaults to BREVO_API_KEY env var.

    Returns:
        Dict with counts: {"enrolled": N, "skipped": N, "failed": N}
    """
    leads = load_leads(csv_path)
    counts = {"enrolled": 0, "skipped": 0, "failed": 0}

    for lead in leads:
        email = lead.get("email", "").strip()
        name = lead.get("name", "").strip()
        practice = lead.get("practice", "").strip()

        if not email:
            print(f"[SKIP] Row has no email: {lead}", file=sys.stderr)
            counts["skipped"] += 1
            continue

        try:
            enrolled = enroll_lead(email, name, practice, api_key=api_key)
            if enrolled:
                print(f"[OK]   Enrolled {email}")
                counts["enrolled"] += 1
            else:
                print(f"[SKIP] Already enrolled: {email}")
                counts["skipped"] += 1
        except Exception as exc:
            print(f"[FAIL] {email}: {exc}", file=sys.stderr)
            counts["failed"] += 1

    return counts


def run_followups(*, api_key: str | None = None) -> dict[str, int]:
    """
    Send any follow-up emails that are due today.

    Queries enrollments where:
      - completed = 0
      - the next step's day offset has elapsed since enrolled_at

    Advances last_step_sent and marks completed=1 after the final step.

    Args:
        api_key: Brevo API key. Defaults to BREVO_API_KEY env var.

    Returns:
        Dict with counts: {"sent": N, "failed": N}
    """
    key = api_key or os.environ.get("BREVO_API_KEY", "").strip()
    if not key:
        raise ValueError("BREVO_API_KEY is not set.")

    db = get_db()
    counts = {"sent": 0, "failed": 0}

    try:
        rows = db.execute(
            "SELECT email, name, practice, enrolled_at, last_step_sent "
            "FROM enrollments WHERE completed = 0"
        ).fetchall()

        now = datetime.now(timezone.utc)
        client = make_client(key)

        for row in rows:
            email = row["email"]
            name = row["name"]
            practice = row["practice"] or ""
            last_step = row["last_step_sent"]
            next_step = last_step + 1

            # All steps sent — shouldn't normally reach here but guard anyway
            if next_step >= len(SEQUENCE):
                db.execute(
                    "UPDATE enrollments SET completed = 1 WHERE email = ?", (email,)
                )
                db.commit()
                continue

            enrolled_at = datetime.fromisoformat(row["enrolled_at"])
            days_since = (now - enrolled_at).days
            required_days = SEQUENCE[next_step].day

            if days_since < required_days:
                continue  # Not due yet

            try:
                send_email(client, email, name, practice, step_index=next_step)

                is_last = next_step == len(SEQUENCE) - 1
                db.execute(
                    "UPDATE enrollments SET last_step_sent = ?, completed = ? WHERE email = ?",
                    (next_step, 1 if is_last else 0, email),
                )
                db.commit()

                status = "FINAL" if is_last else f"step {next_step + 1}/{len(SEQUENCE)}"
                print(f"[OK]   {email} — sent {status}")
                counts["sent"] += 1

            except Exception as exc:
                print(f"[FAIL] {email}: {exc}", file=sys.stderr)
                counts["failed"] += 1
    finally:
        db.close()

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _usage() -> None:
    print(
        "Usage:\n"
        "  python -m outreach.leads enroll <path/to/leads.csv>\n"
        "  python -m outreach.leads followup\n",
        file=sys.stderr,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "enroll":
        if len(sys.argv) < 3:
            print("Error: enroll requires a CSV path.", file=sys.stderr)
            _usage()
            sys.exit(1)

        result = run_enrollment(sys.argv[2])
        print(
            f"\nDone. Enrolled: {result['enrolled']}  "
            f"Skipped: {result['skipped']}  "
            f"Failed: {result['failed']}"
        )

    elif command == "followup":
        result = run_followups()
        print(f"\nDone. Sent: {result['sent']}  Failed: {result['failed']}")

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        _usage()
        sys.exit(1)
