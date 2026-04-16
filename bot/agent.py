"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Tools are discovered from the MCP server at startup rather than hardcoded here.
Implements prompt caching for cost reduction on repeated context.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from anthropic import Anthropic

import db.context as context_db
from db.connection import get_db
from mcp_client import MCPClient
from prompt_builder import PromptBuilder

_system_prompt = PromptBuilder("system.md")
_reflection_prompt = PromptBuilder("reflection.md")

# Tracks the oldest event id to include when projecting messages next turn.
_from_event_id: int = 0


@dataclass
class TurnResult:
    reply: str
    proactive: str | None


def _write_event(conn, turn_id, event_type, payload, parent_event_id=None) -> int:
    """Insert one event row and return its id. conn must be an open sqlite3 connection."""
    cursor = conn.execute(
        "INSERT INTO events (timestamp, turn_id, type, parent_event_id, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            datetime.utcnow().isoformat() + 'Z',
            turn_id,
            event_type,
            parent_event_id,
            json.dumps(payload),
        ),
    )
    return cursor.lastrowid


def project_messages(from_event_id: int = 0) -> list[dict]:
    """Build the messages array from events at or after from_event_id.

    Reconstructs the full interleaved sequence: user messages, tool use/result
    pairs (grouped by api_call), and final assistant messages.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, type, parent_event_id, payload
            FROM events
            WHERE id >= ?
              AND type IN ('user_message', 'api_call', 'tool_call', 'tool_result', 'assistant_message')
            ORDER BY id ASC
            """,
            (from_event_id,),
        ).fetchall()

    # Index for parent lookups (needed to map tool_result → tool_call → api_call).
    event_by_id = {row["id"]: row for row in rows}

    # Group tool_calls and tool_results under their parent api_call id.
    tool_calls_by_api: dict[int, list] = {}
    tool_results_by_api: dict[int, list] = {}
    for row in rows:
        if row["type"] == "tool_call":
            tool_calls_by_api.setdefault(row["parent_event_id"], []).append(row)
        elif row["type"] == "tool_result":
            tc = event_by_id.get(row["parent_event_id"])
            if tc is not None:
                tool_results_by_api.setdefault(tc["parent_event_id"], []).append(row)

    messages: list[dict] = []
    for row in rows:
        etype = row["type"]
        payload = json.loads(row["payload"])

        if etype == "user_message":
            messages.append({"role": "user", "content": payload["content"]})

        elif etype == "api_call":
            tool_calls = tool_calls_by_api.get(row["id"], [])
            if not tool_calls:
                # No tool calls: either a meta-call or an end_turn api_call.
                # The assistant_message event handles the end_turn case.
                continue
            messages.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": json.loads(tc["payload"])["tool_use_id"],
                        "name": json.loads(tc["payload"])["name"],
                        "input": json.loads(tc["payload"])["input"],
                    }
                    for tc in tool_calls
                ],
            })
            result_blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": json.loads(tr["payload"])["tool_use_id"],
                    "content": json.loads(tr["payload"])["content"],
                }
                for tr in tool_results_by_api.get(row["id"], [])
            ]
            if result_blocks:
                messages.append({"role": "user", "content": result_blocks})

        elif etype == "assistant_message":
            if payload["content"]:
                messages.append({"role": "assistant", "content": payload["content"]})

    return messages


def _load_from_event_id() -> int:
    """Return the current context projection bound from the context_bound table."""
    return context_db.get_context_bound()


