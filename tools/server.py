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
import logging
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
import db.tasks as tasks_db
import db.shopping as shopping_db

logger = logging.getLogger(__name__)

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
                    "key": {"type": "string", "description": "Name of the fact", "maxLength": 256},
                    "value": {"type": "string", "description": "Value to store", "maxLength": 10000},
                    "category": {
                        "type": "string",
                        "description": "Grouping label (default: 'general')",
                        "maxLength": 64,
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
        types.Tool(
            name="add_task",
            description="Create a new task tracked by area.",
            inputSchema={
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "Area/project the task belongs to", "maxLength": 256},
                    "summary": {"type": "string", "description": "Short description of the task", "maxLength": 256},
                    "priority": {"type": "integer", "description": "Priority (lower = higher priority)"},
                    "estimated_hours": {"type": "number", "description": "Estimated hours to complete"},
                    "detail_json": {"type": "string", "description": "Additional structured details as JSON", "maxLength": 10000},
                    "status": {"type": "string", "description": "Task status (default: 'open')", "maxLength": 256},
                },
                "required": ["area", "summary"],
            },
        ),
        types.Tool(
            name="update_task",
            description="Update fields on an existing task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "ID of the task to update"},
                    "area": {"type": "string", "description": "Area/project the task belongs to", "maxLength": 256},
                    "summary": {"type": "string", "description": "Short description of the task", "maxLength": 256},
                    "status": {"type": "string", "description": "Task status", "maxLength": 256},
                    "priority": {"type": "integer", "description": "Priority (lower = higher priority)"},
                    "estimated_hours": {"type": "number", "description": "Estimated hours to complete"},
                    "detail_json": {"type": "string", "description": "Additional structured details as JSON", "maxLength": 10000},
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="list_tasks",
            description="List tasks, defaulting to all open tasks. Optionally filter by status and/or area.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (e.g. 'open', 'done')", "maxLength": 256},
                    "area": {"type": "string", "description": "Filter by area", "maxLength": 256},
                },
            },
        ),
        types.Tool(
            name="get_task",
            description="Get full details of a single task by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "ID of the task to retrieve"},
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="add_shopping",
            description="Add an item to the shopping list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "Item to add", "maxLength": 256},
                    "task_id": {"type": "integer", "description": "Optional linked task ID"},
                },
                "required": ["item"],
            },
        ),
        types.Tool(
            name="list_shopping",
            description="List all items on the shopping list.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="remove_shopping",
            description="Remove an item from the shopping list by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "shopping_id": {"type": "integer", "description": "ID of the shopping item to remove"},
                },
                "required": ["shopping_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception:
        logger.exception("Unexpected error in tool %r with arguments %r", name, arguments)
        raise
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, arguments: dict) -> str:
    """Route a tool call to the DB layer and format the result as a string."""
    if name == "add_fact":
        key = arguments["key"].strip()[:256]
        value = arguments["value"].strip()[:10000]
        if not key:
            return "Error: key cannot be empty."
        if not value:
            return "Error: value cannot be empty."
        fact = facts_db.add_fact(
            key=key,
            value=value,
            category=arguments.get("category", "general").strip()[:64] or "general",
        )
        verb = "Updated" if fact["operation"] == "updated" else "Stored"
        return f"{verb} fact [{fact['category']}] {fact['key']} = {fact['value']}"

    elif name == "set_context_bound":
        context_db.set_context_bound(int(arguments["from_event_id"]))
        return f"Context bound set to event id {arguments['from_event_id']}"

    elif name == "add_task":
        area = arguments["area"].strip()[:256]
        summary = arguments["summary"].strip()[:256]
        if not area:
            return "Error: area cannot be empty."
        if not summary:
            return "Error: summary cannot be empty."
        kwargs: dict = {"area": area, "summary": summary}
        if "priority" in arguments:
            kwargs["priority"] = arguments["priority"]
        if "estimated_hours" in arguments:
            kwargs["estimated_hours"] = arguments["estimated_hours"]
        if "detail_json" in arguments:
            kwargs["detail_json"] = arguments["detail_json"][:10000]
        if "status" in arguments:
            kwargs["status"] = arguments["status"][:256]
        task = tasks_db.add_task(**kwargs)
        return f"Created task #{task['id']}: {task['summary']} [{task['area']}]"

    elif name == "update_task":
        kwargs = {}
        for field in ("area", "summary", "status"):
            if field in arguments:
                kwargs[field] = arguments[field].strip()[:256]
        if "priority" in arguments:
            kwargs["priority"] = arguments["priority"]
        if "estimated_hours" in arguments:
            kwargs["estimated_hours"] = arguments["estimated_hours"]
        if "detail_json" in arguments:
            kwargs["detail_json"] = arguments["detail_json"][:10000]
        try:
            task = tasks_db.update_task(int(arguments["task_id"]), **kwargs)
        except ValueError as e:
            return str(e)
        return f"Updated task #{task['id']}: {task['summary']}"

    elif name == "list_tasks":
        filters: dict = {}
        if "status" in arguments:
            filters["status"] = arguments["status"][:256]
        if "area" in arguments:
            filters["area"] = arguments["area"][:256]
        rows = tasks_db.list_tasks(**filters)
        if not rows:
            return "No tasks found."
        lines = []
        for t in rows:
            lines.append(
                f"#{t['id']} [{t['area']}] {t['summary']} "
                f"(status: {t['status']}, priority: {t['priority']})"
            )
        return "\n".join(lines)

    elif name == "get_task":
        task = tasks_db.get_task(int(arguments["task_id"]))
        if task is None:
            return "Task not found."
        lines = [
            f"Task #{task['id']}",
            f"  Area: {task['area']}",
            f"  Summary: {task['summary']}",
            f"  Status: {task['status']}",
            f"  Priority: {task['priority']}",
            f"  Estimated hours: {task['estimated_hours']}",
            f"  Detail JSON: {task['detail_json']}",
            f"  Created: {task['created_at']}",
            f"  Updated: {task['updated_at']}",
        ]
        return "\n".join(lines)

    elif name == "add_shopping":
        item_str = arguments["item"].strip()[:256]
        if not item_str:
            return "Error: item cannot be empty."
        kwargs = {"item": item_str}
        if "task_id" in arguments:
            kwargs["task_id"] = arguments["task_id"]
        item = shopping_db.add_shopping(**kwargs)
        return f"Added to shopping list: {item['item']}"

    elif name == "list_shopping":
        items = shopping_db.list_shopping()
        if not items:
            return "Shopping list is empty."
        return "\n".join(items)

    elif name == "remove_shopping":
        removed = shopping_db.remove_shopping(int(arguments["shopping_id"]))
        if removed:
            return "Removed from shopping list."
        return "Item not found."

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
