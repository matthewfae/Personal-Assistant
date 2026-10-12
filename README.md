# Personal-Assistant

A single-user personal assistant: a Python Telegram bot that routes incoming messages through the Claude API, backed by a local SQLite context database. Claude uses MCP tool calls to read and write the DB in a multi-step agent loop, then replies to the user via Telegram.

Not designed for external use or deployment — this is a personal project running on my own host.

## Status

Stages 1–8 are complete. The agent loop records every API call, tool call, and message as an immutable event row. Conversation history is projected from the events table at the start of each turn. After each reply, a reflection step lets Claude trim the context window (`set_context_bound`), use tools, or send a follow-up message. Claude manages tasks, a shopping list, and symptom logs through MCP tools, and can run read-only SQL over any table. The bot runs as a `systemd` service. See `PROJECT_PLAN.md` for the stage breakdown and current TODOs.

## Repository layout

```
bot/         Entry point, Telegram handler, agent loop, MCP client, prompt builder, test harness
tools/       MCP server (tool schemas and dispatch)
db/          SQLite access layer (connection, schema, tables, mutation log)
prompts/     System prompt template
docs/        Implementation-level design docs
tests/       Tests
scripts/     Helper scripts
logs/        Runtime log output
```

## Docs

- **`PROJECT_PLAN.md`** — architecture decisions, current stage, upcoming stages.
- **`docs/DESIGN.md`** — layout, schema, agent flow, and current tool surface.

## Stack

- Python 3.11+, managed with `uv`
- `anthropic`, `mcp`, `python-telegram-bot`, `python-dotenv`
- `sqlite3` from stdlib
- Target model: configured in `bot/agent.py`
