# Current Implementation

## Layout
```
bot/agent.py              Agent loop, event writing, projection, reflection step, causality tree debug
bot/telegram_handler.py   Telegram long-polling bot: allowlist, ack, wires messages to run_loop
bot/main.py               Entry point: starts the Telegram bot
bot/harness.py            Test harness: sends scripted messages through the loop directly (no Telegram)
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompt templates from prompts/, injects ambient context
tools/server.py           MCP server: tool schemas, routing, result formatting
db/connection.py          Connection management, schema DDL, init_db()
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
db/facts.py               CRUD for the facts table
db/context.py             Read/write the context projection bound (context_bound table)
prompts/system.md         Main turn system prompt ({current_time} available)
prompts/reflection.md     Reflection step system prompt ({current_time} available)
```

## Architecture

### Layers

```
MCP / Claude
    │
tools/server.py       ← MCP boundary: tool schemas, dispatch, string formatting
    │
db/facts.py           ← Data access: returns plain dicts, no MCP types
db/context.py         ← Read/write the context projection bound
    │
db/connection.py      ← Connection management, schema DDL
db/mutation_log.py    ← Audit writer (called within the same transaction as each write)
    │
SQLite
```

Each layer only imports downward. `db/` has no knowledge of MCP or Claude. `tools/server.py` has no SQL.

`bot/agent.py` imports `db/connection.py` directly to write events. This is intentional: event writing is agent-layer responsibility, not tool-layer.

### MCP Server (`tools/server.py`)

- Tool schemas (what Claude sees: names, descriptions, input shapes)
- `_dispatch()` — routes tool calls to the DB layer, formats results as strings
- Calls `init_db()` at startup
- `sys.path` setup at top so `db/` is importable when spawned as a subprocess

### DB Layer (`db/`)

**`connection.py`**
- `get_db()` — context manager yielding a `sqlite3.Connection` (WAL mode, foreign keys on). Commits on clean exit, rolls back on exception.
- `init_db()` — runs schema DDL and any pending migrations. Safe to call on every startup.

**`mutation_log.py`**
- `log(conn, table_name, operation, record_id, data)` — inserts one row into `mutation_log` on the caller's open connection.

**`facts.py`**
- `add_fact(key, value, category)` → dict with `operation: 'inserted' | 'updated'`
- `get_fact(key, category)` → dict or None
- `search_facts(query, category?)` → list of dicts (LIKE match on key/value)
- `list_facts(category?, limit)` → list of dicts, newest first

**`context.py`**
- `get_context_bound()` → int — reads `from_event_id` from the single-row `context_bound` table (default `0`).
- `set_context_bound(from_event_id)` — validates the id exists in `events`, then upserts into `context_bound`. Raises `ValueError` for unknown ids.

## Schema

```sql
facts (id, category, key, value, created_at, updated_at)
    UNIQUE(category, key)

mutation_log (id, table_name, operation, record_id, data_json, timestamp)

events (id, timestamp, turn_id, type, parent_event_id, payload)
    parent_event_id REFERENCES events(id)
    INDEX (type)
    INDEX (parent_event_id)
    TRIGGER: no UPDATE or DELETE (events are immutable)

context_bound (id CHECK (id = 1), from_event_id)
    Single-row table. Stores the current projection bound.
    Seeded with from_event_id = 0 on first init.
```

Timestamps are ISO-8601 UTC strings. Event payloads are JSON blobs, always `{"v": 1, ...}`.

## Event Types and Payload Shapes (v: 1)

| type                | payload fields (beyond `v`)                              | parent              |
|---------------------|----------------------------------------------------------|---------------------|
| `user_message`      | `content`                                                | None (turn root)    |
| `api_call`          | `model`, `input_tokens`, `output_tokens`                 | `user_message` or last `tool_result` (main turn); `assistant_message` or last `tool_result` (reflection) |
| `tool_call`         | `tool_use_id`, `name`, `input`                           | `api_call`          |
| `tool_result`       | `tool_use_id`, `content`                                 | `tool_call`         |
| `assistant_message` | `content`                                                | `api_call`          |
| `telegram_ack`      | `telegram_message_id`                                    | None (turn root)    |
| `error`             | `context`, `message`                                     | `assistant_message` |

