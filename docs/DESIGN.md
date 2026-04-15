# Current Implementation

## Layout
```
bot/agent.py              Agent loop, event writing, projection, post-turn meta-call, causality tree debug
bot/main.py               Test harness that sends scripted messages through the loop
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompt templates from prompts/, injects ambient context
tools/server.py           MCP server: tool schemas, routing, result formatting
db/connection.py          Connection management, schema DDL, init_db()
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
db/facts.py               CRUD for the facts table
prompts/system.md         System prompt template ({current_time} available)
prompts/context_decision.md  Post-turn meta-call template ({events} required)
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

`bot/agent.py` imports `db/connection.py` directly to write events. This is intentional: event writing is agent-layer responsibility, not tool-layer.

### MCP Server (`tools/server.py`)

- Tool schemas (what Claude sees: names, descriptions, input shapes)
- `_dispatch()` — routes tool calls to the DB layer, formats results as strings
- Calls `init_db()` at startup
- `sys.path` setup at top so `db/` is importable when spawned as a subprocess

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

events (id, timestamp, turn_id, conversation_id, type, parent_event_id, payload)
    parent_event_id REFERENCES events(id)
    INDEX (conversation_id, timestamp)
    INDEX (type)
    INDEX (parent_event_id)
    TRIGGER: no UPDATE or DELETE (events are immutable)
```

Timestamps are ISO-8601 UTC strings. Event payloads are JSON blobs, always `{"v": 1, ...}`.

## Event Types and Payload Shapes (v: 1)

| type                | payload fields (beyond `v`)                              | parent              |
|---------------------|----------------------------------------------------------|---------------------|
| `user_message`      | `content`                                                | None (turn root)    |
| `api_call`          | `model`, `input_tokens`, `output_tokens`                 | `user_message` or last `tool_result` or `assistant_message` (meta-call) |
| `tool_call`         | `tool_use_id`, `name`, `input`                           | `api_call`          |
| `tool_result`       | `tool_use_id`, `content`                                 | `tool_call`         |
| `assistant_message` | `content`                                                | `api_call`          |
| `context_decision`  | `from_event_id`                                          | `api_call` (meta)   |
| `error`             | `context`, `message`                                     | varies (see below)  |

`context_decision` is excluded from `project_messages` projection. It is queried separately at turn start to determine the projection bound.

`error` events record failures that are caught rather than surfaced to the user. `context` names the subsystem (e.g. `"context_decision"`); `message` is the exception string. An `error` event written during `_run_context_decision` is parented to the `assistant_message` of that turn.

## Tools (current surface)

`add_fact` only — backed by `db/facts.py`. Read/search tools added only when events projection can no longer serve the relevant context.

## Agent Flow

`main.py` calls `init_db()`, opens an `mcp_client()` context, then sends test messages through `run_loop`.

`run_loop(client, mcp, user_message)`:
1. Generates a fresh `turn_id` (UUID hex). `conversation_id` is module-level, stable for the process.
2. Fetches the most recent `context_decision` event for this `conversation_id` to get `_from_event_id` (defaults to `0` if none exists).
3. Calls `project_messages(conversation_id, from_event_id)` to build the initial messages list from the events table.
4. Appends the user message in memory and writes a `user_message` event to the DB.
5. Calls Claude with the history, rendered system prompt, and tool list. Writes an `api_call` event.
6. On `end_turn`: writes an `assistant_message` event.
7. Post-turn meta-call: fetches all events for this `conversation_id` (id, type, timestamp, payload), validates the returned id against the fetched set, then writes an `api_call` event (parent: `assistant_message`) and a `context_decision` event (parent: meta `api_call`). Updates `_from_event_id`. Any failure (API error, non-integer response, unknown id) is caught and written as an `error` event (parent: `assistant_message`); `_from_event_id` is unchanged. Returns `final_text`.
8. On `tool_use`: writes a `tool_call` event, dispatches via `mcp.call_tool`, writes a `tool_result` event, then loops. Next `api_call`'s parent is the last `tool_result`.

All events carry correct `parent_event_id` (causality chain). In-memory message list is discarded at turn end; next turn re-projects from the DB.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake
- Fetches and converts the tool list to Anthropic API format
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` with `.tools` and `.call_tool(name, arguments)`

## State

Events table is the source of truth. Conversation history is projected from events at each turn start. In-memory message list exists only for the duration of one turn. Facts table is a projection of `add_fact` tool calls, kept in sync at write time.

`_from_event_id` (module-level int, default `0`) controls the projection bound. It is loaded from the most recent `context_decision` event for this `conversation_id` at each turn start and updated after the post-turn meta-call. Resets to `0` on process restart (all events re-projected until the first meta-call completes).

## Debug

`debug_causality_tree(turn_id)` in `bot/agent.py` — queries all events for a turn, walks the parent→child tree, returns an indented text representation.

## Prompt Templates

`PromptBuilder(template)` loads a named file from `prompts/` at init. `build(**kwargs)` injects ambient context (`current_time`) automatically and merges any caller-supplied kwargs. Templates declare their variables; callers pass only what is specific to their use case.

Two instances in `agent.py`:
- `_system_prompt` — renders `prompts/system.md`, used as the system block (marked `cache_control: ephemeral`).
- `_context_decision_prompt` — renders `prompts/context_decision.md`, used for the post-turn meta-call (requires `events=` kwarg).

## Config

`.env` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` and `DB_PATH` are currently consumed.

## Model

`claude-haiku-4-5-20251001` with `max_tokens=8192`.
