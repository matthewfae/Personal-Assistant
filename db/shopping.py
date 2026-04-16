"""
Data access layer for the shopping table.

All functions return plain dicts (or lists). No MCP types, no
string formatting for Claude -- that belongs in the tools layer.
"""

from datetime import datetime, timezone

from db.connection import get_db
from db import mutation_log


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_shopping(item: str, task_id: int | None = None) -> dict:
    """Insert a new shopping item. Returns dict with id, item, task_id, created_at."""
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO shopping(item, task_id, created_at) VALUES (?, ?, ?)",
            (item, task_id, now),
        )
        mutation_log.log(
            conn, "shopping", "INSERT", cur.lastrowid,
            {"item": item, "task_id": task_id},
        )
        return {
            "id": cur.lastrowid,
            "item": item,
            "task_id": task_id,
            "created_at": now,
        }


def list_shopping() -> list[str]:
    """Return just item name strings, ordered by created_at ASC."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT item FROM shopping ORDER BY created_at ASC"
        ).fetchall()

    return [row["item"] for row in rows]


def remove_shopping(shopping_id: int) -> bool:
    """Delete the row. Returns True if deleted, False if not found."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM shopping WHERE id = ?", (shopping_id,))
        if cur.rowcount > 0:
            mutation_log.log(
                conn, "shopping", "DELETE", shopping_id, {},
            )
            return True
        return False
