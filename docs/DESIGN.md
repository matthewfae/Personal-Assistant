> **Status note.** This document describes the current implementation after Stage 3 (events-driven rebuild, complete as of 2026-04-14). For goals, architectural decisions, and the full stage breakdown, see `PROJECT_PLAN.md`. For `v:1` payload schemas for all event types, see `docs/EVENTS.md`.

# Current State

## Layout
```
bot/agent.py              Agent loop: run_loop, build_messages projection
bot/main.py               Test harness: init_db, conversation_id, drives run_loop
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompts/system.md and injects dynamic context
tools/server.py           MCP server: add_fact tool schema, atomic triple-write handler
db/connection.py          Connection management, schema DDL (facts, mutation_log, events), init_db()
db/events.py              Event append and query helpers (append, get_tool_result_event_id, walk_causality)
db/facts.py               add_fact (upsert, accepts open conn, does NOT commit)
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
prompts/system.md         System prompt template with {current_time} placeholder
```

`db/notes.py` was removed in Stage 3. The notes table and its FTS index are gone from the schema.

## Architecture

### Layers

```
MCP / Claude
    │
tools/server.py       ← MCP boundary: tool schemas, dispatch, string formatting
    │                   Also owns the tool_result event write (see Event Write Ownership)
db/facts.py           ← Data access: returns plain dicts, no MCP types
db/events.py          ← Event append/query: no MCP types, no Claude types
    │
db/connection.py      ← Connection management, schema DDL
db/mutation_log.py    ← Audit writer (called within the same transaction as each write)
    │
SQLite
```

**Layering rule:** each layer only imports downward. `db/` has no knowledge of MCP or Claude. `tools/server.py` knows about MCP and calls down into `db/`. The agent layer knows about Claude and calls down into the MCP client. SQL executes in the MCP server process (via `db/` imports) when a tool is dispatched — this is correct and expected; MCP servers host tool execution rather than acting as a passive tool registry.

**Event Write Ownership split:** the agent process writes `user_message`, `api_call`, `tool_call`, and `assistant_message` events. The MCP server process writes `tool_result` events, bundled with the `mutation_log` row and `facts` row inside a single `BEGIN IMMEDIATE` transaction. This is the only event type not written by the agent.

### MCP Server (`tools/server.py`)

Owns:
- Tool schemas (what Claude sees: names, descriptions, input shapes) — currently one tool: `add_fact`
- `call_tool()` handler — reads `turn_id`, `conversation_id`, `tool_call_event_id`, `tool_use_id` from the incoming request's `meta` field; executes the atomic triple-write

**Atomic triple-write** (inside `BEGIN IMMEDIATE`):
1. `facts` row — via `facts_db.add_fact(conn, ...)`
2. `mutation_log` row — via `mutation_log.log(conn, ...)`
3. `tool_result` event — via `events_db.append(conn, 'tool_result', ...)`

