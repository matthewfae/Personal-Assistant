"""
Data access layer for the notes table.

All functions return plain dicts (or lists of dicts). No MCP types, no
string formatting for Claude — that belongs in the tools layer.

Notes support full-text search via the notes_fts FTS5 virtual table, which
is kept in sync by SQL triggers defined in db/connection.py.

Tags are stored as a plain comma-separated string (e.g. "work,ideas").
"""

from datetime import datetime, timezone

from db.connection import get_db
from db import mutation_log


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_note(title: str, body: str = "", tags: str = "") -> dict:
    """
    Create a new note. Tags is a comma-separated string.

    Returns the new note as a dict with keys:
        id, title, body, tags, created_at, updated_at
    """
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO notes(title, body, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, body, tags, now, now),
        )
        mutation_log.log(
            conn, "notes", "INSERT", cur.lastrowid,
            {"title": title, "tags": tags},
        )
        return {
            "id": cur.lastrowid,
            "title": title,
            "body": body,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
        }


def get_note(note_id: int) -> dict | None:
    """
    Return a note by ID, or None if not found.

    Dict keys: id, title, body, tags, created_at, updated_at
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, title, body, tags, created_at, updated_at"
            " FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    return dict(row) if row else None


def search_notes(query: str, limit: int = 10) -> list[dict]:
    """
    Full-text search across title, body, and tags using FTS5.
    Results are ordered by FTS rank (best match first).

    Each item has keys: id, title, body, tags, created_at, updated_at
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT n.id, n.title, n.body, n.tags, n.created_at, n.updated_at"
            " FROM notes_fts f"
            " JOIN notes n ON n.id = f.rowid"
            " WHERE notes_fts MATCH ?"
            " ORDER BY rank"
            " LIMIT ?",
            (query, limit),
        ).fetchall()

    return [dict(r) for r in rows]


def list_notes(limit: int = 10) -> list[dict]:
    """
    Return up to `limit` notes, most recently updated first.
    Body is excluded to keep the listing lightweight.

    Each item has keys: id, title, tags, created_at, updated_at
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, tags, created_at, updated_at"
            " FROM notes ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]
