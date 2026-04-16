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
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that the db/ package is importable
# when this script is run directly as a subprocess.
sys.path.insert(0, str(Path(__file__).parent.parent))

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel.server import Server

from db.connection import init_db
import db.facts as facts_db
import db.context as context_db

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
        types.Tool(
            name="set_context_bound",
            description=(
                "Set the context projection bound. The next turn will only include events "
                "with id >= from_event_id when building the conversation history. "
                "Choose aggressively — exclude anything no longer needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_event_id": {
                        "type": "integer",
                        "description": "ID of the oldest event to include in future projections",
                    },
                },
                "required": ["from_event_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    result = _dispatch(name, arguments)
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, arguments: dict) -> str:
    """Route a tool call to the DB layer and format the result as a string."""
    if name == "add_fact":
        fact = facts_db.add_fact(
            key=arguments["key"],
            value=arguments["value"],
            category=arguments.get("category", "general"),
        )
        verb = "Updated" if fact["operation"] == "updated" else "Stored"
        return f"{verb} fact [{fact['category']}] {fact['key']} = {fact['value']}"

    elif name == "set_context_bound":
        context_db.set_context_bound(int(arguments["from_event_id"]))
        return f"Context bound set to event id {arguments['from_event_id']}"

    raise ValueError(f"Unknown tool: {name}")


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
