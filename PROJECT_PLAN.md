# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database. Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response via Telegram.

## Core Architecture Decisions

### Foundational
- **Model:** Configured in `bot/agent.py`. Expected to change as better models are released.
- **Prompt caching:** Applied to system prompt and tool list.
- **Tool architecture:** MCP server/client split. Tools live in `tools/server.py`; the bot is an MCP client.
- **DB access:** Curated tool functions only, not raw SQL.
- **Layering rule:** `tools/server.py` has no SQL; `db/` has no knowledge of MCP or Claude. Each layer only imports downward.
- **Error handling:** Low-level code raises naturally; the agent layer catches and forwards errors to the user.

### Events-Driven Architecture

Every meaningful thing that happens — incoming user message, outgoing assistant message, API call, tool invocation, tool result — is recorded as an event in a single append-only `events` table.

**Events are the source of truth.**

- **Append-only.** Events are immutable once written. Corrections are new compensating events. Enforced at the DB level (trigger).
- **Per-type payload versioning.** Every event payload is a JSON blob with a `v: <int>` field, versioned per event type. Consumers must check `v` and fail loudly on unknown versions.
- **Full payload captured.** Complete arguments and results of tool calls, complete user message, complete assistant response.
- **`messages` seeded from a projection of events.** At turn start, the `messages` array is built by querying the events table. Within the turn, new messages are appended to an in-memory accumulator. When the turn ends the list is discarded; the next turn re-projects from the DB.
- **Facts and other structured state are projections.** Kept in sync by the same tool call that writes the corresponding event. Rebuildable from the event log.
- **Mutation log is a separate layer.** `mutation_log` records row-level physical writes; `events` records semantic agent activity.

### Turn Model

A **turn** is one cycle: `user_message_in → [api_call / tool_call / tool_result]* → assistant_message_out → reflection_step`.

- Every event in a turn shares a single `turn_id`.
- The main response uses text-on-`end_turn` — Claude's natural output mode. Wrapping message-sending in a tool was considered and rejected: it kills streaming, doubles API calls for the common one-message case, creates ambiguity about `end_turn` text, and puts transport errors inside the agent loop.
- The **reflection step** is a post-turn mini agent loop. Claude reviews the completed turn with full tool access and may use tools (e.g., `set_context_bound`), produce a follow-up message, or signal `PASS` (nothing to add). See Stage 7.

### Causality: `parent_event_id`

Every event (except turn roots) carries a `parent_event_id` pointing to the event that caused it.

- `user_message` — no parent. Turn root.
- `api_call` — parent is the last new input event (first call: `user_message`; later calls: `tool_result`, or shared `api_call` ancestor for parallel results).
- `tool_call` — parent is the `api_call` that requested it.
- `tool_result` — parent is its `tool_call`.
- `assistant_message` — parent is the `api_call` that produced the final text.

### Responsiveness

Not a constraint. Everything runs synchronously. A lightweight "working…" Telegram ack is desired — see Deferred Work.

### Tool Surface

Start minimal. Add tools only when driven by concrete need. Current surface is **`add_fact`** — reads come for free via events projection. Read/search tools are added only when context can no longer fit the relevant events.

`set_context_bound` is planned (Stage 7) — replaces the dedicated `context_decision` meta-call with a tool Claude calls during the reflection step.

## Completed: Stage 3 — Events-Driven Rebuild ✓

**3a.** `events` table, indexes, append-only triggers. ✓
**3b.** `turn_id` (per turn) generated and threaded through the loop. ✓
**3c.** `project_messages()` builds the messages array from events at turn start. `run_loop` no longer takes or returns a messages list. ✓
**3d.** Tools stripped to `add_fact` only. Agent loop writes all five event types. ✓
**3e.** `parent_event_id` wired correctly for every event type. `debug_causality_tree()` added. ✓

## Completed: Stage 4 — Dynamic Context Trimming ✓

**4a.** Post-turn meta-call: after `assistant_message` is written, a lightweight API call passes the full event list (id, type, timestamp, payload) to Claude and asks for the `from_event_id` — the oldest event ID to include when projecting the next turn. Result stored as a `context_decision` event (`{"v": 1, "from_event_id": N}`). `project_messages` takes a `from_event_id` parameter; the meta-call updates a module-level `_from_event_id` used on every subsequent projection. ✓

`project_messages` reconstructs the full interleaved tool use/result sequence, not just user and assistant text turns. ✓

Meta-call failures (API errors, parse errors, invalid returned id) are caught and written as an `error` event (`{"v": 1, "context": "context_decision", "message": "..."}`) parented to `assistant_message`. `_from_event_id` is unchanged on failure. ✓

Read/search tools remain deferred. They are added only when the bounded projection can no longer serve the relevant context.

Note: The `context_decision` meta-call is superseded by the reflection step in Stage 7. The mechanism changes (dedicated meta-call → `set_context_bound` tool in a mini agent loop), but the goal is the same: bound the projection window.

## Completed: Stage 5 — Telegram Bot ✓

Each input channel gets its own handler. No shared channel abstraction — each transport is different enough that a common interface would be leaky. Handlers call `run_loop(user_message)` directly; no channel identity leaks into the agent.

**5a.** `bot/telegram_handler.py` — `python-telegram-bot` with long polling, sender allowlist, wire to `run_loop`. Send a "working…" ack at turn start and record it as an event. ✓

`bot/main.py` promoted to true entry point; test harness moved to `bot/harness.py`. ✓

DB migrations added to drop legacy `conversation_id` and `correlation_id` columns from the live DB. ✓

## Completed: Stage 6 — Monitoring (partial) ✓

