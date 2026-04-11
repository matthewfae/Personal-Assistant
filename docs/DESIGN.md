> **Status note.** This document describes the *current implementation* of the bot as it stands after Stage 2 and the exploratory DB + tool-surface work (see `PROJECT_PLAN.md` → Stage 2.5). An events-driven rebuild is in progress under `PROJECT_PLAN.md` → Stage 3 which will supersede much of what's below — particularly the tool surface, conversation history, and agent flow sections. Sections of this doc will be updated as the rebuild lands; until then, treat this as an accurate description of what exists *today*, not of where we're headed. For target architecture and decisions, see `PROJECT_PLAN.md`.

# Current State

## Layout
```
bot/agent.py              Agent loop (run_loop function)
bot/main.py               Test harness that sends scripted messages through the loop
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompts/system.md and injects dynamic context
tools/server.py           MCP server: tool schemas, routing, result formatting
db/connection.py          Connection management, schema DDL, init_db()
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
db/facts.py               CRUD for the facts table
db/notes.py               CRUD and FTS search for the notes table
prompts/system.md         System prompt template with {current_time} placeholder
```

## Architecture

### Layers

```
MCP / Claude
    │
tools/server.py       ← MCP boundary: tool schemas, dispatch, string formatting
    │
db/facts.py           ← Data access: returns plain dicts, no MCP types
db/notes.py
    │
db/connection.py      ← Connection management, schema DDL
db/mutation_log.py    ← Audit writer (called within the same transaction as each write)
    │
SQLite
```

**Rule:** each layer only imports downward. `db/` has no knowledge of MCP or Claude. `tools/server.py` has no SQL.

### MCP Server (`tools/server.py`)

Owns:
- Tool schemas (what Claude sees: names, descriptions, input shapes)
- `_dispatch()` — routes tool calls to the DB layer and formats results as strings

Calls `init_db()` at startup so the schema is always ready before any tool is dispatched.

### DB Layer (`db/`)

**`connection.py`**
- `get_db()` — context manager yielding a `sqlite3.Connection` with WAL mode and foreign keys enabled. Commits on clean exit, rolls back on exception.
- `init_db()` — runs the schema DDL (tables + FTS triggers). Safe to call on every startup (all statements use `CREATE ... IF NOT EXISTS`).
- Full schema DDL lives here as the single source of truth.

**`mutation_log.py`**
- `log(conn, table_name, operation, record_id, data)` — inserts one row into `mutation_log`. Takes an open connection so the log entry shares the same transaction as the mutation it records.

**`facts.py`** — CRUD for the `facts` table
- `add_fact(key, value, category)` → dict with `operation: 'inserted' | 'updated'`
- `get_fact(key, category)` → dict or None
- `search_facts(query, category?)` → list of dicts (key/value LIKE match)
- `list_facts(category?, limit)` → list of dicts, newest first

**`notes.py`** — CRUD and FTS for the `notes` table
- `add_note(title, body, tags)` → dict
- `get_note(note_id)` → dict or None *(exists at the DB layer but is not currently exposed as an MCP tool)*
- `search_notes(query, limit)` → list of dicts (FTS5, ranked by relevance)
- `list_notes(limit)` → list of dicts (title + tags only, newest first)

## Schema

```sql
facts (id, category, key, value, created_at, updated_at)
    UNIQUE(category, key)

notes (id, title, body, tags, created_at, updated_at)
notes_fts  -- FTS5 virtual table; synced via INSERT/UPDATE/DELETE triggers

mutation_log (id, table_name, operation, record_id, data_json, timestamp)
```

Timestamps are ISO-8601 UTC strings. Tags on notes are a plain comma-separated string.

## Agent Flow

`main.py` opens an `mcp_client()` context, then sends test messages through `run_loop`.

`run_loop(client, mcp, messages, user_message)`:
1. Appends the user message to the conversation history.
2. Calls Claude with the history, rendered system prompt, and tool list from the MCP server.
3. On `end_turn`, extracts the final text response and returns it with updated history.
4. On `tool_use`, dispatches each tool call through `mcp.call_tool`, collects results
   into a single `user` message, appends it to history, and loops.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake (`session.initialize()`)
- Fetches the tool list from the server (`session.list_tools()`)
- Converts MCP `Tool` objects to the dict format the Anthropic API expects
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` instance with `.tools` and `.call_tool(name, arguments)`

## State

Conversation history is an in-memory list passed through `run_loop`. No persistence yet.

## System Prompt

`PromptBuilder` loads `prompts/system.md` at init time and injects `{current_time}`
via `.format()` on each `build()` call. The system prompt block is marked
`cache_control: ephemeral`.

## Config

`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` and `DB_PATH`
are currently consumed.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`.
