# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database (personal info, projects, lists, calendar, etc.). Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response to the user via Telegram.

## Core Architecture Decisions

- **Model:** `claude-haiku-4-5-20251001` as primary.
- **Prompt caching:** Applied to system prompt and tool list.
- **Tool architecture:** MCP server/client split. Tools live in `tools/server.py`; the bot is an MCP client.
- **DB access:** Curated tool functions only (`add_fact`, `search_facts`, etc.), not raw SQL.
- **Mutation audit log:** All DB writes logged to a `mutation_log` table.
- **Schema:** Minimal to start; tables added as needed.
- **Error handling:** Low-level code raises naturally; bot/agent layer catches and forwards errors to the user via Telegram.

## Development Stages

### Stage 1: Python Stack & Environment ✓
- Python 3.11+, `uv` for package management
- Core packages: `anthropic`, `python-telegram-bot`, `python-dotenv`, `mcp`
- `sqlite3` from stdlib
- `.env` for secrets
- Project structure: `bot/`, `tools/`, `db/`, `tests/`

### Stage 2: Agent Loop with MCP Tool Use ✓
- MCP server (`tools/server.py`) with `add_fact` placeholder tool
- MCP client (`bot/mcp_client.py`) that spawns the server, performs the initialization handshake, and fetches the tool list
- Agent loop (`bot/agent.py`) that drives the Claude conversation and dispatches tool calls through the MCP client
- Prompt caching on system prompt and tool list
- System prompt in `prompts/system.md` with dynamic context injected via `PromptBuilder`
- End-to-end flow validated: user message → Claude → tool_use → MCP client → MCP server → tool_result → Claude → final response

### Stage 2b: Context Management & Short-Term Memory
- Append-only `events` table logs every agent loop event (user messages, API calls, tool use, tool results, assistant responses)
- `message_context` table holds an ordered set of recent entries injected into each turn's prompt
- After each assistant response, a lightweight Claude API call (no tools, no system prompt) determines which context entries to retain; returns a JSON array of indices
- `search_facts` and similar tools can query the events table to retrieve entries not in active context

### Stage 3: SQLite Database
- Schema: `facts` (category/key/value), `notes` (FTS), `mutation_log`
- Additional structured tables (projects, tasks, calendar events) added as needed
- Tool functions in `tools/db.py`: `add_fact`, `search_facts`, `update_fact`, `list_notes`, etc.

### Stage 4: Agent + Database Integration
- Wire DB tool functions into the MCP server
- Start with ~5 tools
- Verify mutation log captures all writes

### Stage 5: Telegram Bot
- `python-telegram-bot` with long polling
- Allowlist check on sender ID on every incoming message
- Route incoming messages through the agent loop
- Forward responses and errors back via Telegram

### Stage 6: Monitoring Service
- `systemd` service on the Linux host
- Structured logging to a file
- Basic health checks

### Stage 7: Security Review & Hardening
- Audit secret management (API keys, Telegram bot token)
- Review DB access patterns and mutation log completeness
- Test edge cases: malformed input, API outages, DB corruption
- Lock down file permissions on DB and `.env`
- Review auth allowlist
- Rate limiting and cost monitoring
