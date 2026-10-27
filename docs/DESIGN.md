# Current Implementation

## Layout
```
bot/agent.py              Agent loop, event writing, projection (with history cap), causality tree debug
bot/core.py               Assistant core: turn queue + single worker that runs turns and delivers output to channels
bot/telegram_handler.py   Telegram channel adapter: allowlist, ack, submits messages to the core, delivers replies
bot/jobs.py               Wake-up job definitions (name, prompt, extra tools)
bot/scheduler.py          Scheduler: starts jobs on a clock (SCHEDULE, local time)
bot/heartbeat.py          Heartbeat: pings HEARTBEAT_URL every 5 min while the core is healthy
bot/webhooks.py           Webhook listener (127.0.0.1:8787, behind Caddy): POST /hooks/<name> starts a wake-up job
bot/main.py               Entry point: owns the Anthropic client and MCP session, starts the core, Telegram, webhooks and the scheduler
bot/harness.py            Test harness: sends scripted messages through the loop directly (no Telegram)
bot/mcp_client.py         MCP client: spawns server subprocess, handshake, tool dispatch
bot/prompt_builder.py     Loads prompt templates from prompts/; format_time() for message timestamps
tools/server.py           MCP server: tool schemas, routing, result formatting
db/connection.py          Connection management, schema DDL, init_db()
db/mutation_log.py        Appends entries to mutation_log within the caller's transaction
db/context.py             Read/write the context projection bound (context_bound table); turn_start(), turn_root_id()
db/tasks.py               Tasks CRUD
db/shopping.py            Shopping list CRUD
db/symptoms.py            Symptom log CRUD
db/query.py               Read-only ad-hoc SQL for the query tool
prompts/system.md         Main turn system prompt (static)
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
- `set_context_bound(from_event_id)` — validates the id exists in `events` (or is `0`, the start of the log), then updates `context_bound`. Raises `ValueError` for unknown ids.
- `turn_start(event_id)` → int — the turn root (`user_message`/`wakeup`) at or before an id; used to snap a mid-turn bound back to its turn.
- `turn_root_id(turns_back)` → int — the Nth most recent turn root (1 = latest), or `0`; used for the 30-turn hard cap.

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
| `wakeup`            | `job`, `prompt`                                          | None (turn root)    |
| `api_call`          | `model`, `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | `user_message`/`wakeup` or last `tool_result` |
| `tool_call`         | `tool_use_id`, `name`, `input`                           | `api_call`          |
| `tool_result`       | `tool_use_id`, `content`, `is_error`                     | `tool_call`         |
| `assistant_message` | `content`                                                | `api_call`          |
| `telegram_ack`      | `telegram_message_id`                                    | None (turn root)    |
| `error`             | `context`, `message`                                     | last input event of the turn |

`error` events record failures that are caught rather than surfaced to the user. `context` names the subsystem (`"main_turn"` or `"wakeup"`; older events also have `"reflection"`); `message` is the exception string or a description of the failure.

`api_call` cache counts come from `response.usage` (recorded as `0` when absent). Events written before they were added have no cache fields.

`tool_result.is_error` is `true` when the tool returned an error or the MCP call itself raised. Events written before this field existed have no `is_error`; the projection treats them as successful.

## Tools

| tool | available in | description |
|---|---|---|
| `notify_user` | wake-up turns only | Send the user a message via the primary channel. A `LocalTool` run in-process by the core, not an MCP tool. |
| `set_context_bound` | `trim_context` job only | Set the oldest event future projections include (`from_event_id`). An id mid-turn is moved back to that turn's root. Requires a `rationale` field. |
| `add_task` | all turns | Create a new task with area, summary, and optional priority/effort/detail. |
| `update_task` | all turns | Update fields on an existing task (status, priority, detail, etc.). |
| `list_tasks` | all turns | List tasks (all columns). Filterable by status, area, or summary text search. Pass status='all' for every status. |
| `get_task` | all turns | Get full detail for a single task by id. |
| `add_shopping` | all turns | Add an item to the shopping list, with optional task_id link. |
| `list_shopping` | all turns | Return the full shopping list (id and item name). |
| `remove_shopping` | all turns | Remove a shopping item by id. |
| `log_symptom` | all turns | Record a symptom. `occurred_at` defaults to now; `severity` is 1–10. |
| `update_symptom` | all turns | Correct or annotate a logged symptom. |
| `list_symptoms` | all turns | List symptoms, most recent first. Filter by name (substring) or `since` (ISO-8601 bound). Default limit 100. |
| `get_symptom` | all turns | Full detail for one symptom log by id. |
| `delete_symptom` | all turns | Delete a symptom log (use when logged in error). |
| `query` | all turns | Run an arbitrary read-only query (`SELECT` / `WITH … SELECT`) against any table (tasks, shopping, symptoms, events). Returns up to 200 rows. |

