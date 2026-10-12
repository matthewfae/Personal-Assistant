"""Tests for agent.project_messages: rebuilding the API messages array from events."""

from datetime import datetime, timezone

from agent import project_messages, write_event
from db.connection import get_db

# Every user message in these tests is sent at this time, which renders (in the
# default America/Chicago timezone) as the prefix below.
SENT_AT = datetime(2026, 10, 12, 22, 3, tzinfo=timezone.utc)
PREFIX = "[Monday, October 12, 2026 at 5:03 PM CDT]\n"


class EventLog:
    """Writes events under one turn and returns their ids, for terse test setup."""

    def __init__(self, turn_id="t1"):
        self.turn_id = turn_id

    def add(self, event_type, payload, parent=None, timestamp=None) -> int:
        with get_db() as conn:
            return write_event(conn, self.turn_id, event_type, {"v": 1, **payload}, parent, timestamp)

    def user(self, content):
        return self.add("user_message", {"content": content}, timestamp=SENT_AT)

    def api_call(self, parent):
        return self.add("api_call", {"model": "m", "input_tokens": 1, "output_tokens": 1}, parent)

    def tool_call(self, api_call_id, tool_use_id, name="list_tasks", tool_input=None):
        return self.add(
            "tool_call",
            {"tool_use_id": tool_use_id, "name": name, "input": tool_input or {}},
            api_call_id,
        )

    def tool_result(self, tool_call_id, tool_use_id, content="ok", **extra):
        return self.add(
            "tool_result",
            {"tool_use_id": tool_use_id, "content": content, **extra},
            tool_call_id,
        )

    def assistant(self, api_call_id, content):
        return self.add("assistant_message", {"content": content}, api_call_id)


def test_empty_log():
    assert project_messages() == []


def test_simple_exchange():
    log = EventLog()
    u = log.user("hi")
    a = log.api_call(u)
    log.assistant(a, "hello")

    assert project_messages() == [
        {"role": "user", "content": PREFIX + "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_tool_round_trip():
    log = EventLog()
    u = log.user("what's on my list?")
    a1 = log.api_call(u)
    tc = log.tool_call(a1, "toolu_1", "list_tasks", {"status": "open"})
    tr = log.tool_result(tc, "toolu_1", "#1 fix vent", is_error=False)
    a2 = log.api_call(tr)
    log.assistant(a2, "You have one task.")

    assert project_messages() == [
        {"role": "user", "content": PREFIX + "what's on my list?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "list_tasks", "input": {"status": "open"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "#1 fix vent", "is_error": False},
        ]},
        {"role": "assistant", "content": "You have one task."},
    ]


def test_parallel_tool_calls_grouped_into_one_message_each():
    log = EventLog()
    u = log.user("tasks and shopping?")
    a1 = log.api_call(u)
    tc1 = log.tool_call(a1, "toolu_1", "list_tasks")
    tc2 = log.tool_call(a1, "toolu_2", "list_shopping")
    log.tool_result(tc1, "toolu_1", "tasks")
    tr2 = log.tool_result(tc2, "toolu_2", "shopping")
    a2 = log.api_call(tr2)
    log.assistant(a2, "done")

    messages = project_messages()

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [b["id"] for b in messages[1]["content"]] == ["toolu_1", "toolu_2"]
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["toolu_1", "toolu_2"]


def test_is_error_propagated_and_defaults_to_false_for_old_events():
    log = EventLog()
    u = log.user("x")
    a1 = log.api_call(u)
    tc1 = log.tool_call(a1, "toolu_old")
    tc2 = log.tool_call(a1, "toolu_bad")
    log.tool_result(tc1, "toolu_old", "legacy result")  # predates is_error
    log.tool_result(tc2, "toolu_bad", "boom", is_error=True)

    results = project_messages()[2]["content"]

    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True


def test_empty_assistant_message_skipped():
    log = EventLog()
    u = log.user("x")
    a = log.api_call(u)
    log.assistant(a, "")

    assert project_messages() == [{"role": "user", "content": PREFIX + "x"}]


def test_non_conversation_events_ignored():
    log = EventLog()
    log.add("telegram_ack", {"telegram_message_id": 1})
    u = log.user("x")
    a = log.api_call(u)
    m = log.assistant(a, "y")
    log.add("error", {"context": "reflection", "message": "oops"}, m)

    assert project_messages() == [
        {"role": "user", "content": PREFIX + "x"},
        {"role": "assistant", "content": "y"},
    ]


def test_bound_excludes_earlier_turns():
    old = EventLog("old")
    u = old.user("old question")
    old.assistant(old.api_call(u), "old answer")
    new = EventLog("new")
    u2 = new.user("new question")
    new.assistant(new.api_call(u2), "new answer")

    assert project_messages(from_event_id=u2) == [
        {"role": "user", "content": PREFIX + "new question"},
        {"role": "assistant", "content": "new answer"},
    ]


def _turn_with_tool_round(log):
    """Write user → api_call → tool_call → tool_result → api_call → assistant; return ids by name."""
    ids = {"user": log.user("q")}
    ids["api_call"] = log.api_call(ids["user"])
    ids["tool_call"] = log.tool_call(ids["api_call"], "toolu_1")
    ids["tool_result"] = log.tool_result(ids["tool_call"], "toolu_1")
    ids["api_call_2"] = log.api_call(ids["tool_result"])
    ids["assistant"] = log.assistant(ids["api_call_2"], "answer")
    return ids


def _assert_tool_blocks_paired(messages):
    """Every tool_use has a matching tool_result and vice versa (the API rejects either orphan)."""
    uses, results = set(), set()
    for m in messages:
        if isinstance(m["content"], list):
            for block in m["content"]:
                if block["type"] == "tool_use":
                    uses.add(block["id"])
                elif block["type"] == "tool_result":
                    results.add(block["tool_use_id"])
    assert uses == results


def test_bound_on_any_event_never_orphans_tool_blocks():
    # The reflection step can set the bound to any event id, not just a user_message.
    ids = _turn_with_tool_round(EventLog())
    for name, event_id in ids.items():
        _assert_tool_blocks_paired(project_messages(from_event_id=event_id))


def test_bound_mid_tool_round_drops_the_whole_round():
    ids = _turn_with_tool_round(EventLog())

    assert project_messages(from_event_id=ids["tool_call"]) == [
        {"role": "assistant", "content": "answer"},
    ]


def test_user_message_rendered_identically_on_every_projection():
    # Byte-stable history is what lets the prompt cache hit across turns.
    log = EventLog()
    log.user("hi")
    assert project_messages() == project_messages()
