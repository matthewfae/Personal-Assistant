# Personal-Assistant

A single-user personal assistant: a Python Telegram bot that routes incoming messages through the Claude API, backed by a local SQLite context database. Claude uses MCP tool calls to read and write the DB in a multi-step agent loop, then replies to the user via Telegram.

Not designed for external use or deployment — this is a personal project running on my own host.

## Status

Early. The agent loop, MCP tool plumbing, and an exploratory first-pass database layer are in place. An events-driven rebuild of the core architecture is the current focus. Telegram integration, monitoring, and hardening are deferred to later stages. See `PROJECT_PLAN.md` for the full stage breakdown and current focus.

## Repository layout

```
bot/         Agent loop, MCP client, prompt builder, test harness
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
- Target model: `claude-haiku-4-5-20251001`
