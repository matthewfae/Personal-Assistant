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


def test_end_turn_is_one_api_call():
    client = FakeClient([
        _response("end_turn", _text("hello")),
    ])

    result = asyncio.run(run_loop(client, FakeMCP(), "hi"))

    assert result.reply == "hello"
    assert _event_types() == ["user_message", "api_call", "assistant_message"]


def test_tool_use_round_then_reply():
    mcp = FakeMCP()
    client = FakeClient([
        _response("tool_use", _tool_use("toolu_1", "list_tasks", {"status": "open"})),
        _response("end_turn", _text("You have one task.")),
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
        "api_call", "assistant_message",
    ]


def test_set_context_bound_is_hidden_unless_the_job_allows_it():
    client = FakeClient([
        _response("end_turn", _text("hello")),
        _response("end_turn", _text("trimmed")),
    ])

    asyncio.run(run_loop(client, FakeMCP(), "hi"))
    asyncio.run(run_loop(client, FakeMCP(), "trim", job="trim_context",
                         extra_tools=frozenset({"set_context_bound"})))

    main_tools = [t["name"] for t in client.requests[0]["tools"]]
    trim_tools = [t["name"] for t in client.requests[1]["tools"]]
    assert "set_context_bound" not in main_tools
    assert "set_context_bound" in trim_tools
    # cache_control moves to the new last tool when set_context_bound is removed.
    assert client.requests[0]["tools"][-1].get("cache_control") == {"type": "ephemeral"}
    assert sum(1 for t in client.requests[1]["tools"] if "cache_control" in t) == 1


def test_prompt_is_cache_friendly():
    client = FakeClient([
        _response("end_turn", _text("hello")),
    ])

    asyncio.run(run_loop(client, FakeMCP(), "hi"))

    main = client.requests[0]
    # The system prompt carries no time, so it's identical on every call.
    assert "{" not in main["system"][0]["text"]
    assert "current_time" not in main["system"][0]["text"]
    # The time travels with the user message instead.
    assert main["messages"][-1]["content"].startswith("[")
    assert main["messages"][-1]["content"].endswith("]\nhi")
    # Automatic caching covers the message history.
    assert main["cache_control"] == {"type": "ephemeral"}


def test_cache_token_counts_recorded():
    response = _response("end_turn", _text("hello"))
    response.usage.cache_read_input_tokens = 1500
    response.usage.cache_creation_input_tokens = None  # the API may omit it
    client = FakeClient([response])

    asyncio.run(run_loop(client, FakeMCP(), "hi"))

    with get_db() as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM events WHERE type = 'api_call' ORDER BY id LIMIT 1"
        ).fetchone()["payload"])
    assert payload["cache_read_input_tokens"] == 1500
    assert payload["cache_creation_input_tokens"] == 0


def test_wakeup_turn_runs_local_tool():
    from agent import LocalTool, project_messages

    sent = []

    async def notify(tool_input):
        sent.append(tool_input["text"])
        return "Sent."

    notify_tool = LocalTool({"name": "notify_user", "description": "", "input_schema": {"type": "object"}}, notify)
    mcp = FakeMCP()
    client = FakeClient([
        _response("tool_use", _tool_use("toolu_1", "notify_user", {"text": "Groceries to put away"})),
        _response("end_turn", _text("Notified.")),
    ])

    result = asyncio.run(run_loop(client, mcp, "Matt got home.", job="arrived_home", local_tools=[notify_tool]))

    assert sent == ["Groceries to put away"]
    assert mcp.calls == []  # handled in-process, not sent to MCP
    assert result.reply == "Notified."
    assert _event_types() == ["wakeup", "api_call", "tool_call", "tool_result", "api_call", "assistant_message"]
    # notify_user is offered last, carrying the tool-list cache breakpoint.
    tools = client.requests[0]["tools"]
    assert tools[-1]["name"] == "notify_user"
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert sum(1 for t in tools if "cache_control" in t) == 1
    # The wake-up root is projected as a timestamped user message.
    first = project_messages()[0]
    assert first["role"] == "user"
    assert first["content"].endswith("]\n(wake-up: arrived_home)\nMatt got home.")


def test_only_a_turn_that_can_trim_sees_event_ids():
    client = FakeClient([
        _response("end_turn", _text("hello")),
        _response("end_turn", _text("trimmed")),
    ])

    asyncio.run(run_loop(client, FakeMCP(), "hi"))
    asyncio.run(run_loop(client, FakeMCP(), "trim", job="trim_context",
                         extra_tools=frozenset({"set_context_bound"})))

    assert client.requests[0]["messages"][-1]["content"].startswith("[")
    assert not client.requests[0]["messages"][-1]["content"].startswith("[#")
    assert all(
        m["content"].startswith("[#") for m in client.requests[1]["messages"]
    )
