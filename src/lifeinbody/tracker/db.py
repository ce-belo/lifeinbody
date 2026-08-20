"""SQLite connection + schema init."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import DB_PATH, ensure_dirs

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults and the schema applied."""
    ensure_dirs()
    path = Path(db_path) if db_path is not None else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def thread_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]


def followup_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM threads WHERE needs_followup = 1 AND status != 'closed'"
    ).fetchone()[0]


def invoice_mention_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'mentioned'").fetchone()[0]
