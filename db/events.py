"""
Event append and query helpers for the events table.

All functions take an open sqlite3.Connection and do NOT commit — callers
are responsible for transaction management. This lets callers bundle event
writes into their own transactions (e.g. the MCP server writing a tool_result
alongside a facts row and mutation_log entry in one BEGIN IMMEDIATE).
"""

import json
import sqlite3
from datetime import datetime, timezone


def append(
    conn: sqlite3.Connection,
    type: str,
    payload: dict,
    turn_id: str,
    conversation_id: str,
    parent_event_id: int | None = None,
) -> int:
    """Insert one event row within the caller's open connection/transaction.

    Sets correlation_id = turn_id (aliased per spec — the two concepts are
    identical in the current single-bot single-MCP-server architecture).

    Returns the new event id. Does NOT commit.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    correlation_id = turn_id
    cursor = conn.execute(
        """
        INSERT INTO events
            (timestamp, turn_id, conversation_id, correlation_id, type, parent_event_id, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (timestamp, turn_id, conversation_id, correlation_id, type, parent_event_id, json.dumps(payload)),
    )
    return cursor.lastrowid


def get_tool_result_event_id(conn: sqlite3.Connection, tool_use_id: str) -> int | None:
    """Return the event id of the most recent tool_result event matching tool_use_id, or None."""
    row = conn.execute(
        """
        SELECT id FROM events
        WHERE type = 'tool_result'
          AND json_extract(payload, '$.tool_use_id') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (tool_use_id,),
    ).fetchone()
    return row[0] if row else None


def walk_causality(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    """Return all events in a turn ordered by id, with enough fields to inspect the causality tree."""
    rows = conn.execute(
        """
        SELECT id, type, parent_event_id, timestamp
        FROM events
        WHERE turn_id = ?
        ORDER BY id ASC
        """,
        (turn_id,),
    ).fetchall()
    return [dict(row) for row in rows]
