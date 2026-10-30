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
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure the project root is on sys.path so that the db/ package is importable
# when this script is run directly as a subprocess.
sys.path.insert(0, str(Path(__file__).parent.parent))

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel.server import Server

from bot.log_setup import configure_logging
from db.connection import init_db
import db.context as context_db
import db.tasks as tasks_db
import db.shopping as shopping_db
import db.query as query_db
import db.symptoms as symptoms_db
import integrations.icloud_calendar as calendar_api

logger = logging.getLogger(__name__)

server = Server("personal-assistant-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="set_context_bound",
            description=(
                "Trim the conversation history that future turns see. Pass the event id "
                "(the #N labels) of the oldest thing to keep; everything before it is "
                "dropped. An id in the middle of a turn keeps that whole turn. Keep "
                "anything still unresolved or likely to come up again. Always include a "
                "brief rationale."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_event_id": {
                        "type": "integer",
                        "description": "Event id of the oldest thing to keep.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Brief explanation of what is being trimmed and why it's no longer needed",
                        "maxLength": 500,
                    },
                },
                "required": ["from_event_id", "rationale"],
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
            description=(
                "List tasks. Defaults to all open tasks. "
                "Pass status='all' to include every status. "
                "Optionally filter by area or search summary text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status ('open', 'done', 'waiting', 'someday', or 'all')", "maxLength": 256},
                    "area": {"type": "string", "description": "Filter by area", "maxLength": 256},
                    "search": {"type": "string", "description": "Case-insensitive substring match on summary", "maxLength": 256},
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
            description="List all items on the shopping list. Returns id and item name for each entry.",
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
        types.Tool(
            name="query",
            description=(
                "Run a read-only SQL query (SELECT, or WITH ... SELECT) against the database. "
                "Returns up to 200 rows. "
                "Tables: "
                "tasks (id, area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at), "
                "shopping (id, item, task_id, created_at), "
                "symptoms (id, symptom, severity, occurred_at, notes, logged_at), "
                "events (id, timestamp, turn_id, type, parent_event_id, payload). "
                "JOINs, aggregations, ORDER BY, and WHERE clauses are all supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single read-only statement. Writes are rejected by the database.",
                    },
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="log_symptom",
            description=(
                "Record a symptom occurrence. Use occurred_at to capture when the symptom happened "
                "(ISO-8601 UTC string) — defaults to now if omitted. Severity is 1–10."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symptom": {"type": "string", "description": "Name or description of the symptom", "maxLength": 256},
                    "severity": {"type": "integer", "description": "Severity on a 1–10 scale", "minimum": 1, "maximum": 10},
                    "occurred_at": {"type": "string", "description": "ISO-8601 UTC timestamp of when the symptom occurred (defaults to now)"},
                    "notes": {"type": "string", "description": "Optional freeform context or detail", "maxLength": 2000},
                },
                "required": ["symptom"],
            },
        ),
        types.Tool(
            name="update_symptom",
            description="Correct or add notes to a previously logged symptom.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symptom_id": {"type": "integer", "description": "ID of the symptom log to update"},
                    "symptom": {"type": "string", "description": "Corrected symptom name", "maxLength": 256},
                    "severity": {"type": "integer", "description": "Corrected severity (1–10)", "minimum": 1, "maximum": 10},
                    "occurred_at": {"type": "string", "description": "Corrected ISO-8601 UTC occurrence timestamp"},
                    "notes": {"type": "string", "description": "Updated notes", "maxLength": 2000},
                },
                "required": ["symptom_id"],
            },
        ),
        types.Tool(
            name="list_symptoms",
            description=(
                "List logged symptoms, most recent first. "
                "Optionally filter by symptom name (substring match) or since a date. "
                "Defaults to last 100 entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symptom": {"type": "string", "description": "Case-insensitive substring match on symptom name", "maxLength": 256},
                    "since": {"type": "string", "description": "ISO-8601 UTC timestamp — only return symptoms occurring on or after this time"},
                    "limit": {"type": "integer", "description": "Max results (default 100)", "minimum": 1, "maximum": 500},
                },
            },
        ),
        types.Tool(
            name="get_symptom",
            description="Get full details of a single symptom log by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symptom_id": {"type": "integer", "description": "ID of the symptom log to retrieve"},
                },
                "required": ["symptom_id"],
            },
        ),
        types.Tool(
            name="delete_symptom",
            description="Delete a symptom log by ID (use when logged in error).",
            inputSchema={
                "type": "object",
                "properties": {
                    "symptom_id": {"type": "integer", "description": "ID of the symptom log to delete"},
                },
                "required": ["symptom_id"],
            },
        ),
        types.Tool(
            name="list_events",
            description=(
                "List Matt's iCloud calendar events (all calendars unless one is named), "
                "recurring events expanded. Dates are local, YYYY-MM-DD; the range is "
                "start inclusive, end exclusive. Defaults: today through 7 days out."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "First day, YYYY-MM-DD"},
                    "end": {"type": "string", "description": "Day after the last day, YYYY-MM-DD"},
                    "calendar": {"type": "string", "description": "Calendar name, e.g. Home or Work"},
                },
            },
        ),
        types.Tool(
            name="add_event",
            description=(
                "Add an event to Matt's iCloud calendar; it syncs to his iPhone. Times are "
                "local: YYYY-MM-DDTHH:MM for a timed event, YYYY-MM-DD for all day. An alert "
                "becomes a phone notification, so use one when the event is a reminder. "
                "Defaults: the Home calendar, 1 hour (or 1 day if all day), no alert."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 256},
                    "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, or YYYY-MM-DD for all day"},
                    "end": {"type": "string", "description": "Same format as start"},
                    "calendar": {"type": "string", "description": "Calendar name, e.g. Home or Work"},
                    "alert_minutes": {"type": "integer", "description": "Minutes before start to alert (0 = at start)"},
                    "location": {"type": "string", "maxLength": 256},
                    "notes": {"type": "string", "maxLength": 2000},
                },
                "required": ["title", "start"],
            },
        ),
        types.Tool(
            name="delete_event",
            description=(
                "Delete a one-off calendar event by its uid (from list_events or add_event). "
                "Recurring events can't be deleted here."
            ),
            inputSchema={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
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
    if name == "set_context_bound":
        requested = int(arguments["from_event_id"])
        from_event_id = context_db.turn_start(requested)
        context_db.set_context_bound(from_event_id)
        if from_event_id != requested:
            return f"Event {requested} is mid-turn; context now starts at that turn's start, event {from_event_id}"
        return f"Context now starts at event {from_event_id}"

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
        if "search" in arguments:
            filters["search"] = arguments["search"][:256]
        rows = tasks_db.list_tasks(**filters)
        if not rows:
            return "No tasks found."
        lines = []
        for t in rows:
            parts = [
                f"#{t['id']} [{t['area']}] {t['summary']}",
                f"status={t['status']}",
                f"priority={t['priority']}",
                f"est={t['estimated_hours']}h" if t["estimated_hours"] is not None else "est=none",
            ]
            if t["detail_json"]:
                parts.append(f"detail={t['detail_json']}")
            lines.append(" | ".join(parts))
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
        return "\n".join(f"#{i['id']} {i['item']}" for i in items)

    elif name == "remove_shopping":
        removed = shopping_db.remove_shopping(int(arguments["shopping_id"]))
        if removed:
            return "Removed from shopping list."
        return "Item not found."

    elif name == "query":
        try:
            rows = query_db.run_query(arguments["sql"])
        except ValueError as e:
            return f"Error: {e}"
        if not rows:
            return "No rows returned."
        headers = list(rows[0].keys())
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(v) if v is not None else "NULL" for v in row.values()))
        return "\n".join(lines)

    elif name == "log_symptom":
        symptom_str = arguments["symptom"].strip()[:256]
        if not symptom_str:
            return "Error: symptom cannot be empty."
        kwargs: dict = {"symptom": symptom_str}
        if "severity" in arguments:
            kwargs["severity"] = int(arguments["severity"])
        if "occurred_at" in arguments:
            kwargs["occurred_at"] = arguments["occurred_at"]
        if "notes" in arguments:
            kwargs["notes"] = arguments["notes"][:2000]
        s = symptoms_db.log_symptom(**kwargs)
        sev = f"severity={s['severity']}" if s["severity"] is not None else "severity=none"
        return f"Logged symptom #{s['id']}: {s['symptom']} ({sev}, at {s['occurred_at'][:16]})"

    elif name == "update_symptom":
        kwargs = {}
        if "symptom" in arguments:
            kwargs["symptom"] = arguments["symptom"].strip()[:256]
        if "severity" in arguments:
            kwargs["severity"] = int(arguments["severity"])
        if "occurred_at" in arguments:
            kwargs["occurred_at"] = arguments["occurred_at"]
        if "notes" in arguments:
            kwargs["notes"] = arguments["notes"][:2000]
        try:
            s = symptoms_db.update_symptom(int(arguments["symptom_id"]), **kwargs)
        except ValueError as e:
            return str(e)
        return f"Updated symptom #{s['id']}: {s['symptom']}"

    elif name == "list_symptoms":
        filters: dict = {}
        if "symptom" in arguments:
            filters["symptom"] = arguments["symptom"][:256]
        if "since" in arguments:
            filters["since"] = arguments["since"]
        if "limit" in arguments:
            filters["limit"] = min(int(arguments["limit"]), 500)
        rows = symptoms_db.list_symptoms(**filters)
        if not rows:
            return "No symptoms found."
        lines = []
        for s in rows:
            sev = str(s["severity"]) if s["severity"] is not None else "none"
            parts = [f"#{s['id']} {s['symptom']}", f"severity={sev}", s["occurred_at"][:16]]
            if s["notes"]:
                parts.append(f"notes={s['notes'][:80]}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    elif name == "get_symptom":
        s = symptoms_db.get_symptom(int(arguments["symptom_id"]))
        if s is None:
            return "Symptom log not found."
        lines = [
            f"Symptom #{s['id']}",
            f"  Symptom: {s['symptom']}",
            f"  Severity: {s['severity']}",
            f"  Occurred at: {s['occurred_at']}",
            f"  Notes: {s['notes']}",
            f"  Logged at: {s['logged_at']}",
        ]
        return "\n".join(lines)

    elif name == "delete_symptom":
        deleted = symptoms_db.delete_symptom(int(arguments["symptom_id"]))
        if deleted:
            return "Symptom log deleted."
        return "Symptom log not found."

    elif name == "list_events":
        today = datetime.now(calendar_api.TZ).date()
        try:
            start = date.fromisoformat(arguments["start"]) if "start" in arguments else today
            end = date.fromisoformat(arguments["end"]) if "end" in arguments else start + timedelta(days=7)
            events = calendar_api.list_events(start, end, arguments.get("calendar"))
        except (ValueError, calendar_api.NotConfigured) as e:
            return f"Error: {e}"
        if not events:
            return f"No events from {start} to {end} (end exclusive)."
        return "\n".join(format_event(e) for e in events)

    elif name == "add_event":
        try:
            start = calendar_api.parse_when(arguments["start"])
            end = calendar_api.parse_when(arguments["end"]) if "end" in arguments else None
            event = calendar_api.add_event(
                arguments["title"].strip()[:256], start, end,
                calendar=arguments.get("calendar"),
                alert_minutes=arguments.get("alert_minutes"),
                location=arguments.get("location"),
                notes=arguments.get("notes"),
            )
        except (ValueError, calendar_api.NotConfigured) as e:
            return f"Error: {e}"
        alert = f" (alert {arguments['alert_minutes']} min before)" if "alert_minutes" in arguments else ""
        return f"Added: {format_event(event)}{alert}"

    elif name == "delete_event":
        try:
            event = calendar_api.delete_event(arguments["uid"])
        except (ValueError, calendar_api.NotConfigured) as e:
            return f"Error: {e}"
        if event is None:
            return "Event not found."
        return f"Deleted: {format_event(event)}"

    raise ValueError(f"Unknown tool: {name}")


def format_event(e: dict) -> str:
    """One line per event, e.g. 'Sat Nov 7, 10:00 AM–11:00 AM | Fix mirror | Home | uid=...'."""
    start, end = e["start"], e["end"]
    if e["all_day"]:
        last = end - timedelta(days=1)
        when = f"{start:%a %b %-d}" + (f"–{last:%a %b %-d}" if last > start else "") + ", all day"
    elif start.date() == end.date():
        when = f"{start:%a %b %-d, %-I:%M %p}–{end:%-I:%M %p}"
    else:
        when = f"{start:%a %b %-d, %-I:%M %p} – {end:%a %b %-d, %-I:%M %p}"
    parts = [when, e["title"], e["calendar"]]
    if e["location"]:
        parts.append(e["location"])
    if e["recurring"]:
        parts.append("recurring")
    parts.append(f"uid={e['uid']}")
    return " | ".join(parts)


async def main():
    init_db()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    # Logs go to stderr; stdout carries the MCP protocol and must stay clean.
    configure_logging()
    # The MCP SDK logs every request at INFO ("Processing request of type ...").
    logging.getLogger("mcp").setLevel(logging.WARNING)
    asyncio.run(main())
