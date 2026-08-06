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
CREATE INDEX IF NOT EXISTS idx_events_turn_id
    ON events(turn_id);

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

CREATE TABLE IF NOT EXISTS context_bound (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    from_event_id INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO context_bound (id, from_event_id) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    area             TEXT    NOT NULL,
    summary          TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open',
    priority         INTEGER,
    estimated_hours  REAL,
    detail_json      TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item       TEXT    NOT NULL,
    task_id    INTEGER REFERENCES tasks(id),
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS symptoms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symptom     TEXT    NOT NULL,
    severity    INTEGER,
    occurred_at TEXT    NOT NULL,
    notes       TEXT,
    logged_at   TEXT    NOT NULL
);
"""


def _migrate(conn) -> None:
    """Apply schema migrations to existing databases."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # Drop legacy facts table.
    if "facts" in tables:
        conn.execute("DROP TABLE facts")
        tables.discard("facts")

    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "conversation_id" in cols:
        conn.execute("DROP INDEX IF EXISTS idx_events_conv_ts")
        conn.execute("DROP INDEX IF EXISTS idx_events_conversation_timestamp")
        conn.execute("ALTER TABLE events DROP COLUMN conversation_id")
    if "correlation_id" in cols:
        conn.execute("ALTER TABLE events DROP COLUMN correlation_id")

    # Migrate: create context_bound table if it doesn't exist yet.
    if "context_bound" not in tables:
        conn.execute(
            "CREATE TABLE context_bound ("
            "    id           INTEGER PRIMARY KEY CHECK (id = 1),"
            "    from_event_id INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute("INSERT INTO context_bound (id, from_event_id) VALUES (1, 0)")

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)").fetchall()}
    if "idx_events_turn_id" not in indexes:
        conn.execute("CREATE INDEX idx_events_turn_id ON events(turn_id)")

    # Migrate: create tasks and shopping tables if they don't exist yet.
    if "tasks" not in tables:
        conn.execute(
            "CREATE TABLE tasks ("
            "    id               INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    area             TEXT    NOT NULL,"
            "    summary          TEXT    NOT NULL,"
            "    status           TEXT    NOT NULL DEFAULT 'open',"
            "    priority         INTEGER,"
            "    estimated_hours  REAL,"
            "    detail_json      TEXT,"
            "    created_at       TEXT    NOT NULL,"
            "    updated_at       TEXT    NOT NULL"
            ")"
        )
    if "shopping" not in tables:
        conn.execute(
            "CREATE TABLE shopping ("
            "    id         INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    item       TEXT    NOT NULL,"
            "    task_id    INTEGER REFERENCES tasks(id),"
            "    created_at TEXT    NOT NULL"
            ")"
        )

    if "symptoms" not in tables:
        conn.execute(
            "CREATE TABLE symptoms ("
            "    id          INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    symptom     TEXT    NOT NULL,"
            "    severity    INTEGER,"
            "    occurred_at TEXT    NOT NULL,"
            "    notes       TEXT,"
            "    logged_at   TEXT    NOT NULL"
            ")"
        )


def init_db() -> None:
    """Create all tables and triggers if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