The server opens its own connection with `isolation_level=None` for manual transaction control. WAL, foreign keys, and busy_timeout pragmas are applied explicitly (matching `get_db()`'s pragma order).

Error path: rollback the failed transaction; write a `tool_result` event with `is_error: True` on a fresh `get_db()` connection. The original error is still returned to the MCP client.

Calls `init_db()` at startup.

### DB Layer (`db/`)

**`connection.py`**
- `get_db()` — context manager yielding a `sqlite3.Connection` with WAL mode, foreign keys, and `busy_timeout = 5000` enabled. Commits on clean exit, rolls back on exception. The `busy_timeout` pragma is required because both the agent process and the MCP server process write to the same DB file; without it a concurrent write would get an immediate `SQLITE_BUSY` error instead of waiting.
- `init_db()` — runs the schema DDL (tables + indexes + triggers). Safe to call on every startup (`CREATE ... IF NOT EXISTS` throughout). Uses `executescript`, which auto-commits — do not call inside a handler.
- Full schema DDL lives here as the single source of truth.

**`events.py`**
- `append(conn, type, payload, turn_id, conversation_id, parent_event_id) -> int` — inserts one event row. Sets `correlation_id = turn_id` (aliased per spec — the two concepts are identical in the current single-bot single-MCP-server architecture). Does NOT commit; callers manage their own transaction.
- `get_tool_result_event_id(conn, tool_use_id) -> int | None` — looks up the most recent `tool_result` event for a given `tool_use_id` via `json_extract`. Used by the agent to set `last_input_event_id` for causality tracking.
- `walk_causality(conn, turn_id) -> list[dict]` — returns all events in a turn ordered by id with `id`, `type`, `parent_event_id`, `timestamp`. Debug/sanity-check helper.

**`mutation_log.py`**
- `log(conn, table_name, operation, record_id, data)` — inserts one row into `mutation_log`. Takes an open connection so the log entry shares the same transaction as the mutation it records.

**`facts.py`**
- `add_fact(conn, key, value, category) -> dict` — upsert on `(category, key)`. Accepts a caller-supplied open connection, does NOT commit. Returns a dict with `id`, `category`, `key`, `value`, `created_at`, `updated_at`, `operation` (`'inserted'` or `'updated'`). Does not call `mutation_log` directly — the server handler does that in the same transaction.

The old `get_fact`, `search_facts`, `list_facts` functions have been removed. If read/search tools are added in Stage 4, they will be built as new functions here.

## Schema

```sql
facts (id, category, key, value, created_at, updated_at)
    UNIQUE(category, key)

mutation_log (id, table_name, operation, record_id, data_json, timestamp)

events (id, timestamp, turn_id, conversation_id, correlation_id, type, parent_event_id, payload)
    INDEX ON (conversation_id, timestamp)   -- idx_events_conv_ts
    INDEX ON (type)                         -- idx_events_type
    INDEX ON (parent_event_id)              -- idx_events_parent
    TRIGGER events_no_update  BEFORE UPDATE  RAISE(ABORT, ...)
    TRIGGER events_no_delete  BEFORE DELETE  RAISE(ABORT, ...)
```

Timestamps are ISO-8601 UTC strings. `payload` is a JSON blob; see `docs/EVENTS.md` for `v:1` schemas per event type.

## Agent Flow

`bot/main.py` calls `init_db()`, instantiates `AsyncAnthropic`, generates a single `conversation_id` for the session, opens an `mcp_client()` context, then sends test messages through `run_loop` sequentially.

`run_loop(client, mcp, conversation_id, user_message) -> str`:

1. Generates `turn_id = uuid4()`.
2. Writes `user_message` event to DB (turn root, `parent_event_id=None`).
3. Calls `build_messages(conn, conversation_id)` to project prior conversation history from the events table, then appends the current user message to the local list.
4. Enters the agent loop:
   - Calls `await client.messages.create(...)` with the projected messages, system prompt, and tool list.
   - Writes `api_call` event with full payload (usage, caching tokens, model, request_id, started_at/finished_at, status).
   - On `end_turn`: writes `assistant_message` event; returns the final text.
   - On `tool_use`: for each tool block — writes `tool_call` event, dispatches via `mcp.call_tool(..., meta={...})`, collects result. After all tools: appends tool results to local list; loops.

`build_messages(conn, conversation_id) -> list[dict]`:
- Queries all events for `conversation_id` ordered by id.
- Groups by `turn_id` preserving encounter order.
- Skips turns missing `user_message` or `assistant_message` (crash tolerance: agent crash or in-progress turn).
- Skips orphaned `tool_call` events with no matching `tool_result` (crash tolerance: MCP server crash).
- Batches complete tool pairs into an assistant message (tool_use blocks) + user message (tool_result blocks).

## Causality Tracking

Four locals maintained in `run_loop`:
- `user_event_id` — written once; used as the first `api_call`'s parent.
- `last_input_event_id` — starts as `user_event_id`; updated to the `tool_result` event id after each single-tool step.
- `current_api_call_event_id` — set after every `api_call` write; used as parent for `tool_call` and `assistant_message` events.
- `spawning_api_call_event_id` — set to `current_api_call_event_id` when parallel tools are dispatched; consumed by the next `api_call`'s parent selection (the api_call following parallel results points at the api_call that spawned them, not any individual tool_result).

See `docs/EVENTS.md` for full causality rules per event type.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters` (persistent for the session)
- Performs the MCP initialization handshake (`session.initialize()`)
- Fetches the tool list from the server (`session.list_tools()`)
- Converts MCP `Tool` objects to the dict format the Anthropic API expects
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` instance with `.tools` and `.call_tool(name, arguments, meta=None)`

`call_tool` passes `meta` directly to `session.call_tool(...)` (MCP SDK 1.27.0+).

## State

Conversation history is derived from the `events` table via `build_messages` at the start of each turn. No in-memory history is maintained between turns.

## System Prompt

`PromptBuilder` loads `prompts/system.md` at init time and injects `{current_time}` via `.format()` on each `build()` call. The system prompt block is marked `cache_control: ephemeral`. The prompt currently describes only the `add_fact` capability.

## Config

`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` and `DB_PATH` are currently consumed. Telegram config is wired up in Stage 5.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`. Prompt caching applied to system prompt and tool list (both marked `cache_control: ephemeral`).

## Tests / Scripts

`tests/` contains only `__init__.py` — no automated tests yet. `scripts/` is empty. The test harness is `bot/main.py` (scripted messages, not `pytest`).

The Stage 3 implementation plan (`docs/STAGE3_IMPL_PLAN.md`) recommends adding a manual smoke-test prompt that forces parallel tool calls (e.g. `"Remember I like coffee and my name is Matt"`) to exercise the `spawning_api_call_event_id` path and verify the causality tree with `walk_causality`. This has not been done yet.
