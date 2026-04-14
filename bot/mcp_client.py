"""
MCP client: spawns the tools server as a subprocess and provides a simple
interface for the agent to list tools and dispatch tool calls.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Absolute path to the server script, regardless of working directory
_TOOLS_SERVER = Path(__file__).parent.parent / "tools" / "server.py"


class MCPClient:
    """Thin wrapper around a live ClientSession."""

    def __init__(self, session: ClientSession, tools: list[dict]):
        self._session = session
        self._tools = tools

    @property
    def tools(self) -> list[dict]:
        """Tool definitions formatted for the Anthropic API."""
        return self._tools

    async def call_tool(self, name: str, arguments: dict, meta: dict | None = None) -> str:
        result = await self._session.call_tool(name, arguments, meta=meta)
        return "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )


@asynccontextmanager
async def mcp_client():
    """
    Async context manager that spawns the MCP server subprocess, performs
    the initialization handshake, fetches the tool list, and yields an
    MCPClient ready to use.

    Usage:
        async with mcp_client() as client:
            tools = client.tools       # list[dict] for Anthropic API
            result = await client.call_tool("add_fact", {...})
    """
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(_TOOLS_SERVER)],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()

            # Convert MCP Tool objects to the dict format the Anthropic SDK expects.
            # The last tool gets cache_control so Claude caches the entire tool list.
            tools = [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                }
                for tool in tools_result.tools
            ]
            if tools:
                tools[-1]["cache_control"] = {"type": "ephemeral"}

            yield MCPClient(session, tools)