`set_context_bound` is in `agent._RESTRICTED_TOOLS`: hidden from every turn unless the job lists it in `Job.extra_tools` (only `trim_context` does). `agent.py` filters `mcp.tools` and re-attaches `cache_control` to the new last tool. Only a turn that has it sees event ids: `project_messages(show_ids=True)` labels turn roots (`[#812 · <time>]`), tool results and assistant messages (`[#827] …`). Ordinary turns don't, because Claude tends to copy a label pattern from its own past replies into new ones. The bound always lands on a turn root (`db.context.turn_start`), since cutting between a tool call and its result would make the API reject every later request.

The `query` tool covers all tables: `tasks`, `shopping`, `symptoms (id, symptom, severity, occurred_at, notes, logged_at)`, `events`, and `mutation_log`.

### Data Access Layer

**`db/query.py`**
- `run_query(sql)` → list of dicts — runs one statement on a connection opened in SQLite's read-only mode (`file:…?mode=ro`), so the database itself rejects writes. Any `SELECT` works, including `WITH … SELECT`. Returns at most 200 rows. Every SQLite failure (bad SQL, attempted write, multiple statements) is raised as `ValueError("Query failed: …")`, which the tool returns to Claude as an error message.

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

`run_loop(client, mcp, user_message, turn_id?, job?, local_tools?, extra_tools?)` → `TurnResult(reply)`. `client` is an `AsyncAnthropic`; every API call is awaited, so the event loop is never blocked while waiting on Claude.

1. Generates a fresh `turn_id` (UUID hex).
2. Writes the `user_message` event.
3. Calls `_load_from_event_id()` to get the projection bound: the `context_bound` table, or the root of the 30th most recent turn (`_MAX_TURNS`) if that is later.
4. Calls `project_messages(from_event_id)` to build the messages list, which now ends with the new user message. Projecting after the write means the new message is rendered from its stored timestamp, byte-identical to how later turns will render it.
5. Filters `mcp.tools` to exclude restricted tools the turn's job doesn't allow.
6. Loops (main turn):
   - Calls Claude with the history, system prompt (`cache_control: ephemeral`), filtered tool list, and top-level `cache_control: ephemeral` (automatic caching of the message history). Writes an `api_call` event.
   - On `end_turn`: writes an `assistant_message` event.
   - On `tool_use`: calls `_execute_tool_calls()` (below), appends results to messages, loops.
   - On `refusal`: returns a reply saying Claude declined, with the `stop_details` category.
   - On any other stop reason: returns an "Unexpected stop reason" reply.
7. Returns `TurnResult(reply=final_text)`.
8. If the loop hits `_MAX_API_CALLS` (15) without finishing, writes an `error` event (`context: "main_turn"`) and returns a short failure reply.

`_execute_tool_calls(mcp, turn_id, response, api_call_id)` → `(tool_results, last_tool_result_id)`:

For each `tool_use` block, writes a `tool_call` event, calls the tool, and **always** writes a matching `tool_result` event — even if `mcp.call_tool` raises (the exception text becomes the result with `is_error: true`). This guarantees every `tool_use` in the projection has its `tool_result`; the API rejects a history where one is missing.

All events carry correct `parent_event_id` (causality chain). In-memory message list is discarded at turn end; next turn re-projects from the DB.

## Core (`bot/core.py`)

The core is the master; channels plug into it. `bot/main.py` creates the Anthropic client and MCP session, builds a `Core` around them, and registers channels. Channels import the core, never the reverse.

