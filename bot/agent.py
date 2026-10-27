"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Tools are discovered from the MCP server at startup rather than hardcoded here.
Implements prompt caching for cost reduction on repeated context.
"""

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

import db.context as context_db
from db.connection import get_db
from mcp_client import MCPClient
from prompt_builder import PromptBuilder, format_time

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-5-5"

# Thinking is on by default on Sonnet 5.5 and counts toward output tokens.
# Effort controls how much it thinks: "medium" suits multi-step tool use;
# drop to "low" to cut cost further on chat-style turns.
_EFFORT = "medium"

# Thinking also counts toward max_tokens, so leave room beyond the reply itself.
_MAX_TOKENS = 16000

# Upper bound on API calls per turn, so a model that
# keeps calling tools can't run up unbounded cost.
_MAX_API_CALLS = 15

# Hard cap on history: a turn never sees more than this many turns, even if the
# weekly trim_context job hasn't run. The bound only ever moves forward.
_MAX_TURNS = 30

# MCP tools hidden from turns unless a job asks for them (Job.extra_tools).
_RESTRICTED_TOOLS = frozenset({"set_context_bound"})

_system_prompt = PromptBuilder("system.md")


@dataclass
class TurnResult:
    reply: str


@dataclass
class LocalTool:
    """A tool run in-process by the agent instead of through the MCP server.

    For tools that need the running process, such as notify_user, which sends
    through a channel. `handler` takes the tool input and returns the result text.
    """

    definition: dict  # Anthropic API tool definition
    handler: Callable[[dict], Awaitable[str]]


def write_event(conn, turn_id, event_type, payload, parent_event_id=None, timestamp=None) -> int:
    """Insert one event row and return its id. conn must be an open sqlite3 connection.

    timestamp is an aware datetime; it defaults to now.
    """
    cursor = conn.execute(
        "INSERT INTO events (timestamp, turn_id, type, parent_event_id, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            (timestamp or datetime.now(timezone.utc)).isoformat(),
            turn_id,
            event_type,
            parent_event_id,
            json.dumps(payload),
        ),
    )
    return cursor.lastrowid


def _api_call_payload(response) -> dict:
    usage = response.usage
    return {
        "v": 1,
        "model": _MODEL,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        # Cache counts may be absent (None); record 0 so they can be summed.
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
    }


def render_wakeup(job: str, prompt: str) -> str:
    """Text of a wake-up turn root as Claude sees it (before the timestamp prefix)."""
    return f"(wake-up: {job})\n{prompt}"


def _timestamped(text: str, timestamp: str, event_id: int | None = None) -> str:
    """Prefix a user message with the time it was sent, and its event id if given.

    The time comes from the event's stored timestamp, so a message renders
    identically every time it is projected and the cached prefix stays valid.
    """
    when = format_time(datetime.fromisoformat(timestamp))
    return f"[#{event_id} · {when}]\n{text}" if event_id is not None else f"[{when}]\n{text}"


def project_messages(from_event_id: int = 0, show_ids: bool = False) -> list[dict]:
    """Build the messages array from events at or after from_event_id.

    Reconstructs the full interleaved sequence: turn roots (user messages and
    wake-ups), tool use/result pairs (grouped by api_call), and final assistant
    messages. Each turn root is prefixed with its time (see _timestamped).

    show_ids labels turn roots, tool results and assistant messages with their
    event ids, for the trim_context job, which picks a bound by id. Other turns
    don't see ids: Claude tends to copy a label pattern from its own past replies.
    """
    def label(event_id: int, text: str) -> str:
        return f"[#{event_id}] {text}" if show_ids else text

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, type, parent_event_id, payload
            FROM events
            WHERE id >= ?
              AND type IN ('user_message', 'wakeup', 'api_call', 'tool_call', 'tool_result', 'assistant_message')
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
            messages.append({"role": "user", "content": _timestamped(
                payload["content"], row["timestamp"], row["id"] if show_ids else None)})

        elif etype == "wakeup":
            text = render_wakeup(payload["job"], payload["prompt"])
            messages.append({"role": "user", "content": _timestamped(
                text, row["timestamp"], row["id"] if show_ids else None)})

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
                    "content": label(tr["id"], json.loads(tr["payload"])["content"]),
                    # Older events predate is_error; treat as success.
                    "is_error": json.loads(tr["payload"]).get("is_error", False),
                }
                for tr in tool_results_by_api.get(row["id"], [])
            ]
            if result_blocks:
                messages.append({"role": "user", "content": result_blocks})

        elif etype == "assistant_message":
            if payload["content"]:
                messages.append({"role": "assistant", "content": label(row["id"], payload["content"])})

    return messages


def _load_from_event_id() -> int:
    """Return the projection bound: the stored context bound, capped at _MAX_TURNS turns."""
    return max(context_db.get_context_bound(), context_db.turn_root_id(_MAX_TURNS))


async def _execute_tool_calls(
    mcp: MCPClient,
    turn_id: str,
    response,
    api_call_id: int,
    local_tools: dict[str, LocalTool] | None = None,
) -> tuple[list[dict], int | None]:
    """Run every tool_use block in response, writing tool_call/tool_result events.

    Tools named in local_tools run in-process; everything else goes to MCP.

    Returns (tool_result blocks for the next request, id of the last tool_result event).

    A tool_result is always written for every tool_call, even if the MCP call
    itself raises. Otherwise the event log would hold a tool_use with no
    matching tool_result, and every later projection would be rejected by the API.
    """
    tool_results = []
    last_tool_result_id: int | None = None
    for block in response.content:
        if block.type != "tool_use":
            continue
        with get_db() as conn:
            tool_call_id = write_event(conn, turn_id, "tool_call", {
                "v": 1,
                "tool_use_id": block.id,
                "name": block.name,
                "input": block.input,
            }, parent_event_id=api_call_id)
        try:
            if local_tools and block.name in local_tools:
                content, is_error = await local_tools[block.name].handler(block.input), False
            else:
                output = await mcp.call_tool(block.name, block.input)
                content, is_error = output.content, output.is_error
        except Exception as e:
            content, is_error = f"Tool call failed: {type(e).__name__}: {e}", True
        with get_db() as conn:
            last_tool_result_id = write_event(conn, turn_id, "tool_result", {
                "v": 1,
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            }, parent_event_id=tool_call_id)
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
            "is_error": is_error,
        })
    return tool_results, last_tool_result_id


async def run_loop(
    client: AsyncAnthropic,
    mcp: MCPClient,
    user_message: str,
    turn_id: str | None = None,
    job: str | None = None,
    local_tools: list[LocalTool] | None = None,
    extra_tools: frozenset[str] = frozenset(),
) -> TurnResult:
    """
    Run the agent loop for one turn.

    Projects the message history from the events table, appends the new turn
    root, and runs until Claude produces a final text response.

    If job is given, this is a wake-up turn: user_message is the job's prompt
    and the root event is `wakeup` instead of `user_message`. local_tools are
    offered to Claude alongside the MCP tools and run in-process (e.g.
    notify_user for wake-ups). extra_tools names restricted MCP tools
    (_RESTRICTED_TOOLS) this turn may use.

    turn_id may be supplied by the caller (e.g. a transport handler that has
    already written a pre-turn event under the same id). If omitted, a fresh
    UUID is generated.

    Logs a one-line summary of the turn when it ends, whether or not it succeeded.
    """
    if turn_id is None:
        turn_id = uuid.uuid4().hex

    start = time.monotonic()
    try:
        return await _run_turn(client, mcp, user_message, turn_id, job, local_tools or [], extra_tools)
    finally:
        _log_turn_summary(turn_id, time.monotonic() - start)


_TOKEN_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def _log_turn_summary(turn_id: str, elapsed: float) -> None:
    """Log the turn's API call count and token totals, and a WARNING per error event.

    Reads the turn's events back from the DB. Failures here are logged and swallowed; they must not affect the turn.
    """
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, type, payload FROM events "
                "WHERE turn_id = ? AND type IN ('api_call', 'error') ORDER BY id",
                (turn_id,),
            ).fetchall()

        api_calls = 0
        totals = dict.fromkeys(_TOKEN_FIELDS, 0)
        for row in rows:
            payload = json.loads(row["payload"])
            if row["type"] == "api_call":
                api_calls += 1
                for field in _TOKEN_FIELDS:
                    totals[field] += payload.get(field) or 0
            else:
                logger.warning(
                    "Turn %s: %s error (event id=%d): %s",
                    turn_id, payload.get("context"), row["id"], payload.get("message"),
                )

        logger.info(
            "Turn %s finished in %.1fs: %d API calls, tokens in=%d cache_read=%d cache_write=%d out=%d",
            turn_id, elapsed, api_calls,
            totals["input_tokens"], totals["cache_read_input_tokens"],
            totals["cache_creation_input_tokens"], totals["output_tokens"],
        )
    except Exception:
        logger.exception("Could not summarize turn %s", turn_id)


async def _run_turn(
    client: AsyncAnthropic,
    mcp: MCPClient,
    user_message: str,
    turn_id: str,
    job: str | None,
    local_tools: list[LocalTool],
    extra_tools: frozenset[str],
) -> TurnResult:
    """Body of run_loop; see its docstring."""
    with get_db() as conn:
        if job is None:
            root_id = write_event(
                conn, turn_id, "user_message",
                {"v": 1, "content": user_message},
                parent_event_id=None,
            )
        else:
            root_id = write_event(
                conn, turn_id, "wakeup",
                {"v": 1, "job": job, "prompt": user_message},
                parent_event_id=None,
            )

    # Project after writing the root, so it is rendered from its stored
    # timestamp exactly as later turns will render it.
    from_event_id = _load_from_event_id()
    # Whoever may trim the history sees event ids, so it can name a bound.
    messages = project_messages(from_event_id, show_ids="set_context_bound" in extra_tools)

    # For the first api_call, parent is the turn root.
    # For subsequent api_calls, parent is the last tool_result written in the previous round.
    next_api_call_parent_id: int = root_id

    # Restricted tools (set_context_bound) are hidden unless the job asks for them.
    # Local tools go last. Re-attach cache_control to the new last tool so the
    # whole tool list stays cached.
    main_tools = [
        {k: v for k, v in t.items() if k != "cache_control"}
        for t in mcp.tools if t["name"] not in _RESTRICTED_TOOLS - extra_tools
    ] + [t.definition for t in local_tools]
    if main_tools:
        main_tools[-1] = {**main_tools[-1], "cache_control": {"type": "ephemeral"}}
    local_tools_by_name = {t.definition["name"]: t for t in local_tools}

    for _ in range(_MAX_API_CALLS):
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            output_config={"effort": _EFFORT},
            system=[
                {
                    "type": "text",
                    "text": _system_prompt.build(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=main_tools,
            messages=messages,
            # Automatic caching: also caches the conversation so far, so later
            # API calls in the same turn reuse it.
            cache_control={"type": "ephemeral"},
        )

        with get_db() as conn:
            api_call_id = write_event(
                conn, turn_id, "api_call", _api_call_payload(response),
                parent_event_id=next_api_call_parent_id,
            )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            with get_db() as conn:
                write_event(
                    conn, turn_id, "assistant_message",
                    {"v": 1, "content": final_text},
                    parent_event_id=api_call_id,
                )
            return TurnResult(reply=final_text)

        elif response.stop_reason == "tool_use":
            tool_results, last_tool_result_id = await _execute_tool_calls(
                mcp, turn_id, response, api_call_id, local_tools_by_name,
            )
            messages.append({"role": "user", "content": tool_results})
            # Next api_call's parent is the last tool_result written in this round.
            if last_tool_result_id is not None:
                next_api_call_parent_id = last_tool_result_id

        elif response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            logger.warning("Turn %s: Claude refused (category: %s)", turn_id, category)
            return TurnResult(reply=f"Claude declined this request (category: {category}).")

        else:
            logger.warning("Turn %s: unexpected stop reason %r", turn_id, response.stop_reason)
            return TurnResult(reply=f"Unexpected stop reason: {response.stop_reason}")

    message = f"Stopped after {_MAX_API_CALLS} API calls without a final response"
    with get_db() as conn:
        write_event(
            conn, turn_id, "error",
            {"v": 1, "context": "main_turn" if job is None else "wakeup", "message": message},
            parent_event_id=next_api_call_parent_id,
        )
    return TurnResult(reply=f"{message}. Try rephrasing or breaking the request up.")


def debug_causality_tree(turn_id: str) -> str:
    """Return a text representation of the causality tree for a turn.

    Queries all events for the given turn_id, builds a parent→children map,
    then recursively formats the tree with indentation reflecting depth.

    Example output::

        user_message (id=1)
          api_call (id=2, in=120, out=45)
            tool_call (id=3, name=add_task)
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
