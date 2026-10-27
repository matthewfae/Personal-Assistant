from db.connection import get_db


def get_context_bound() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT from_event_id FROM context_bound WHERE id = 1").fetchone()
    return row["from_event_id"] if row else 0


def set_context_bound(from_event_id: int) -> None:
    """Persist the context projection bound. 0 means the start of the log.

    Raises ValueError if from_event_id is neither 0 nor in events.
    """
    with get_db() as conn:
        exists = from_event_id == 0 or conn.execute(
            "SELECT 1 FROM events WHERE id = ?", (from_event_id,)
        ).fetchone()
        if not exists:
            raise ValueError(f"Unknown event id: {from_event_id}")
        conn.execute("UPDATE context_bound SET from_event_id = ? WHERE id = 1", (from_event_id,))


def turn_start(event_id: int) -> int:
    """Id of the turn root (user_message or wakeup) at or before event_id; 0 if none.

    A bound must sit on a turn root: cutting mid-turn could split a tool call
    from its result, which the API rejects.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS id FROM events WHERE type IN ('user_message', 'wakeup') AND id <= ?",
            (event_id,),
        ).fetchone()
    return row["id"] or 0


def turn_root_id(turns_back: int) -> int:
    """Id of the turns_back-th most recent turn root (user_message or wakeup); 1 is the latest.

    Returns 0 (the start of the log) if there are fewer turns than that.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE type IN ('user_message', 'wakeup') "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (turns_back - 1,),
        ).fetchone()
    return row["id"] if row else 0
