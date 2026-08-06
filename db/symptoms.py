"""
Data access layer for the symptoms table.

All functions return plain dicts (or lists of dicts). No MCP types, no
string formatting for Claude -- that belongs in the tools layer.
"""

import logging
from datetime import datetime, timezone

from db.connection import get_db
from db import mutation_log

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_symptom(
    symptom: str,
    severity: int | None = None,
    occurred_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Insert a new symptom log. Returns a dict with all columns."""
    now = _now()
    if occurred_at is None:
        occurred_at = now
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO symptoms(symptom, severity, occurred_at, notes, logged_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (symptom, severity, occurred_at, notes, now),
        )
        mutation_log.log(
            conn, "symptoms", "INSERT", cur.lastrowid,
            {"symptom": symptom, "severity": severity, "occurred_at": occurred_at, "notes": notes},
        )
        return {
            "id": cur.lastrowid,
            "symptom": symptom,
            "severity": severity,
            "occurred_at": occurred_at,
            "notes": notes,
            "logged_at": now,
        }


_UPDATABLE_FIELDS = {"symptom", "severity", "occurred_at", "notes"}


def update_symptom(symptom_id: int, **fields) -> dict:
    """Update only the provided fields. Raises ValueError if not found. Returns updated dict."""
    to_set = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not to_set:
        raise ValueError("No valid fields to update")

    set_clause = ", ".join(f"{col} = ?" for col in to_set)
    values = list(to_set.values()) + [symptom_id]

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE symptoms SET {set_clause} WHERE id = ?",
            values,
        )
        if cur.rowcount == 0:
            raise ValueError(f"Symptom #{symptom_id} not found")
        mutation_log.log(conn, "symptoms", "UPDATE", symptom_id, to_set)
        row = conn.execute(
            "SELECT id, symptom, severity, occurred_at, notes, logged_at FROM symptoms WHERE id = ?",
            (symptom_id,),
        ).fetchone()
    return dict(row)


def get_symptom(symptom_id: int) -> dict | None:
    """Return full dict or None if not found."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, symptom, severity, occurred_at, notes, logged_at FROM symptoms WHERE id = ?",
            (symptom_id,),
        ).fetchone()
    return dict(row) if row else None


def list_symptoms(
    symptom: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """
    Return all columns for matching symptom logs, ordered by occurred_at DESC.
    'symptom' is a case-insensitive substring match.
    'since' is an ISO-8601 timestamp string (inclusive lower bound on occurred_at).
    """
    clauses = []
    params: list = []

    if symptom is not None:
        clauses.append("LOWER(symptom) LIKE LOWER(?)")
        params.append(f"%{symptom}%")
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(since)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, symptom, severity, occurred_at, notes, logged_at"
            f" FROM symptoms {where}"
            f" ORDER BY occurred_at DESC"
            f" LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def delete_symptom(symptom_id: int) -> bool:
    """Delete a symptom log. Returns True if deleted, False if not found."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM symptoms WHERE id = ?", (symptom_id,))
        if cur.rowcount == 0:
            return False
        mutation_log.log(conn, "symptoms", "DELETE", symptom_id, {})
    return True
