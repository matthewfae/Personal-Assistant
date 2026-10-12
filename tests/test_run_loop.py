"""Tests for agent.run_loop, using a fake async Anthropic client and a fake MCP client."""

import asyncio
import json
from types import SimpleNamespace

from agent import run_loop
from db.connection import get_db
from mcp_client import ToolOutput


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use(tool_use_id, name, tool_input):
    return SimpleNamespace(type="tool_use", id=tool_use_id, name=name, input=tool_input)


def _response(stop_reason, *content):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=list(content),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeClient:
    """Async stand-in for AsyncAnthropic that returns scripted responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kwargs):
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class FakeMCP:
    tools = [
        {"name": "list_tasks", "description": "", "input_schema": {"type": "object"}},
        {"name": "set_context_bound", "description": "", "input_schema": {"type": "object"},
         "cache_control": {"type": "ephemeral"}},
    ]

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolOutput(content="#1 fix vent", is_error=False)


def _event_types():
    with get_db() as conn:
        return [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]


def test_end_turn_with_reflection_pass():
    client = FakeClient([
        _response("end_turn", _text("hello")),
        _response("end_turn", _text("PASS")),
    ])

    result = asyncio.run(run_loop(client, FakeMCP(), "hi"))

    assert result.reply == "hello"
    assert result.proactive is None
    assert _event_types() == ["user_message", "api_call", "assistant_message", "api_call"]


def test_tool_use_round_then_reply():
    mcp = FakeMCP()
    client = FakeClient([
        _response("tool_use", _tool_use("toolu_1", "list_tasks", {"status": "open"})),
        _response("end_turn", _text("You have one task.")),
        _response("end_turn", _text("PASS")),
    ])

    result = asyncio.run(run_loop(client, mcp, "what's on my list?"))

    assert result.reply == "You have one task."
    assert mcp.calls == [("list_tasks", {"status": "open"})]
    # The second request carries the tool result back to Claude.
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["content"] == "#1 fix vent"
    assert _event_types() == [
        "user_message", "api_call", "tool_call", "tool_result",
        "api_call", "assistant_message", "api_call",
    ]


def test_main_turn_hides_set_context_bound_but_reflection_sees_it():
    client = FakeClient([
        _response("end_turn", _text("hello")),
        _response("end_turn", _text("PASS")),
    ])

    asyncio.run(run_loop(client, FakeMCP(), "hi"))

    main_tools = [t["name"] for t in client.requests[0]["tools"]]
    reflection_tools = [t["name"] for t in client.requests[1]["tools"]]
    assert "set_context_bound" not in main_tools
    assert "set_context_bound" in reflection_tools
    # cache_control moves to the new last tool when set_context_bound is removed.
    assert client.requests[0]["tools"][-1].get("cache_control") == {"type": "ephemeral"}


def test_reflection_follow_up_is_returned_and_recorded():
    client = FakeClient([
        _response("end_turn", _text("hello")),
        _response("end_turn", _text("Also: the vent task is overdue.")),
    ])

    result = asyncio.run(run_loop(client, FakeMCP(), "hi"))

    assert result.proactive == "Also: the vent task is overdue."
    with get_db() as conn:
        last = conn.execute("SELECT type, payload FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert last["type"] == "assistant_message"
    assert json.loads(last["payload"])["content"] == "Also: the vent task is overdue."
