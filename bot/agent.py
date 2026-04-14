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


def _write_event(conn, turn_id, conversation_id, event_type, payload, parent_event_id=None) -> int:
    """Insert one event row and return its id. conn must be an open sqlite3 connection."""
    cursor = conn.execute(
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
    return cursor.lastrowid


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
        user_message_id = _write_event(
            conn, turn_id, conversation_id, "user_message",
            {"v": 1, "content": user_message},
            parent_event_id=None,
        )

    # For the first api_call, parent is the user_message.
    # For subsequent api_calls, parent is the last tool_result written in the previous round.
    next_api_call_parent_id: int = user_message_id

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
            api_call_id = _write_event(conn, turn_id, conversation_id, "api_call", {
                "v": 1,
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }, parent_event_id=next_api_call_parent_id)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            with get_db() as conn:
                _write_event(
                    conn, turn_id, conversation_id, "assistant_message",
                    {"v": 1, "content": final_text},
                    parent_event_id=api_call_id,
                )
            return final_text

        elif response.stop_reason == "tool_use":
            tool_results = []
            last_tool_result_id: int | None = None
            for block in response.content:
                if block.type == "tool_use":
                    with get_db() as conn:
                        tool_call_id = _write_event(conn, turn_id, conversation_id, "tool_call", {
                            "v": 1,
                            "tool_use_id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }, parent_event_id=api_call_id)
                    result = await mcp.call_tool(block.name, block.input)
                    with get_db() as conn:
                        last_tool_result_id = _write_event(conn, turn_id, conversation_id, "tool_result", {
                            "v": 1,
                            "tool_use_id": block.id,
                            "content": result,
                        }, parent_event_id=tool_call_id)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            # Next api_call's parent is the last tool_result written in this round.
            if last_tool_result_id is not None:
                next_api_call_parent_id = last_tool_result_id

        else:
            return f"Unexpected stop reason: {response.stop_reason}"


def debug_causality_tree(turn_id: str) -> str:
    """Return a text representation of the causality tree for a turn.

    Queries all events for the given turn_id, builds a parent→children map,
    then recursively formats the tree with indentation reflecting depth.

    Example output::

        user_message (id=1)
          api_call (id=2, in=120, out=45)
            tool_call (id=3, name=add_fact)
              tool_result (id=4)
            api_call (id=5, in=200, out=80)
              assistant_message (id=6)
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, type, parent_event_id, payload FROM events "
            "WHERE turn_id = ? ORDER BY id ASC",
            (turn_id,),
        ).fetchall()

    if not rows:
        return f"No events found for turn_id={turn_id}"

    # Build lookup structures.
    children: dict[int | None, list] = {}
    event_by_id: dict[int, dict] = {}
    for row in rows:
        event = {
            "id": row["id"],
            "type": row["type"],
            "parent_event_id": row["parent_event_id"],
            "payload": json.loads(row["payload"]),
        }
        event_by_id[event["id"]] = event
        parent = event["parent_event_id"]
        children.setdefault(parent, []).append(event["id"])

    def _format_label(event: dict) -> str:
        """Build the label string for a single event node."""
        etype = event["type"]
        eid = event["id"]
        payload = event["payload"]
        if etype == "api_call":
            in_tok = payload.get("input_tokens", "?")
            out_tok = payload.get("output_tokens", "?")
            return f"api_call (id={eid}, in={in_tok}, out={out_tok})"
        elif etype == "tool_call":
            name = payload.get("name", "?")
            return f"tool_call (id={eid}, name={name})"
        else:
            return f"{etype} (id={eid})"

    lines: list[str] = []

    def _walk(event_id: int, depth: int) -> None:
        indent = "  " * depth
        lines.append(indent + _format_label(event_by_id[event_id]))
        for child_id in children.get(event_id, []):
            _walk(child_id, depth + 1)

    # Roots are events with no parent (None key in children map).
    roots = children.get(None, [])
    for root_id in roots:
        _walk(root_id, 0)

    return "\n".join(lines)
