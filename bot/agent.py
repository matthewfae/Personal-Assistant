"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Tools are discovered from the MCP server at startup rather than hardcoded here.
Implements prompt caching for cost reduction on repeated context.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

import db.events as events_db
from db.connection import get_db
from mcp_client import MCPClient
from prompt_builder import PromptBuilder

_prompt_builder = PromptBuilder()


def build_messages(conn: sqlite3.Connection, conversation_id: str) -> list[dict]:
    """Build the Anthropic messages list from all prior events in this conversation.

    Processes events in id order, grouping by turn. Only complete turns are
    included — incomplete turns (crashes, orphaned tool calls) are skipped.
    Does not include the current in-progress turn.
    """
    rows = conn.execute(
        """
        SELECT id, turn_id, type, payload
        FROM events
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()

    # Group events by turn_id, preserving encounter order
    turns: dict[str, list] = {}
    turn_order: list[str] = []
    for row in rows:
        tid = row["turn_id"]
        if tid not in turns:
            turns[tid] = []
            turn_order.append(tid)
        turns[tid].append(row)

    messages = []
    for tid in turn_order:
        events = turns[tid]
        by_type = {}
        for e in events:
            by_type.setdefault(e["type"], []).append(e)

        # Skip turns with no user_message or no assistant_message (incomplete/crashed turns)
        if "user_message" not in by_type or "assistant_message" not in by_type:
            continue

        # User message
        user_payload = json.loads(by_type["user_message"][0]["payload"])
        messages.append({"role": "user", "content": user_payload["text"]})

        # Tool exchanges: pair tool_call + tool_result by tool_use_id
        # Only emit complete pairs; skip any orphaned tool_calls
        if "tool_call" in by_type:
            tool_results_by_id = {}
            if "tool_result" in by_type:
                for tr in by_type["tool_result"]:
                    tr_payload = json.loads(tr["payload"])
                    tool_results_by_id[tr_payload["tool_use_id"]] = tr_payload

            # Collect complete tool_use blocks for the assistant message
            tool_use_blocks = []
            result_blocks = []
            for tc in by_type["tool_call"]:
                tc_payload = json.loads(tc["payload"])
                tuid = tc_payload["tool_use_id"]
                if tuid not in tool_results_by_id:
                    continue  # orphaned tool_call — skip entire exchange
                tool_use_blocks.append({
                    "type": "tool_use",
                    "id": tuid,
                    "name": tc_payload["tool_name"],
                    "input": tc_payload["input"],
                })
                tr_payload = tool_results_by_id[tuid]
                result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tuid,
                    "content": tr_payload["content"],
                })

            if tool_use_blocks:
                messages.append({"role": "assistant", "content": tool_use_blocks})
                messages.append({"role": "user", "content": result_blocks})

        # Final assistant message
        asst_payload = json.loads(by_type["assistant_message"][0]["payload"])
        messages.append({"role": "assistant", "content": [{"type": "text", "text": asst_payload["text"]}]})

    return messages


async def run_loop(
    client: AsyncAnthropic,
    mcp: MCPClient,
    conversation_id: str,
    user_message: str,
) -> str:
    """
    Run the agent loop for one user turn.

    Takes a conversation_id and a new user message, writes the user_message
    event to the DB, runs until Claude produces a final text response, and
    returns that response. Message history is projected from the events table
    at turn start and appended to locally during the turn.
    """
    turn_id = str(uuid.uuid4())

    # Write user_message event — turn root, no parent
    with get_db() as conn:
        user_event_id = events_db.append(
            conn,
            "user_message",
            {"v": 1, "text": user_message, "source": "test_harness"},
            turn_id=turn_id,
            conversation_id=conversation_id,
            parent_event_id=None,
        )

    last_input_event_id: int = user_event_id
    current_api_call_event_id: int | None = None
    spawning_api_call_event_id: int | None = None
    is_first_api_call: bool = True

    # Build message history from all prior events in this conversation
    with get_db() as conn:
        messages = build_messages(conn, conversation_id)
    messages.append({"role": "user", "content": user_message})

    while True:
        started_at = datetime.now(timezone.utc).isoformat()

        # Determine parent for this api_call per causality rules
        if is_first_api_call:
            api_call_parent = user_event_id
        elif spawning_api_call_event_id is not None:
            # Parallel tool calls: the api_call that follows their results points at
            # the api_call that spawned them, not any individual tool_result. This
            # keeps the causality tree a clean tree (no fan-in edges). See PROJECT_PLAN.md §3e.
            api_call_parent = spawning_api_call_event_id
            spawning_api_call_event_id = None  # consumed
        else:
            api_call_parent = last_input_event_id

        try:
            response = await client.messages.create(
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
            finished_at = datetime.now(timezone.utc).isoformat()
            api_call_payload = {
                "v": 1,
                "status": "success",
                "model_requested": "claude-haiku-4-5-20251001",
                "model_used": response.model,
                "request_id": getattr(response, "_request_id", None),
                "beta_headers": [],
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                },
                "cost_usd_millicents": None,
                "price_table_version": None,
                "error": None,
                "started_at": started_at,
                "finished_at": finished_at,
            }
        except Exception as api_err:
            finished_at = datetime.now(timezone.utc).isoformat()
            api_call_payload = {
                "v": 1,
                "status": "error",
                "model_requested": "claude-haiku-4-5-20251001",
                "model_used": None,
                "request_id": None,
                "beta_headers": [],
                "stop_reason": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "cost_usd_millicents": None,
                "price_table_version": None,
                "error": str(api_err),
                "started_at": started_at,
                "finished_at": finished_at,
            }
            with get_db() as conn:
                events_db.append(
                    conn,
                    "api_call",
                    api_call_payload,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    parent_event_id=api_call_parent,
                )
            raise

        with get_db() as conn:
            current_api_call_event_id = events_db.append(
                conn,
                "api_call",
                api_call_payload,
                turn_id=turn_id,
                conversation_id=conversation_id,
                parent_event_id=api_call_parent,
            )
        is_first_api_call = False

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            with get_db() as conn:
                events_db.append(
                    conn,
                    "assistant_message",
                    {
                        "v": 1,
                        "text": final_text,
                        "api_call_event_id": current_api_call_event_id,
                    },
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    parent_event_id=current_api_call_event_id,
                )
            return final_text

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Write tool_call event before dispatching; pass its id to the
                    # server via meta so it can set parent_event_id on tool_result.
                    with get_db() as conn:
                        tc_event_id = events_db.append(
                            conn,
                            "tool_call",
                            {
                                "v": 1,
                                "tool_name": block.name,
                                "tool_use_id": block.id,
                                "input": block.input,
                            },
                            turn_id=turn_id,
                            conversation_id=conversation_id,
                            parent_event_id=current_api_call_event_id,
                        )
                    result = await mcp.call_tool(
                        block.name,
                        block.input,
                        meta={
                            "turn_id": turn_id,
                            "conversation_id": conversation_id,
                            "tool_call_event_id": tc_event_id,
                            "tool_use_id": block.id,
                        },
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            # Track for causality: set spawning_api_call for parallel tool case
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if len(tool_use_blocks) > 1:
                # Parallel tool calls: next api_call parent = the api_call that spawned these
                spawning_api_call_event_id = current_api_call_event_id
            else:
                # Single tool: next api_call parent = this tool_result event
                with get_db() as conn:
                    tr_event_id = events_db.get_tool_result_event_id(
                        conn, tool_use_blocks[0].id
                    )
                if tr_event_id is not None:
                    last_input_event_id = tr_event_id

            messages.append({"role": "user", "content": tool_results})

        else:
            return f"Unexpected stop reason: {response.stop_reason}"
