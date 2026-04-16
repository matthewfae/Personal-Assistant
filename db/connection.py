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

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    turn_id          TEXT    NOT NULL,
    type             TEXT    NOT NULL,
    parent_event_id  INTEGER REFERENCES events(id),
    payload          TEXT    NOT NULL   -- JSON blob, always includes {"v": 1, ...}
);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_parent
    ON events(parent_event_id);

CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(FAIL, 'events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
BEGIN
    SELECT RAISE(FAIL, 'events are immutable');
END;
"""


def _migrate(conn) -> None:
    """Apply schema migrations to existing databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "conversation_id" in cols:
        conn.execute("DROP INDEX IF EXISTS idx_events_conv_ts")
        conn.execute("DROP INDEX IF EXISTS idx_events_conversation_timestamp")
        conn.execute("ALTER TABLE events DROP COLUMN conversation_id")
    if "correlation_id" in cols:
        conn.execute("ALTER TABLE events DROP COLUMN correlation_id")


def init_db() -> None:
    """Create all tables and triggers if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
