# Current Implementation

## Layout
```
bot/agent.py              Agent loop, event writing, projection, reflection step, causality tree debug
bot/telegram_handler.py   Telegram long-polling bot: allowlist, ack, wires messages to run_loop, sends replies
bot/main.py               Entry point: starts the Telegram bot
bot/harness.py            Test harness: sends scripted messages through the loop directly (no Telegram)
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompt templates from prompts/; format_time() for message timestamps
tools/server.py           MCP server: tool schemas, routing, result formatting
db/connection.py          Connection management, schema DDL, init_db()
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
db/context.py             Read/write the context projection bound (context_bound table)
db/tasks.py               Tasks CRUD
db/shopping.py            Shopping list CRUD
db/symptoms.py            Symptom log CRUD
db/query.py               Read-only ad-hoc SQL for the query tool
prompts/system.md         Main turn system prompt (static)
prompts/reflection.md     Reflection step system prompt (static)
```

## Architecture

### Layers

```
MCP / Claude
    │
tools/server.py       ← MCP boundary: tool schemas, dispatch, string formatting
    │
db/context.py, db/tasks.py, db/shopping.py, db/symptoms.py, db/query.py
                      ← Table-level data access
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

**`context.py`**
- `get_context_bound()` → int — reads `from_event_id` from the single-row `context_bound` table (default `0`).
- `set_context_bound(from_event_id)` — validates the id exists in `events`, then upserts into `context_bound`. Raises `ValueError` for unknown ids.

## Schema

```sql
mutation_log (id, table_name, operation, record_id, data_json, timestamp)

events (id, timestamp, turn_id, type, parent_event_id, payload)
    parent_event_id REFERENCES events(id)
    INDEX (type)
    INDEX (parent_event_id)
    TRIGGER: no UPDATE or DELETE (events are immutable)

context_bound (id CHECK (id = 1), from_event_id)
    Single-row table. Stores the current projection bound.
    Seeded with from_event_id = 0 on first init.

tasks (id, area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at)
    status: TEXT NOT NULL DEFAULT 'open' — one of 'open', 'done', 'waiting', 'someday'
    area: TEXT NOT NULL — loose grouping chosen by Claude (e.g. 'home', 'health', 'finances', 'system')
    priority: INTEGER — nullable, lower = higher priority
    estimated_hours: REAL — nullable, rough effort estimate
    detail_json: TEXT — nullable JSON blob for freeform notes, sub-steps, context

shopping (id, item, task_id, created_at)
    item: TEXT NOT NULL — what to buy
    task_id: INTEGER — nullable FK to tasks(id), links shopping items to their originating task

symptoms (id, symptom, severity, occurred_at, notes, logged_at)
    symptom: TEXT NOT NULL — name/description of the symptom
    severity: INTEGER — nullable, 1–10 scale
    occurred_at: TEXT NOT NULL — ISO-8601 UTC, when the symptom happened (user-reported time)
    notes: TEXT — nullable freeform context
    logged_at: TEXT NOT NULL — ISO-8601 UTC, when the entry was recorded
```

Two timestamps distinguish *when it happened* from *when it was logged* — the user may report a symptom that occurred hours or days earlier.

Timestamps are ISO-8601 UTC strings. Event payloads are JSON blobs, always `{"v": 1, ...}`.

## Event Types and Payload Shapes (v: 1)

| type                | payload fields (beyond `v`)                              | parent              |
|---------------------|----------------------------------------------------------|---------------------|
| `user_message`      | `content`                                                | None (turn root)    |
| `api_call`          | `model`, `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | `user_message` or last `tool_result` (main turn); `assistant_message` or last `tool_result` (reflection) |
| `tool_call`         | `tool_use_id`, `name`, `input`                           | `api_call`          |
| `tool_result`       | `tool_use_id`, `content`, `is_error`                     | `tool_call`         |
| `assistant_message` | `content`                                                | `api_call`          |
| `telegram_ack`      | `telegram_message_id`                                    | None (turn root)    |
| `error`             | `context`, `message`                                     | `assistant_message` (reflection); last input event (main turn) |

