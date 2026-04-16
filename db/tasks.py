"""
Data access layer for the tasks table.

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


def add_task(
    area: str,
    summary: str,
    priority: int | None = None,
    estimated_hours: float | None = None,
    detail_json: str | None = None,
    status: str = "open",
) -> dict:
    """
    Insert a new task. Returns a dict with all columns.
    """
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (area, summary, status, priority, estimated_hours, detail_json, now, now),
        )
        mutation_log.log(
            conn, "tasks", "INSERT", cur.lastrowid,
            {"area": area, "summary": summary, "status": status,
             "priority": priority, "estimated_hours": estimated_hours,
             "detail_json": detail_json},
        )
        return {
            "id": cur.lastrowid,
            "area": area,
            "summary": summary,
            "status": status,
            "priority": priority,
            "estimated_hours": estimated_hours,
            "detail_json": detail_json,
            "created_at": now,
            "updated_at": now,
        }


_UPDATABLE_FIELDS = {"area", "summary", "status", "priority", "estimated_hours", "detail_json"}


def update_task(task_id: int, **fields) -> dict:
    """
    Update only the provided fields on a task. Updates updated_at automatically.
    Raises ValueError if task_id not found.
    Returns the updated full dict.
    """
    to_set = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not to_set:
        raise ValueError("No valid fields to update")

    now = _now()
    to_set["updated_at"] = now

    set_clause = ", ".join(f"{col} = ?" for col in to_set)
    values = list(to_set.values()) + [task_id]

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            values,
        )
        if cur.rowcount == 0:
            raise ValueError(f"Task #{task_id} not found")

        mutation_log.log(
            conn, "tasks", "UPDATE", task_id, to_set,
        )

        row = conn.execute(
            "SELECT id, area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at"
            " FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    return dict(row)


def get_task(task_id: int) -> dict | None:
    """Return full dict with all columns, or None if not found."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at"
            " FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    return dict(row) if row else None


def list_tasks(status: str | None = None, area: str | None = None) -> list[dict]:
    """
    Return compact dicts: id, area, summary, status, priority.
    Default: all open tasks. Order by priority ASC NULLS LAST, then created_at ASC.
    """
    clauses = []
    params: list = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    else:
        clauses.append("status = 'open'")

    if area is not None:
        clauses.append("area = ?")
        params.append(area)

    where = " AND ".join(clauses)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, area, summary, status, priority"
            f" FROM tasks WHERE {where}"
            " ORDER BY CASE WHEN priority IS NULL THEN 1 ELSE 0 END, priority ASC, created_at ASC",
            params,
        ).fetchall()

    return [dict(r) for r in rows]
