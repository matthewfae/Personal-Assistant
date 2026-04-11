"""
Database connection management and schema initialization.

DB_PATH is read from the environment (via .env). Call init_db() once at
startup. Use get_db() as a context manager for all queries — commits on
clean exit, rolls back on exception.
"""

import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

_DB_PATH: str = os.environ.get("DB_PATH", "personal_assistant.db")


def get_db_path() -> str:
    return _DB_PATH


@contextmanager
def get_db():
    """Yield a sqlite3 connection. Commits on clean exit, rolls back on error."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT    NOT NULL DEFAULT 'general',
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS mutation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT    NOT NULL,
    operation   TEXT    NOT NULL,
    record_id   INTEGER,
    data_json   TEXT,
    timestamp   TEXT    NOT NULL
);
"""


def init_db() -> None:
    """Create all tables and triggers if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