`error` events record failures that are caught rather than surfaced to the user. `context` names the subsystem (`"reflection"` or `"main_turn"`); `message` is the exception string or a description of the failure.

`api_call` cache counts come from `response.usage` (recorded as `0` when absent). Events written before they were added have no cache fields.

`tool_result.is_error` is `true` when the tool returned an error or the MCP call itself raised. Events written before this field existed have no `is_error`; the projection treats them as successful.

## Tools

| tool | available in | description |
|---|---|---|
| `set_context_bound` | reflection only | Advance the projection bound. Requires a `rationale` field. Hidden from main turn. |
| `add_task` | main turn + reflection | Create a new task with area, summary, and optional priority/effort/detail. |
| `update_task` | main turn + reflection | Update fields on an existing task (status, priority, detail, etc.). |
| `list_tasks` | main turn + reflection | List tasks (all columns). Filterable by status, area, or summary text search. Pass status='all' for every status. |
| `get_task` | main turn + reflection | Get full detail for a single task by id. |
| `add_shopping` | main turn + reflection | Add an item to the shopping list, with optional task_id link. |
| `list_shopping` | main turn + reflection | Return the full shopping list (id and item name). |
| `remove_shopping` | main turn + reflection | Remove a shopping item by id. |
| `log_symptom` | main turn + reflection | Record a symptom. `occurred_at` defaults to now; `severity` is 1–10. |
| `update_symptom` | main turn + reflection | Correct or annotate a logged symptom. |
| `list_symptoms` | main turn + reflection | List symptoms, most recent first. Filter by name (substring) or `since` (ISO-8601 bound). Default limit 100. |
| `get_symptom` | main turn + reflection | Full detail for one symptom log by id. |
| `delete_symptom` | main turn + reflection | Delete a symptom log (use when logged in error). |
| `query` | main turn + reflection | Run an arbitrary read-only SELECT against any table (tasks, shopping, symptoms, events). Returns up to 200 rows. |

`set_context_bound` is intentionally excluded from the main turn tool list — it is a reflection-step concern. `agent.py` filters `mcp.tools` before passing to the main turn API call and re-attaches `cache_control` to the new last tool.

The `query` tool covers all tables: `tasks`, `shopping`, `symptoms (id, symptom, severity, occurred_at, notes, logged_at)`, `events`, and `mutation_log`.

### Data Access Layer

**`db/query.py`**
- `run_query(sql)` → list of dicts — executes a read-only SELECT (max 200 rows). Raises `ValueError` for non-SELECT statements or queries containing semicolons.

**`db/tasks.py`**
- `add_task(area, summary, priority?, estimated_hours?, detail_json?, status?)` → dict
- `update_task(task_id, **fields)` → dict
- `get_task(task_id)` → dict or None
- `list_tasks(status?, area?, search?)` → list of full dicts (all columns). `status='all'` skips status filter; `search` is a LIKE match on summary.

**`db/shopping.py`**
- `add_shopping(item, task_id?)` → dict
- `list_shopping()` → list of `{id, item}` dicts
- `remove_shopping(shopping_id)` → bool

**`db/symptoms.py`**
- `log_symptom(symptom, severity?, occurred_at?, notes?)` → dict
- `update_symptom(symptom_id, **fields)` → dict
- `get_symptom(symptom_id)` → dict or None
- `list_symptoms(symptom?, since?, limit?)` → list of full dicts, ordered occurred_at DESC
- `delete_symptom(symptom_id)` → bool

## Agent Flow

`run_loop(client, mcp, user_message)` → `TurnResult(reply, proactive)`. `client` is an `AsyncAnthropic`; every API call is awaited, so the event loop is never blocked while waiting on Claude.

