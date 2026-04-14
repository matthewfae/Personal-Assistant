# Current Implementation

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
    │
db/connection.py      ← Connection management, schema DDL
db/mutation_log.py    ← Audit writer (called within the same transaction as each write)
    │
SQLite
```

Each layer only imports downward. `db/` has no knowledge of MCP or Claude. `tools/server.py` has no SQL.

### MCP Server (`tools/server.py`)

- Tool schemas (what Claude sees: names, descriptions, input shapes)
- `_dispatch()` — routes tool calls to the DB layer, formats results as strings
- Calls `init_db()` at startup

### DB Layer (`db/`)

**`connection.py`**
- `get_db()` — context manager yielding a `sqlite3.Connection` (WAL mode, foreign keys on). Commits on clean exit, rolls back on exception.
- `init_db()` — runs schema DDL. Safe to call on every startup.

**`mutation_log.py`**
- `log(conn, table_name, operation, record_id, data)` — inserts one row into `mutation_log` on the caller's open connection.

**`facts.py`**
- `add_fact(key, value, category)` → dict with `operation: 'inserted' | 'updated'`
- `get_fact(key, category)` → dict or None
- `search_facts(query, category?)` → list of dicts (LIKE match on key/value)
- `list_facts(category?, limit)` → list of dicts, newest first

## Schema

```sql
facts (id, category, key, value, created_at, updated_at)
    UNIQUE(category, key)

mutation_log (id, table_name, operation, record_id, data_json, timestamp)
```

Timestamps are ISO-8601 UTC strings.

## Tools (current surface)

`add_fact`, `get_fact`, `search_facts`, `list_facts` — all backed by `db/facts.py`.

## Agent Flow

`main.py` opens an `mcp_client()` context, then sends test messages through `run_loop`.

`run_loop(client, mcp, messages, user_message)`:
1. Appends the user message to the in-memory conversation history.
2. Calls Claude with the history, rendered system prompt, and tool list.
3. On `end_turn`, extracts the final text and returns it with updated history.
4. On `tool_use`, dispatches each tool call through `mcp.call_tool`, collects results into a single `user` message, appends to history, and loops.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake
- Fetches and converts the tool list to Anthropic API format
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` with `.tools` and `.call_tool(name, arguments)`

## State

Conversation history is an in-memory list for the duration of one turn, then discarded. No cross-turn persistence.

## System Prompt

`PromptBuilder` loads `prompts/system.md` at init and injects `{current_time}` on each `build()` call. System prompt block is marked `cache_control: ephemeral`.

## Config

`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` and `DB_PATH` are currently consumed.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`.