`error` events record failures that are caught rather than surfaced to the user. `context` names the subsystem (e.g. `"reflection"`); `message` is the exception string.

## Tools

| tool | available in | description |
|---|---|---|
| `add_fact` | main turn + reflection | Store a key/value fact. Upserts on (category, key). |
| `set_context_bound` | reflection only | Advance the projection bound. Hidden from main turn. |

`set_context_bound` is intentionally excluded from the main turn tool list — it is a reflection-step concern. `agent.py` filters `mcp.tools` before passing to the main turn API call and re-attaches `cache_control` to the new last tool.

Read/search tools remain deferred. They are added only when the bounded projection can no longer serve the relevant context.

## Agent Flow

`run_loop(client, mcp, user_message)` → `TurnResult(reply, proactive)`:

1. Generates a fresh `turn_id` (UUID hex).
2. Calls `_load_from_event_id()` (reads `context_bound` table) to get the projection bound.
3. Calls `project_messages(from_event_id)` to build the initial messages list.
4. Appends the user message in memory and writes a `user_message` event.
5. Filters `mcp.tools` to exclude `set_context_bound` for the main turn.
6. Loops (main turn):
   - Calls Claude with the history, system prompt (`cache_control: ephemeral`), and filtered tool list. Writes an `api_call` event.
   - On `end_turn`: writes an `assistant_message` event, then calls `_run_reflection()`.
   - On `tool_use`: writes `tool_call` and `tool_result` events, appends results to messages, loops.
7. After reflection, reloads `_from_event_id` from the DB.
8. Returns `TurnResult(reply=final_text, proactive=proactive_text)`.

`_run_reflection(client, mcp, turn_id, assistant_message_id)` → `str | None`:

Runs as a mini agent loop after the main turn:
1. Re-projects messages from the DB (now includes the just-written `assistant_message`).
2. Loops with the full tool list (including `set_context_bound`) and the reflection system prompt (`cache_control: ephemeral`).
3. On `end_turn`: if final text is `PASS` (case-insensitive), returns `None` without writing an event. Otherwise writes an `assistant_message` event and returns the text.
4. On `tool_use`: writes `tool_call` and `tool_result` events, loops.
5. Any exception is caught, written as an `error` event (parented to `assistant_message_id`), and returns `None`.

All events carry correct `parent_event_id` (causality chain). In-memory message list is discarded at turn end; next turn re-projects from the DB.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake
- Fetches and converts the tool list to Anthropic API format
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` with `.tools` and `.call_tool(name, arguments)`

## State

The events table is the source of truth — a flat, append-only log. There is no "conversation" concept: the only grouping unit is `turn_id`. The projection bound is Claude's working memory, managed by the `set_context_bound` tool during the reflection step.

The messages array is projected from events at each turn start and discarded at turn end. In-memory state exists only for the duration of one turn. Facts table is a projection of `add_fact` tool calls, kept in sync at write time.

`_from_event_id` (module-level int, default `0`) controls the projection bound. It is loaded from the `context_bound` table at each turn start and reloaded after the reflection step completes. Resets to `0` on process restart (all events re-projected until the first `set_context_bound` call).

## Debug

`debug_causality_tree(turn_id)` in `bot/agent.py` — queries all events for a turn, walks the parent→child tree, returns an indented text representation.

## Prompt Templates

`PromptBuilder(template)` loads a named file from `prompts/` at init. `build(**kwargs)` injects ambient context (`current_time`) automatically and merges any caller-supplied kwargs.

Two instances in `agent.py`:
- `_system_prompt` — renders `prompts/system.md`, used as the system block in the main turn (marked `cache_control: ephemeral`).
- `_reflection_prompt` — renders `prompts/reflection.md`, used as the system block in the reflection step (marked `cache_control: ephemeral`).

## Config

`.env` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. All four are consumed.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`.
