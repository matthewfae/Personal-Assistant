"""
Data access layer for the facts table.

All functions return plain dicts (or lists of dicts). No MCP types, no
string formatting for Claude — that belongs in the tools layer.

A fact is identified by (category, key). Adding a fact that already exists
under that pair updates it in place (upsert semantics).
"""

from datetime import datetime, timezone

from db.connection import get_db
from db import mutation_log


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_fact(key: str, value: str, category: str = "general") -> dict:
    """
    Insert a new fact or update the value if (category, key) already exists.

    Returns the stored fact as a dict with keys:
        id, category, key, value, created_at, updated_at, operation
    where operation is 'inserted' or 'updated'.
    """
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, value, created_at FROM facts WHERE category = ? AND key = ?",
            (category, key),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE facts SET value = ?, updated_at = ? WHERE id = ?",
                (value, now, existing["id"]),
            )
            mutation_log.log(
                conn, "facts", "UPDATE", existing["id"],
                {"category": category, "key": key,
                 "old_value": existing["value"], "new_value": value},
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
            mutation_log.log(
                conn, "facts", "INSERT", cur.lastrowid,
                {"category": category, "key": key, "value": value},
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


def get_fact(key: str, category: str = "general") -> dict | None:
    """
    Return the fact for (category, key) as a dict, or None if not found.

    Dict keys: id, category, key, value, created_at, updated_at
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, category, key, value, created_at, updated_at"
            " FROM facts WHERE category = ? AND key = ?",
            (category, key),
        ).fetchone()

    return dict(row) if row else None


def search_facts(query: str, category: str | None = None) -> list[dict]:
    """
    Return facts whose key or value contains `query` (case-insensitive).
    Optionally filter to a specific category. Results ordered by category, key.

    Each item has keys: id, category, key, value, created_at, updated_at
    """
    like = f"%{query}%"
    with get_db() as conn:
        if category:
            rows = conn.execute(
                "SELECT id, category, key, value, created_at, updated_at"
                " FROM facts WHERE category = ? AND (key LIKE ? OR value LIKE ?)"
                " ORDER BY category, key",
                (category, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, category, key, value, created_at, updated_at"
                " FROM facts WHERE key LIKE ? OR value LIKE ?"
                " ORDER BY category, key",
                (like, like),
            ).fetchall()

    return [dict(r) for r in rows]


def list_facts(category: str | None = None, limit: int = 20) -> list[dict]:
    """
    Return up to `limit` facts, most recently updated first.
    Optionally filter to a specific category.

    Each item has keys: id, category, key, value, created_at, updated_at
    """
    with get_db() as conn:
        if category:
            rows = conn.execute(
                "SELECT id, category, key, value, created_at, updated_at"
                " FROM facts WHERE category = ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, category, key, value, created_at, updated_at"
                " FROM facts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    return [dict(r) for r in rows]