async def _run_reflection(
    client: Anthropic,
    mcp: MCPClient,
    turn_id: str,
    assistant_message_id: int,
) -> str | None:
    """Run the reflection mini agent loop after a main turn completes.

    May call tools (including set_context_bound) or send a follow-up message.
    Returns the final assistant text if it is not PASS, otherwise returns None.
    """
    try:
        from_event_id = _load_from_event_id()
        messages = project_messages(from_event_id)

        next_api_call_parent_id: int = assistant_message_id

        while True:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8192,
                system=[
                    {
                        "type": "text",
                        "text": _reflection_prompt.build(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=mcp.tools,
                messages=messages,
            )

            with get_db() as conn:
                api_call_id = _write_event(conn, turn_id, "api_call", {
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
                if final_text.strip().upper() == "PASS":
                    return None
                with get_db() as conn:
                    _write_event(
                        conn, turn_id, "assistant_message",
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
                            tool_call_id = _write_event(conn, turn_id, "tool_call", {
                                "v": 1,
                                "tool_use_id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }, parent_event_id=api_call_id)
                        result = await mcp.call_tool(block.name, block.input)
                        with get_db() as conn:
                            last_tool_result_id = _write_event(conn, turn_id, "tool_result", {
                                "v": 1,
                                "tool_use_id": block.id,
                                "content": result,
                            }, parent_event_id=tool_call_id)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "user", "content": tool_results})
                if last_tool_result_id is not None:
                    next_api_call_parent_id = last_tool_result_id

            else:
                return None

    except Exception as e:
        with get_db() as conn:
            _write_event(
                conn, turn_id, "error",
                {"v": 1, "context": "reflection", "message": str(e)},
                parent_event_id=assistant_message_id,
            )
        return None


async def run_loop(
    client: Anthropic,
    mcp: MCPClient,
    user_message: str,
    turn_id: str | None = None,
) -> TurnResult:
    """
    Run the agent loop for one user turn.

    Projects the message history from the events table, appends the new user
    message, runs until Claude produces a final text response, then runs the
    reflection step. Returns a TurnResult with the reply and optional proactive
    follow-up text.

    turn_id may be supplied by the caller (e.g. a transport handler that has
    already written a pre-turn event under the same id). If omitted, a fresh
    UUID is generated.
    """
    global _from_event_id

    if turn_id is None:
        turn_id = uuid.uuid4().hex

    from_event_id = _load_from_event_id()
    messages = project_messages(from_event_id)
    messages.append({"role": "user", "content": user_message})

    with get_db() as conn:
        user_message_id = _write_event(
            conn, turn_id, "user_message",
            {"v": 1, "content": user_message},
            parent_event_id=None,
        )

    # For the first api_call, parent is the user_message.
    # For subsequent api_calls, parent is the last tool_result written in the previous round.
    next_api_call_parent_id: int = user_message_id

    # set_context_bound is a reflection-step tool; hide it from the main turn.
    # Re-attach cache_control to the new last tool so caching is preserved.
    main_tools = [t for t in mcp.tools if t["name"] != "set_context_bound"]
    if main_tools and not main_tools[-1].get("cache_control"):
        main_tools = [*main_tools[:-1], {**main_tools[-1], "cache_control": {"type": "ephemeral"}}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _system_prompt.build(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=main_tools,
            messages=messages,
        )

        with get_db() as conn:
            api_call_id = _write_event(conn, turn_id, "api_call", {
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
                assistant_message_event_id = _write_event(
                    conn, turn_id, "assistant_message",
                    {"v": 1, "content": final_text},
                    parent_event_id=api_call_id,
                )
            proactive_text = await _run_reflection(client, mcp, turn_id, assistant_message_event_id)
            _from_event_id = _load_from_event_id()
            return TurnResult(reply=final_text, proactive=proactive_text)

        elif response.stop_reason == "tool_use":
            tool_results = []
            last_tool_result_id: int | None = None
            for block in response.content:
                if block.type == "tool_use":
                    with get_db() as conn:
                        tool_call_id = _write_event(conn, turn_id, "tool_call", {
                            "v": 1,
                            "tool_use_id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }, parent_event_id=api_call_id)
                    result = await mcp.call_tool(block.name, block.input)
                    with get_db() as conn:
                        last_tool_result_id = _write_event(conn, turn_id, "tool_result", {
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
            return TurnResult(reply=f"Unexpected stop reason: {response.stop_reason}", proactive=None)


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