1. Generates a fresh `turn_id` (UUID hex).
2. Writes the `user_message` event.
3. Calls `_load_from_event_id()` (reads `context_bound` table) to get the projection bound.
4. Calls `project_messages(from_event_id)` to build the messages list, which now ends with the new user message. Projecting after the write means the new message is rendered from its stored timestamp, byte-identical to how later turns will render it.
5. Filters `mcp.tools` to exclude `set_context_bound` for the main turn.
6. Loops (main turn):
   - Calls Claude with the history, system prompt (`cache_control: ephemeral`), filtered tool list, and top-level `cache_control: ephemeral` (automatic caching of the message history). Writes an `api_call` event.
   - On `end_turn`: writes an `assistant_message` event, then calls `_run_reflection()`.
   - On `tool_use`: calls `_execute_tool_calls()` (below), appends results to messages, loops.
   - On `refusal`: returns a reply saying Claude declined, with the `stop_details` category.
   - On any other stop reason: returns an "Unexpected stop reason" reply.
7. Returns `TurnResult(reply=final_text, proactive=proactive_text)`.
8. If the loop hits `_MAX_API_CALLS` (15) without finishing, writes an `error` event (`context: "main_turn"`) and returns a short failure reply.

`_run_reflection(client, mcp, turn_id, assistant_message_id)` → `str | None`:

Runs as a mini agent loop after the main turn:
1. Re-projects messages from the DB (now includes the just-written `assistant_message`) and appends a synthetic `(reflection — <current time>)` user message, which is not stored as an event.
2. Loops with the full tool list (including `set_context_bound`) and the reflection system prompt (`cache_control: ephemeral`).
3. On `end_turn`: if final text is `PASS` (case-insensitive), returns `None` without writing an event. Otherwise writes an `assistant_message` event and returns the text.
4. On `tool_use`: calls `_execute_tool_calls()`, loops.
5. Capped at `_MAX_API_CALLS` (15); hitting the cap raises, which is handled as below.
6. Transient API errors (rate limit, connection, timeout) are re-raised. Any other exception is caught, written as an `error` event (parented to `assistant_message_id`), and returns `None`.

`_execute_tool_calls(mcp, turn_id, response, api_call_id)` → `(tool_results, last_tool_result_id)`:

Shared by both loops. For each `tool_use` block, writes a `tool_call` event, calls the tool, and **always** writes a matching `tool_result` event — even if `mcp.call_tool` raises (the exception text becomes the result with `is_error: true`). This guarantees every `tool_use` in the projection has its `tool_result`; the API rejects a history where one is missing.

All events carry correct `parent_event_id` (causality chain). In-memory message list is discarded at turn end; next turn re-projects from the DB.

## Telegram Handler (`bot/telegram_handler.py`)

For each message from the allowed user: send a "working…" ack, write a `telegram_ack` event, call `run_loop` under the same `turn_id`, then send `TurnResult.reply` and, if present, `TurnResult.proactive`.

