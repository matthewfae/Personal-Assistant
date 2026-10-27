"""Tests for trimming history: the keep_turns tool, turn lookup and the hard cap."""

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


def test_keep_turns_keeps_that_many_before_the_current_turn():
    a, b, c = _turn(), _turn(), _turn()
    trim = _turn("wakeup")  # the trim job's own turn

    _dispatch("set_context_bound", {"keep_turns": 2, "rationale": "r"})
    assert context_db.get_context_bound() == b

    _dispatch("set_context_bound", {"keep_turns": 0, "rationale": "r"})
    assert context_db.get_context_bound() == trim

    _dispatch("set_context_bound", {"keep_turns": 10, "rationale": "r"})
    assert context_db.get_context_bound() == 0  # keep everything


def test_hard_cap_limits_history_to_max_turns(monkeypatch):
    monkeypatch.setattr(agent, "_MAX_TURNS", 3)
    roots = [_turn() for _ in range(5)]

    assert agent._load_from_event_id() == roots[2]  # last 3 turns
    # A stored bound newer than the cap wins.
    context_db.set_context_bound(roots[4])
    assert agent._load_from_event_id() == roots[4]
