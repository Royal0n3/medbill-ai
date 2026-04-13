"""
app/db.py

SQLite schema and per-request connection helpers.

Tables
------
bills     — raw text + extracted JSON for each uploaded bill
errors    — full AnalysisResult JSON for each audit run
disputes  — full DisputePackage JSON for each dispute generation run

Usage (inside a Flask route)
-----------------------------
    from app.db import get_db

    db = get_db()
    db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, current_app, g

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bills (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    file_type       TEXT NOT NULL CHECK (file_type IN ('pdf', 'txt')),
    raw_text        TEXT NOT NULL,
    extracted_json  TEXT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS errors (
    id                       TEXT PRIMARY KEY,
    bill_id                  TEXT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    eob_provided             INTEGER NOT NULL DEFAULT 0,
    analysis_json            TEXT NOT NULL,
    total_estimated_recovery REAL NOT NULL DEFAULT 0.0,
    error_count              INTEGER NOT NULL DEFAULT 0,
    created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS disputes (
    id           TEXT PRIMARY KEY,
    bill_id      TEXT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    dispute_json TEXT NOT NULL,
    letter_count INTEGER NOT NULL DEFAULT 0,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_errors_bill_id    ON errors(bill_id);
CREATE INDEX IF NOT EXISTS idx_disputes_bill_id  ON disputes(bill_id);
"""

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    """
    Return the per-request SQLite connection, opening it on first access.
    Must be called from within a Flask application context.
    """
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc: BaseException | None = None) -> None:
    """Teardown handler — close the connection at the end of each request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_db(app: Flask) -> None:
    """
    Create all tables if they don't exist, then register the teardown hook.
    Call once from the app factory — safe to call on every startup.
    """
    db_path = app.config["DATABASE"]
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Use a direct connection here (no app context yet when called from factory)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
