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

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel.server import Server

from db.connection import init_db
import db.facts as facts_db

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
            name="get_fact",
            description="Retrieve a single fact by category and key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "category": {"type": "string", "description": "Default: 'general'"},
                },
                "required": ["key"],
            },
        ),
        types.Tool(
            name="search_facts",
            description=(
                "Search facts whose key or value contains the query string. "
                "Optionally restrict to a category."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="list_facts",
            description="List stored facts, most recently updated first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Filter by category"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
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

    if name == "get_fact":
        fact = facts_db.get_fact(
            key=arguments["key"],
            category=arguments.get("category", "general"),
        )
        if fact is None:
            return f"No fact found for [{arguments.get('category', 'general')}] {arguments['key']}"
        return f"[{fact['category']}] {fact['key']} = {fact['value']}"

    if name == "search_facts":
        results = facts_db.search_facts(
            query=arguments["query"],
            category=arguments.get("category"),
        )
        if not results:
            return f"No facts found matching '{arguments['query']}'"
        return "\n".join(f"[{r['category']}] {r['key']} = {r['value']}" for r in results)

    if name == "list_facts":
        results = facts_db.list_facts(
            category=arguments.get("category"),
            limit=arguments.get("limit", 20),
        )
        if not results:
            return "No facts stored yet."
        return "\n".join(f"[{r['category']}] {r['key']} = {r['value']}" for r in results)

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