**One turn at a time:** every turn goes through one `asyncio.Queue` drained by a single worker task. Turns must not overlap: they would interleave events and race on the context bound. A message that arrives mid-turn waits in the queue. (python-telegram-bot's `concurrent_updates` no longer matters for this; the Telegram handler only acks and enqueues.)

**Channel interface:** a channel has a `name` and an async `deliver(text)`. Inbound, it calls `core.submit(text, channel, turn_id=None)`, which queues the turn and returns immediately. The worker runs `run_loop`, then delivers `TurnResult.reply` to the originating channel. If `run_loop` raises, the channel gets "Something went wrong. Please try again." Exceptions from `run_loop` or `deliver` are logged and never kill the worker.

**Shutdown:** `Core.stop()` lets the in-flight turn finish rather than cancelling it — cancelling mid-tool-call would leave a `tool_call` with no `tool_result`. Turns still queued are dropped with a WARNING; their `user_message` events were never written, so history stays consistent (their `telegram_ack` events remain). `main.py` nests the context managers so shutdown runs: stop Telegram polling → stop the core → shut down the Telegram bot (the in-flight turn can still deliver).

**Wake-ups:** `core.submit_wakeup(job)` queues a turn with no inbound message, on the same queue as user turns. `run_loop(..., job=name, local_tools=[...])` writes a `wakeup` root event instead of `user_message`, offers the local tools after the MCP tools, and unhides the job's `extra_tools`. The final reply is written as an `assistant_message` but not delivered. To reach the user, Claude calls `notify_user`, which delivers to `core.primary_channel` (Telegram). A failed wake-up is logged, not sent. `project_messages` renders a wake-up root as a timestamped user message: `(wake-up: <job>)` then the prompt.

## Webhooks (`bot/webhooks.py`)

Caddy (`/etc/caddy/Caddyfile`, a copy of `scripts/Caddyfile`) terminates HTTPS for `pa.mattfae.com`, gets and renews the certificate itself, forwards `/hooks/*` to `127.0.0.1:8787`, and answers everything else with 404. The listener runs in the bot process and only starts if `WEBHOOK_TOKEN` is set in `.env`.

`POST /hooks/<name>` with `Authorization: Bearer <WEBHOOK_TOKEN>` (compared in constant time):
- wrong/missing token → 401; unknown name → 404; other methods → 405
- within the hook's cooldown → 200 `ignored (cooldown)` (geofences can fire repeatedly; cooldowns are in memory and reset on restart)
- otherwise → 202, and the hook's job is queued as a wake-up

| hook | job | cooldown | trigger |
|---|---|---|---|
| `arrived-home` | `arrived_home` | 15 min | iPhone Shortcuts personal automation: Arrive at Home → Get Contents of URL |

## Scheduler (`bot/scheduler.py`)

`SCHEDULE` is a list of `Schedule(job, at, weekdays?, should_run?)` in local time (`TIMEZONE`), so times hold across DST changes. The scheduler ticks every 30 s; a job fires when its most recent scheduled time has passed and it hasn't run since. The last run per job is read from `wakeup` events at startup, then tracked in memory. A run missed while the bot was down is caught up if under 2 hours old, else skipped. `should_run` lets a job skip without an API call.

| job | when | does |
|---|---|---|
| `weekend_plan` | Sat 09:00 | Suggests a few open tasks for the weekend via `notify_user` |
| `trim_context` | Mon 03:00, only if there were turns since its last run | Tidies tasks/shopping from the conversation, then trims history with `set_context_bound` |

## Telegram Channel (`bot/telegram_handler.py`)

`TelegramChannel` is the Telegram adapter. For each message from the allowed user: send a "working…" ack, write a `telegram_ack` event, and submit the message to the core under the same `turn_id`. The handler returns right away.

`deliver(text)` sends to the allowed user's private chat (the chat id equals the user id), so it doesn't need an incoming message to reply to:
- `split_message(text)` splits text into chunks of at most 4096 UTF-16 code units (Telegram's limit, counted the way Telegram counts it, so most emoji count as 2). It breaks at the last paragraph break, line break, or space in the second half of the window, and hard-cuts otherwise. It never returns empty chunks.
- If there is no text (e.g. the turn ended after tool calls), it sends `(done — no reply text)` instead, since Telegram rejects empty messages.

**Polling errors:** `start_polling` gets `_on_polling_error` as its `error_callback`. Transient network errors (Telegram returning 502, dropped connections) are logged as a one-line WARNING; python-telegram-bot retries them on its own. Other errors, including `BadRequest` (which subclasses `NetworkError`), keep their traceback.

## Logging (`bot/log_setup.py`)

Each process configures logging once, at its entry point, with `configure_logging()`: `bot/main.py`, `bot/harness.py`, and `tools/server.py`. Library modules only create loggers. Everything goes to stderr (in the MCP server, stdout carries the protocol).

- **Journal vs terminal.** When stderr is the systemd journal (detected by matching `JOURNAL_STREAM` against stderr's device and inode), every line, traceback lines included, is prefixed with a syslog priority (`<3>` error, `<4>` warning, `<6>` info). journald strips the prefix and stores the real level, so `journalctl -p warning` works. No timestamp, since journald adds one. In a terminal, lines carry a timestamp instead.
- **MCP server.** The MCP SDK passes the server subprocess only an allowlist of environment variables, so `mcp_client` adds `JOURNAL_STREAM` explicitly. The server inherits the bot's stderr. The SDK's own `mcp` logger is set to WARNING; at INFO it logs every request.
- **`httpx` is set to WARNING.** At INFO it logs every request URL, and Telegram Bot API URLs contain the bot token (`https://api.telegram.org/bot<TOKEN>/...`). Keep it at WARNING or above.
- **Turn summary.** `run_loop` logs one INFO line when a turn ends, success or not: API call count and token totals, read back from the turn's `api_call` events. Each `error` event in the turn is also logged as a WARNING with its event id. Refusals and unexpected stop reasons are logged as warnings.

The events table remains the detailed record of what happened in a turn; the journal is for process-level activity and a quick view of turns.

## MCP Client (`bot/mcp_client.py`)

`mcp_client()` is an async context manager that:
- Spawns `tools/server.py` as a subprocess via `StdioServerParameters`
- Performs the MCP initialization handshake
- Fetches and converts the tool list to Anthropic API format
- Adds `cache_control: ephemeral` to the last tool
- Yields an `MCPClient` with `.tools` and `.call_tool(name, arguments)`

## State

The events table is the source of truth — a flat, append-only log. There is no "conversation" concept: the only grouping unit is `turn_id`. The projection bound is Claude's working memory, trimmed weekly by the `trim_context` job via `set_context_bound` and capped at the last 30 turns.

The messages array is projected from events at each turn start and discarded at turn end. In-memory state exists only for the duration of one turn. Tasks, shopping, and symptoms are ordinary tables written by tools; each write is also recorded as `tool_call`/`tool_result` events and in `mutation_log`.

The projection bound lives in the `context_bound` table (default `0`). `_load_from_event_id()` reads it at the start of each turn and applies the 30-turn cap (the cap is not written back). Because it is persisted, it survives process restarts.

## Debug

`debug_causality_tree(turn_id)` in `bot/agent.py` — queries all events for a turn, walks the parent→child tree, returns an indented text representation.

## Prompt Templates

`PromptBuilder(template)` loads a named file from `prompts/` at init. `build(**kwargs)` fills in any caller-supplied kwargs.

**System prompts are static: nothing time-varying goes in them.** Any byte change to the system prompt invalidates the cache for it and everything after it. The current time reaches Claude through the messages instead:
- `project_messages()` prefixes every user message with its send time, e.g. `[Monday, October 12, 2026 at 5:03 PM CDT]\n…`, rendered by `format_time()` from the event's stored timestamp. Event timestamps never change, so the projected history is byte-stable.

`format_time(dt)` renders in the `TIMEZONE` env var's zone (default `America/Chicago`).

### Prompt caching

Three cache breakpoints per request: the last tool definition, the system prompt, and automatic caching (top-level `cache_control`) on the end of the message history. Within a tool-using turn, each API call reuses the previous call's prefix. Across turns, tools and system reuse the cache if a message arrives within the 5-minute TTL; the message history only reuses it if the context bound hasn't moved.

The `trim_context` job's tool list differs (it includes `set_context_bound`), so it doesn't share cache entries with other turns; that's fine weekly. When the 30-turn cap advances, the history prefix changes, so the history cache is rebuilt then too. Check `cache_read_input_tokens` / `cache_creation_input_tokens` on `api_call` events to see what caching actually saves.

`_system_prompt` in `agent.py` renders `prompts/system.md`, used as the system block for every turn (marked `cache_control: ephemeral`).

## Config

`.env` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`, and optionally `WEBHOOK_TOKEN` and `TIMEZONE`. All are consumed.

## Model

Configured in `bot/agent.py`:
- `_MODEL = "claude-sonnet-5-5"`.
- `_EFFORT = "medium"`, sent as `output_config.effort` on every call. Sonnet 5.5 runs adaptive thinking by default and thinking is billed as output tokens; effort controls how much it thinks. `"low"` is the next lever for cost.
- `_MAX_TOKENS = 16000`. Thinking counts toward `max_tokens`, so it is sized above the expected reply length.
- `_MAX_API_CALLS = 15` caps API calls per turn.

Thinking blocks are kept in the in-memory message list within a turn (`response.content` is appended unchanged) but are not stored in events, so they are not replayed in later turns.

## Tests

`uv run pytest` runs the suite in `tests/`. `pytest` is a dev dependency; `pyproject.toml` puts both the repo root and `bot/` on the import path, matching how `bot/` modules import each other.

`tests/conftest.py` has an autouse fixture that points `db.connection` at a fresh temporary database for every test, so tests never touch `personal_assistant.db`.

Covered so far: `project_messages()` (`tests/test_project_messages.py`), `split_message()` (`tests/test_split_message.py`), `run_loop()` (`tests/test_run_loop.py`, using a fake async client and fake MCP client), `run_query()` (`tests/test_query.py`), the core's turn queue (`tests/test_core.py`), webhooks (`tests/test_webhooks.py`), the scheduler (`tests/test_scheduler.py`), the heartbeat (`tests/test_heartbeat.py`), history trimming (`tests/test_context.py`), logging (`tests/test_logging.py`), the `db/` CRUD modules (`tests/test_db.py`) and the MCP client, run against the real tools server subprocess on the test database (`tests/test_mcp_client.py`).
