"""Tests for trimming history: set_context_bound, event id labels, turn lookup and the hard cap."""

import agent
import db.context as context_db
from agent import write_event
from db.connection import get_db
from tools.server import _dispatch


def _turn(kind="user_message"):
    """Write a turn root plus a reply; return the root's id."""
    with get_db() as conn:
        root = write_event(conn, "t", kind, {"v": 1, "content": "x", "job": "j", "prompt": "p"})
        write_event(conn, "t", "assistant_message", {"v": 1, "content": "ok"}, parent_event_id=root)
    return root


def test_turn_root_id_counts_back_from_latest():
    a, b, c = _turn(), _turn("wakeup"), _turn()
    assert context_db.turn_root_id(1) == c
    assert context_db.turn_root_id(2) == b
    assert context_db.turn_root_id(3) == a
    assert context_db.turn_root_id(4) == 0  # fewer turns than that


def _reply_id(root):
    with get_db() as conn:
        return conn.execute(
            "SELECT id FROM events WHERE parent_event_id = ?", (root,)
        ).fetchone()["id"]


def test_bound_on_a_turn_root_is_kept_as_is():
    a, b = _turn(), _turn()
    assert "starts at event %d" % b in _dispatch("set_context_bound", {"from_event_id": b, "rationale": "r"})
    assert context_db.get_context_bound() == b


def test_bound_mid_turn_keeps_the_whole_turn():
    a, b = _turn(), _turn()
    result = _dispatch("set_context_bound", {"from_event_id": _reply_id(b), "rationale": "r"})
    assert context_db.get_context_bound() == b
    assert "mid-turn" in result


def test_projection_shows_ids_only_when_asked():
    root = _turn()
    reply = _reply_id(root)

    plain = agent.project_messages()
    assert "#" not in plain[0]["content"]
    assert plain[1]["content"] == "ok"

    labelled = agent.project_messages(show_ids=True)
    assert labelled[0]["content"].startswith(f"[#{root} · ")
    assert labelled[1]["content"] == f"[#{reply}] ok"


def test_hard_cap_limits_history_to_max_turns(monkeypatch):
    monkeypatch.setattr(agent, "_MAX_TURNS", 3)
    roots = [_turn() for _ in range(5)]

    assert agent._load_from_event_id() == roots[2]  # last 3 turns
    # A stored bound newer than the cap wins.
    context_db.set_context_bound(roots[4])
    assert agent._load_from_event_id() == roots[4]
