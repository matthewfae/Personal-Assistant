"""
Read-only SQL query execution.

Validates that only SELECT statements are run, then executes against the DB
and returns rows as plain dicts. No MCP types, no formatting — that lives in
the tools layer.
"""

import logging

from db.connection import get_db

logger = logging.getLogger(__name__)

_MAX_ROWS = 200


def run_query(sql: str) -> list[dict]:
    """
    Execute a read-only SELECT statement and return up to _MAX_ROWS rows.

    Raises ValueError if the statement is not a SELECT or contains semicolons
    (to prevent stacked statements).
    """
    sql = sql.strip()
    if not sql.upper().startswith("SELECT"):
        raise ValueError("Only SELECT statements are allowed.")
    if ";" in sql:
        raise ValueError("Semicolons are not allowed.")
    with get_db() as conn:
        rows = conn.execute(sql).fetchmany(_MAX_ROWS)
    return [dict(row) for row in rows]