Replies go through `_send_reply()`:
- `split_message(text)` splits text into chunks of at most 4096 UTF-16 code units (Telegram's limit, counted the way Telegram counts it, so most emoji count as 2). It breaks at the last paragraph break, line break, or space in the second half of the window, and hard-cuts otherwise. It never returns empty chunks.
- If there is no text (e.g. the turn ended after tool calls), it sends `(done — no reply text)` instead, since Telegram rejects empty messages.

**One turn at a time:** the application is built with `concurrent_updates(False)` (python-telegram-bot's default, set explicitly). Updates are handled sequentially, so a message that arrives mid-turn waits for the current turn to finish. Turns must not overlap: they would interleave events and race on the context bound. Enabling concurrent updates requires a lock around `run_loop`.

**Logging:** the `httpx` logger is set to WARNING. At INFO it logs every request URL, and Telegram Bot API URLs contain the bot token (`https://api.telegram.org/bot<TOKEN>/...`). Keep it at WARNING or above.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake
- Fetches and converts the tool list to Anthropic API format
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` with `.tools` and `.call_tool(name, arguments)`

## State

The events table is the source of truth — a flat, append-only log. There is no "conversation" concept: the only grouping unit is `turn_id`. The projection bound is Claude's working memory, managed by the `set_context_bound` tool during the reflection step.

The messages array is projected from events at each turn start and discarded at turn end. In-memory state exists only for the duration of one turn. Tasks, shopping, and symptoms are ordinary tables written by tools; each write is also recorded as `tool_call`/`tool_result` events and in `mutation_log`.

The projection bound lives in the `context_bound` table (default `0`). `_load_from_event_id()` reads it at the start of the main turn and again at the start of the reflection step. Because it is persisted, it survives process restarts.

## Debug

`debug_causality_tree(turn_id)` in `bot/agent.py` — queries all events for a turn, walks the parent→child tree, returns an indented text representation.

## Prompt Templates

`PromptBuilder(template)` loads a named file from `prompts/` at init. `build(**kwargs)` fills in any caller-supplied kwargs.

**System prompts are static: nothing time-varying goes in them.** Any byte change to the system prompt invalidates the cache for it and everything after it. The current time reaches Claude through the messages instead:
- `project_messages()` prefixes every user message with its send time, e.g. `[Monday, October 12, 2026 at 5:03 PM CDT]\n…`, rendered by `format_time()` from the event's stored timestamp. Event timestamps never change, so the projected history is byte-stable.
- The reflection trigger message carries the current time.

`format_time(dt)` renders in the `TIMEZONE` env var's zone (default `America/Chicago`).

### Prompt caching

Three cache breakpoints per request: the last tool definition, the system prompt, and automatic caching (top-level `cache_control`) on the end of the message history. Within a tool-using turn, each API call reuses the previous call's prefix. Across turns, tools and system reuse the cache if a message arrives within the 5-minute TTL; the message history only reuses it if the context bound hasn't moved.

The main turn and the reflection step don't share cache entries: their tool lists differ (`set_context_bound`) and so do their system prompts. Check `cache_read_input_tokens` / `cache_creation_input_tokens` on `api_call` events to see what caching actually saves.

Two instances in `agent.py`:
- `_system_prompt` — renders `prompts/system.md`, used as the system block in the main turn (marked `cache_control: ephemeral`).
- `_reflection_prompt` — renders `prompts/reflection.md`, used as the system block in the reflection step (marked `cache_control: ephemeral`).

## Config

`.env` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. All four are consumed.

## Model

Configured in `bot/agent.py`:
- `_MODEL = "claude-sonnet-5-5"`.
- `_EFFORT = "medium"`, sent as `output_config.effort` on every call. Sonnet 5.5 runs adaptive thinking by default and thinking is billed as output tokens; effort controls how much it thinks. `"low"` is the next lever for cost.
- `_MAX_TOKENS = 16000`. Thinking counts toward `max_tokens`, so it is sized above the expected reply length.
- `_MAX_API_CALLS = 15` caps API calls per loop (main turn and reflection each).

Thinking blocks are kept in the in-memory message list within a turn (`response.content` is appended unchanged) but are not stored in events, so they are not replayed in later turns.

## Tests

`uv run pytest` runs the suite in `tests/`. `pytest` is a dev dependency; `pyproject.toml` puts both the repo root and `bot/` on the import path, matching how `bot/` modules import each other.

`tests/conftest.py` has an autouse fixture that points `db.connection` at a fresh temporary database for every test, so tests never touch `personal_assistant.db`.

Covered so far: `project_messages()` (`tests/test_project_messages.py`), `split_message()` (`tests/test_split_message.py`), and `run_loop()` including the reflection step (`tests/test_run_loop.py`, using a fake async client and fake MCP client).
