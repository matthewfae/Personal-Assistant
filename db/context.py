from db.connection import get_db


def get_context_bound() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT from_event_id FROM context_bound WHERE id = 1").fetchone()
    return row["from_event_id"] if row else 0


def set_context_bound(from_event_id: int) -> None:
    """Persist the context projection bound. Raises ValueError if from_event_id is not in events."""
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM events WHERE id = ?", (from_event_id,)).fetchone()
        if exists is None:
            raise ValueError(f"Unknown event id: {from_event_id}")
        conn.execute("UPDATE context_bound SET from_event_id = ? WHERE id = 1", (from_event_id,))
