"""
Data access layer for the facts table.

All functions return plain dicts (or lists of dicts). No MCP types, no
string formatting for Claude — that belongs in the tools layer.

A fact is identified by (category, key). Adding a fact that already exists
under that pair updates it in place (upsert semantics).
"""

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_fact(conn: sqlite3.Connection, key: str, value: str, category: str = "general") -> dict:
    """
    Insert a new fact or update the value if (category, key) already exists.
    Operates on the caller-supplied open connection. Does NOT commit.

    Returns the stored fact as a dict with keys:
        id, category, key, value, created_at, updated_at, operation
    where operation is 'inserted' or 'updated'.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id, value, created_at FROM facts WHERE category = ? AND key = ?",
        (category, key),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE facts SET value = ?, updated_at = ? WHERE id = ?",
            (value, now, existing["id"]),
        )
        return {
            "id": existing["id"],
            "category": category,
            "key": key,
            "value": value,
            "created_at": existing["created_at"],
            "updated_at": now,
            "operation": "updated",
        }
    else:
        cur = conn.execute(
            "INSERT INTO facts(category, key, value, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (category, key, value, now, now),
        )
        return {
            "id": cur.lastrowid,
            "category": category,
            "key": key,
            "value": value,
            "created_at": now,
            "updated_at": now,
            "operation": "inserted",
        }
