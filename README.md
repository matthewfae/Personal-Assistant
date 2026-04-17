# Personal-Assistant

A single-user personal assistant: a Python Telegram bot that routes incoming messages through the Claude API, backed by a local SQLite context database. Claude uses MCP tool calls to read and write the DB in a multi-step agent loop, then replies to the user via Telegram.

Not designed for external use or deployment — this is a personal project running on my own host.

## Status

Stages 1–5 are complete. The agent loop writes every API call, tool call, and message as an immutable event row; conversation history is projected from the events table at turn start; a post-turn meta-call sets the projection bound for the next turn as a `context_decision` event; the Telegram handler routes incoming messages through the loop with a sender allowlist and a "working…" ack. Monitoring and hardening are deferred. See `PROJECT_PLAN.md` for the full stage breakdown.

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
