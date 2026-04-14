"""
MCP server exposing personal assistant tools.

Run as a subprocess by the bot's MCP client. Communicates over stdio
using the JSON-RPC-based Model Context Protocol.

This module is the boundary between MCP and the DB layer:
  - Tool schemas are defined here (what Claude sees).
  - Handlers call into db/ and format results as plain strings for Claude.
  - No SQL lives here; no MCP types leak into db/.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

# Ensure the project root is on sys.path so db.* imports resolve whether the
# server is run directly or spawned as a subprocess by the MCP client.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel.server import Server

from db.connection import get_db, get_db_path, init_db
import db.facts as facts_db
import db.mutation_log as mutation_log
import db.events as events_db

server = Server("personal-assistant-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="add_fact",
            description=(
                "Store a fact. If the (category, key) pair already exists, "
                "the value is updated in place."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Name of the fact"},
                    "value": {"type": "string", "description": "Value to store"},
                    "category": {
                        "type": "string",
                        "description": "Grouping label (default: 'general')",
                    },
                },
                "required": ["key", "value"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "add_fact":
        raise ValueError(f"Unknown tool: {name}")

    # Extract meta from request context
    ctx = server.request_context
    meta = ctx.meta if ctx else None
    turn_id = meta.model_extra.get("turn_id") if meta else None
    conversation_id = meta.model_extra.get("conversation_id") if meta else None
    tool_call_event_id = meta.model_extra.get("tool_call_event_id") if meta else None
    tool_use_id = meta.model_extra.get("tool_use_id") if meta else None

    key = arguments["key"]
    value = arguments["value"]
    category = arguments.get("category", "general")

    # Open a connection in autocommit mode for full manual transaction control.
    # isolation_level=None prevents Python's sqlite3 from auto-issuing BEGIN DEFERRED,
    # which would conflict with our explicit BEGIN IMMEDIATE.
    conn = sqlite3.connect(get_db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    try:
        conn.execute("BEGIN IMMEDIATE")

        result = facts_db.add_fact(conn, key=key, value=value, category=category)

        verb = "Updated" if result["operation"] == "updated" else "Stored"
        result_string = f"{verb} fact [{result['category']}] {result['key']} = {result['value']}"

        mutation_log.log(
            conn,
            "facts",
            result["operation"],
            result["id"],
            {
                "category": result["category"],
                "key": result["key"],
                "value": result["value"],
            },
        )

        payload = {
            "v": 1,
            "tool_use_id": tool_use_id,
            "tool_call_event_id": tool_call_event_id,
            "is_error": False,
            "content": result_string,
        }
        events_db.append(
            conn,
            "tool_result",
            payload,
            turn_id,
            conversation_id,
            parent_event_id=tool_call_event_id,
        )

        conn.execute("COMMIT")
        conn.close()

        return [types.TextContent(type="text", text=result_string)]

    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()

        # Write the error tool_result event on a fresh connection since the
        # original transaction is in a failed state.
        error_payload = {
            "v": 1,
            "tool_use_id": tool_use_id,
            "tool_call_event_id": tool_call_event_id,
            "is_error": True,
            "content": str(e),
        }
        try:
            with get_db() as err_conn:
                events_db.append(
                    err_conn,
                    "tool_result",
                    error_payload,
                    turn_id,
                    conversation_id,
                    parent_event_id=tool_call_event_id,
                )
        except Exception:
            pass  # Best-effort; don't mask the original error

        return [types.TextContent(type="text", text=str(e))]


async def main():
    init_db()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
