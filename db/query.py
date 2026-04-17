"""
Read-only SQL query execution against the database.

Only SELECT statements are permitted. Results are capped at 200 rows to
avoid flooding the context window.
"""

import re

from db.connection import get_db

_MAX_ROWS = 200
_ALLOWED = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


def run_query(sql: str) -> list[dict]:
    """Execute a SELECT query and return rows as dicts. Raises ValueError for non-SELECT."""
    if not _ALLOWED.match(sql):
        raise ValueError("Only SELECT queries are permitted.")
    with get_db() as conn:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(_MAX_ROWS)
        return [dict(zip(columns, row)) for row in rows]
