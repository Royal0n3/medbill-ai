"""
outreach/db.py

SQLite state for the cold-outreach sequence.

Database file: outreach/outreach.db  (created automatically on first use)

Schema
------
enrollments
  email          TEXT PRIMARY KEY
  name           TEXT NOT NULL
  practice       TEXT
  enrolled_at    TEXT NOT NULL   -- ISO-8601 UTC datetime
  last_step_sent INTEGER NOT NULL DEFAULT 0
  completed      INTEGER NOT NULL DEFAULT 0   -- 1 once all 4 emails sent
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "outreach.db"

_DDL = """
CREATE TABLE IF NOT EXISTS enrollments (
    email          TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    practice       TEXT,
    enrolled_at    TEXT NOT NULL,
    last_step_sent INTEGER NOT NULL DEFAULT 0,
    completed      INTEGER NOT NULL DEFAULT 0
);
"""


def init_db(db_path: Path = _DB_PATH) -> None:
    """Create the database file and table if they don't exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_DDL)
        conn.commit()
    finally:
        conn.close()


def get_db(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """Return an open connection with row_factory set to sqlite3.Row."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