**6a.** `systemd` service — unit file at `scripts/personal-assistant.service`, installed and running. ✓

**6b.** Structured logging — consistent log levels and format across all modules.

**6c.** Basic health checks.

## Completed: Stage 7 — Reflection Step ✓

Replaced the Stage 4 `context_decision` meta-call with a unified post-turn reflection step. After the main response, a mini agent loop runs where Claude can trim context, store facts, or send a follow-up message.

**7a.** `set_context_bound(from_event_id)` tool — persists the projection bound to a single-row `context_bound` table. Added to the MCP server. Hidden from the main turn; available only in the reflection step. ✓

**7b.** Reflection step (`_run_reflection`) — mini agent loop after `assistant_message`. Full tool access. `PASS` response suppresses the follow-up; any other text is delivered to the user. Failures written as `error` events. ✓

**7c.** Removed `_run_context_decision()`, `prompts/context_decision.md`, and the `context_decision` event type. `_load_from_event_id()` now reads from `context_bound` table. ✓

**7d.** `run_loop` returns `TurnResult(reply: str, proactive: str | None)`. Telegram handler and harness updated. ✓

**7e.** Both system prompts describe the full architecture so Claude understands its role at each step. Main turn prompt is focused on responding; reflection prompt explains the event/projection/bound system in detail. ✓

## Current Stage: Stage 8 — Tasks & Shopping

Two new tables and tool sets to support the core use case: keeping track of everything the user needs to do so nothing falls through the cracks, and Claude can surface the right things at the right time.

### Design Rationale

The user's core problem is *losing track of things* — home improvement projects, appointments, errands, finances. Value comes from Claude reasoning over the full picture and surfacing what's relevant: "It's Saturday, you have 5 hours — here are your top priorities" or "You're going to Home Depot — here's what you can pick up."

**Tasks and shopping are separate tables** because they have different shapes and lifecycles. A task has priority, effort estimates, areas, status lifecycle, and detail. A shopping item is a string that gets removed when you buy it. Forcing both into one table means meaningless null columns on every grocery item.

**Claude is the query engine.** For a single user's volume (dozens to low hundreds of active items), Claude loads all open items via read tools and reasons in-context. No complex SQL filtering needed — Claude decides what's relevant.

**Shopping items can link to tasks** via an optional `task_id`. "Fix bathroom vent" might generate shopping items like "4-inch flexi duct" and "HVAC tape." The link is quiet — `list_shopping` still returns a flat list. But Claude can trace from a task to its associated shopping items when reviewing a project.

**Tool output is compact by default.** `list_tasks` returns one-line summaries (id, area, summary, status, priority). `list_shopping` returns a simple list of item names. Detail is available on demand via `get_task`. This keeps token cost low for the common "scan everything" case.

**System improvements are just tasks.** If the assistant identifies a potential improvement to its own infrastructure, it stores it as a task with area `"system"`. No special mechanism needed.

### Schema

```sql
tasks (id, area, summary, status, priority, estimated_hours, detail_json, created_at, updated_at)
    status: 'open', 'done', 'waiting', 'someday'
    priority: nullable integer (lower = higher priority)
    estimated_hours: nullable real
    detail_json: nullable JSON blob for freeform notes, sub-steps, etc.

shopping (id, item, task_id, created_at)
    task_id: nullable FK to tasks(id), for items generated by a task
```

### Tools

| tool | available in | description |
|---|---|---|
| `add_task` | main turn + reflection | Create a new task |
| `update_task` | main turn + reflection | Update fields on an existing task |
| `list_tasks` | main turn + reflection | List tasks (compact: id, area, summary, status, priority). Filterable by status, area |
| `get_task` | main turn + reflection | Get full detail for a single task |
| `add_shopping` | main turn + reflection | Add an item to the shopping list |
| `list_shopping` | main turn + reflection | Return the full shopping list (item names only) |
| `remove_shopping` | main turn + reflection | Remove an item (bought or no longer needed) |

### Implementation Steps

**8a.** `tasks` table DDL in `_SCHEMA`, migration in `_migrate()` for existing DBs.
**8b.** `db/tasks.py` — CRUD: `add_task`, `update_task`, `get_task`, `list_tasks`.
**8c.** `shopping` table DDL, migration.
**8d.** `db/shopping.py` — CRUD: `add_shopping`, `list_shopping`, `remove_shopping`.
**8e.** MCP tool schemas and dispatch in `tools/server.py`.
**8f.** Update `docs/DESIGN.md` with new schema, tools, and data access docs.

Prompt updates and security hardening are deferred to subsequent stages.

## Stage 9 (Future) — Security Hardening

Secret management, DB access patterns, file permissions, rate limiting.

## Deferred Work

### Unit Tests
Full automated test coverage for the core agent layer. Priority areas:
- `project_messages()` — event projection logic, edge cases (empty log, trimmed bound, orphaned tool results)
- `_run_reflection()` — PASS handling, tool call path, error event writing, exception propagation
- `run_loop()` — end_turn path, tool_use loop, unexpected stop_reason
- `db/` layer — facts CRUD, upsert semantics, context_bound read/write, migration idempotency
- `mcp_client.py` — tool list formatting, cache_control attachment, call_tool dispatch

### Structured Logging in agent.py
Add `logging` calls throughout `agent.py` and `mcp_client.py`:
- API call start/end with token counts
- Tool dispatch (name, truncated input)
- Reflection step outcome (PASS vs. follow-up vs. error)
- Context bound changes

### Rate Limiting
Protect against rapid Telegram message bursts exhausting API credits:
- Per-turn debounce or simple in-flight lock so only one turn runs at a time
- Backpressure signal to user if a turn is already running
