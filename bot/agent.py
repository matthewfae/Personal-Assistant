"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Tools are discovered from the MCP server at startup rather than hardcoded here.
Implements prompt caching for cost reduction on repeated context.
"""

import json
import uuid
from datetime import datetime

from anthropic import Anthropic

from db.connection import get_db
from mcp_client import MCPClient
from prompt_builder import PromptBuilder

_prompt_builder = PromptBuilder()

# One conversation_id per process start (stable for the process lifetime).
conversation_id: str = uuid.uuid4().hex


def _write_event(conn, turn_id, conversation_id, event_type, payload, parent_event_id=None):
    """Insert one event row. conn must be an open sqlite3 connection."""
    conn.execute(
        "INSERT INTO events (timestamp, turn_id, conversation_id, type, parent_event_id, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.utcnow().isoformat() + 'Z',
            turn_id,
            conversation_id,
            event_type,
            parent_event_id,
            json.dumps(payload),
        ),
    )


def project_messages(conversation_id: str) -> list[dict]:
    """Build the messages array from events for the given conversation."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT type, payload
            FROM events
            WHERE conversation_id = ?
              AND type IN ('user_message', 'assistant_message')
            ORDER BY timestamp ASC
            """,
            (conversation_id,),
        ).fetchall()

    messages = []
    for row in rows:
        payload = json.loads(row["payload"])
        if row["type"] == "user_message":
            messages.append({"role": "user", "content": payload["content"]})
        elif row["type"] == "assistant_message":
            messages.append({"role": "assistant", "content": payload["content"]})
    return messages


async def run_loop(
    client: Anthropic,
    mcp: MCPClient,
    user_message: str,
) -> str:
    """
    Run the agent loop for one user turn.

    Projects the message history from the events table, appends the new user
    message, runs until Claude produces a final text response, and returns that
    response. The in-memory message list is discarded at the end of the turn;
    the next turn re-projects from the DB.
    """
    # Fresh turn_id for every run_loop call; all events in this turn share it.
    turn_id: str = uuid.uuid4().hex

    messages = project_messages(conversation_id)
    messages.append({"role": "user", "content": user_message})

    with get_db() as conn:
        _write_event(conn, turn_id, conversation_id, "user_message", {"v": 1, "content": user_message})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _prompt_builder.build(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=mcp.tools,
            messages=messages,
        )

        with get_db() as conn:
            _write_event(conn, turn_id, conversation_id, "api_call", {
                "v": 1,
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            })

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            with get_db() as conn:
                _write_event(conn, turn_id, conversation_id, "assistant_message", {"v": 1, "content": final_text})
            return final_text

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    with get_db() as conn:
                        _write_event(conn, turn_id, conversation_id, "tool_call", {
                            "v": 1,
                            "tool_use_id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    result = await mcp.call_tool(block.name, block.input)
                    with get_db() as conn:
                        _write_event(conn, turn_id, conversation_id, "tool_result", {
                            "v": 1,
                            "tool_use_id": block.id,
                            "content": result,
                        })
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        else:
            return f"Unexpected stop reason: {response.stop_reason}"
