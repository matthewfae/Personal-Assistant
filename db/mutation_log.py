"""
Mutation log writer.

Called by db/facts.py after every write. Takes an open connection so
the log entry is part of the same transaction as the mutation.
"""

import json
import sqlite3
from datetime import datetime, timezone


def log(
    conn: sqlite3.Connection,
    table_name: str,
    operation: str,
    record_id: int,
    data: dict,
) -> None:
    """Append one entry to mutation_log within the caller's transaction."""
    conn.execute(
        "INSERT INTO mutation_log(table_name, operation, record_id, data_json, timestamp)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            table_name,
            operation,
            record_id,
            json.dumps(data),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
