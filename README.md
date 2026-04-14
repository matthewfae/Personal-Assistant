# Personal-Assistant

A single-user personal assistant: a Python Telegram bot that routes incoming messages through the Claude API, backed by a local SQLite context database. Claude uses MCP tool calls to read and write the DB in a multi-step agent loop, then replies to the user via Telegram.

Not designed for external use or deployment — this is a personal project running on my own host.

## Status

Stage 3 complete as of 2026-04-14. The full events-driven architecture is in place: append-only `events` table, `turn_id`/`conversation_id` threading, messages-as-projection replacing in-memory history, `add_fact` as the sole tool with the MCP server owning the atomic triple-write transaction, and correct `parent_event_id` causality wiring throughout. The test harness (`bot/main.py`) drives the loop end-to-end against the real Claude API.

Next up: Stage 4 (read/search tools, added only when driven by a concrete need) and Stage 5 (Telegram integration). Monitoring and hardening are deferred to later stages. See `PROJECT_PLAN.md` for the full stage breakdown.

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

## Documentation

Read these in order depending on what you need:

- **`PROJECT_PLAN.md`** — start here. Goals, architectural decisions, stages, deferred work. Highest level.
- **`docs/DESIGN.md`** — implementation-level description of the current code: layout, layering rules, schema, agent flow. Middle level. Updated to reflect Stage 3.
- **`README.md`** — this file. Front door and orientation.

## Stack

- Python 3.11+, managed with `uv`
- `anthropic`, `mcp`, `python-telegram-bot`, `python-dotenv`
- `sqlite3` from stdlib
- Target model: `claude-haiku-4-5-20251001`
