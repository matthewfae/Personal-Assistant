"""
Read-only SQL query execution.

Runs Claude-written SQL on a connection opened in SQLite's read-only mode, so
the database itself refuses any write, whatever the statement. Returns rows as
plain dicts. No MCP types, no formatting — that lives in the tools layer.
"""

import logging
import sqlite3
from pathlib import Path

from db.connection import get_db_path

logger = logging.getLogger(__name__)

_MAX_ROWS = 200


def run_query(sql: str) -> list[dict]:
    """
    Execute one read-only statement and return up to _MAX_ROWS rows.

    Any SELECT, including WITH ... SELECT, works. Writes fail because the
    connection is read-only. Raises ValueError with SQLite's message for any
    failure (bad SQL, unknown column, attempted write, multiple statements).
    """
    uri = Path(get_db_path()).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchmany(_MAX_ROWS)
    except (sqlite3.Error, sqlite3.Warning) as e:
        # sqlite3.ProgrammingError (e.g. multiple statements) is a subclass of
        # sqlite3.Error; sqlite3.Warning is not, so it's listed separately.
        raise ValueError(f"Query failed: {e}") from e
    finally:
        conn.close()
    return [dict(row) for row in rows]
