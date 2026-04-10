# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database (personal info, projects, lists, calendar, etc.). Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response to the user via Telegram.

## Core Architecture Decisions

- **Model:** Haiku 4.5 as primary. Escalate to Sonnet later only if needed.
- **Cost strategy:** Aggressive prompt caching on system prompt, tool definitions, and stable context. Expect ~80–90% cost reduction on repeated context.
- **Tool architecture:** Phased approach — start with native Claude API tool use, then refactor into an MCP server/client split as a learning exercise (see Stages 2 and 5.5).
- **DB access:** Curated tool functions only (`add_fact`, `search_facts`, etc.), not raw SQL. Safer, clearer, easier for Claude to use well.
- **Mutation audit log:** All DB writes logged from day one. Gives us an audit trail and undo capability.
- **Schema philosophy:** Start simple, grow organically. SQLite handles evolution fine if we're disciplined.
- **Error handling:** All errors should surface as Telegram messages to the user, not silent failures or log-only output. Low-level code raises naturally; bot/agent layer catches and communicates back via Telegram.

## Development Stages

### Stage 1: Python Stack & Environment
- Python 3.11+
- `uv` for package management (faster and simpler than pip/poetry)
- Core packages: `anthropic`, `python-telegram-bot`, `python-dotenv`, `mcp` (later)
- `sqlite3` from stdlib (no ORM)
- `.env` file for API keys and secrets (never hardcode)
- Project structure: `bot/`, `tools/`, `db/`, `tests/`

### Stage 2: Agent Loop with Native Tool Use
- Build the tool-use loop from the start — this is the core architecture.
- Define 1–2 toy tools (e.g., `get_current_time`) to validate the loop end-to-end.
- Confirm multi-turn flow: user message → Claude → tool_use → execute → tool_result → Claude → final response.
- Add prompt caching on system prompt and tool definitions.
- **Prompt management:** System prompt lives in a template file (`prompts/system.md`), with dynamic context (current time, etc.) injected at runtime via a `PromptBuilder`.
- **Design discipline:** write tool functions as clean, stateless, JSON-serializable Python so they can become MCP tools later without redesign.
- **Error strategy:** Low-level modules don't catch errors; bot/agent layer catches exceptions and sends user-friendly messages via Telegram.

### Stage 3: SQLite Database
- Start with a minimal schema: `facts` table (category/key/value), `notes` table with FTS for search, `mutation_log` table for audit.
- Add more structured tables (projects, tasks, calendar events) as real needs emerge.
- Write curated helper functions in `tools/db.py`: `add_fact`, `search_facts`, `update_fact`, `list_notes`, etc.
- Seed with simple test data.

### Stage 4: Agent + Database Integration
- Wire the DB tool functions into the native tool-use loop from Stage 2.
- Start with ~5 tools. Add more as patterns emerge, not speculatively.
- Test end-to-end: query DB → send to Claude → Claude calls tools → response.
- Verify mutation log captures all writes.

### Stage 5: Telegram Bot
- Build bot using `python-telegram-bot` with long polling (no public URL needed).
- **Auth: allowlist check on sender ID — the bot only responds to you.** This is non-negotiable and must ship with the first version.
- Route incoming messages through the agent loop.
- Send final responses back via Telegram.
- Basic error handling: what happens if Claude API fails, if Telegram fails.

### Stage 5.5: MCP Refactor
- Create `mcp_server.py` using the Python MCP SDK.
- Wrap existing DB tool functions as MCP tools (mostly decorators — logic unchanged).
- Convert bot into an MCP client: spawn the MCP server as a stdio subprocess, fetch tool list, route Claude's tool calls through MCP protocol.
- Verify behavior is identical to the pre-refactor version.
- **Learning outcome:** understand JSON-RPC flow, tool discovery, client/server split. End state: an MCP server that could also be plugged into Claude Desktop or Claude Code.

### Stage 6: Monitoring Service
- Run as a `systemd` service on the Linux host.
- Handle automatic restarts on crash.
- Structured logging to a file.
- Basic health checks.

### Stage 7: Security Review & Hardening
- Audit secret management (API keys, tokens, Telegram bot token).
- Review DB access patterns and mutation log completeness.
- Test edge cases: malformed input, API outages, DB corruption.
- Lock down file permissions on DB and .env.
- Review auth allowlist.
- Consider rate limiting and cost monitoring.
