"""
MCP server exposing personal assistant tools.

Run as a subprocess by the bot's MCP client. Communicates over stdio
using the JSON-RPC-based Model Context Protocol.
"""

import asyncio

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

server = Server("personal-assistant-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="add_fact",
            description="Add a fact to the knowledge base",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to store the fact under",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value of the fact",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for organization",
                    },
                },
                "required": ["key", "value"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "add_fact":
        key = arguments["key"]
        value = arguments["value"]
        category = arguments.get("category", "general")
        # Placeholder — will write to SQLite in Stage 3
        return [
            types.TextContent(
                type="text",
                text=f"Stored fact: {key}={value} (category: {category})",
            )
        ]
    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
