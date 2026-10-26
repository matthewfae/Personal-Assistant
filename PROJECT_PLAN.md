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
- **Structured state (tasks, shopping, symptoms) is written by tools.** Each write is recorded as a `tool_call` / `tool_result` event pair, plus a row in `mutation_log`.
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

Start minimal. Add tools only when driven by concrete need. Current surface: task, shopping and symptom CRUD tools, a read-only `query` tool for ad-hoc SQL over any table, and `set_context_bound` (reflection step only). The full list lives in `docs/DESIGN.md` → Tools.

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

**6b.** Logging — one `configure_logging()` per process, journald priorities, per-turn summary line, quiet Telegram network blips, MCP server logs formatted. See `docs/DESIGN.md` → Logging. ✓

**6c.** Basic health checks. *(Not started — folded into Stage 10.)*

## Completed: Stage 7 — Reflection Step ✓

Replaced the Stage 4 `context_decision` meta-call with a unified post-turn reflection step. After the main response, a mini agent loop runs where Claude can trim context, call tools, or send a follow-up message.

**7a.** `set_context_bound(from_event_id)` tool — persists the projection bound to a single-row `context_bound` table. Added to the MCP server. Hidden from the main turn; available only in the reflection step. ✓

**7b.** Reflection step (`_run_reflection`) — mini agent loop after `assistant_message`. Full tool access. `PASS` response suppresses the follow-up; any other text is delivered to the user. Failures written as `error` events. ✓

**7c.** Removed `_run_context_decision()`, `prompts/context_decision.md`, and the `context_decision` event type. `_load_from_event_id()` now reads from `context_bound` table. ✓

**7d.** `run_loop` returns `TurnResult(reply: str, proactive: str | None)`. Telegram handler and harness updated. ✓

**7e.** Both system prompts describe the full architecture so Claude understands its role at each step. Main turn prompt is focused on responding; reflection prompt explains the event/projection/bound system in detail. ✓

## Completed: Stage 8 — Tasks & Shopping ✓

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

## Completed: Stage 8b — Symptoms Tracking ✓

New `symptoms` table for logging medical symptom occurrences with severity and timestamps.

*(Task: "Add medical symptoms tracking table/feature to Claude assistant app" — done.)*

**Design notes:**
- Two timestamps: `occurred_at` (when the symptom happened, user-reported) and `logged_at` (when it was recorded). This handles the common case of reporting a symptom after the fact.
- Severity is a nullable 1–10 integer.
- Claude is the query/analysis engine — use `list_symptoms` for recent history, `query` for aggregations ("headaches per week").
- `delete_symptom` rather than status lifecycle — a symptom either happened or it didn't. Corrections are updates; "logged in error" is deletion.

**8b tools:** `log_symptom`, `update_symptom`, `list_symptoms`, `get_symptom`, `delete_symptom`.

## Current TODO — Review Findings (2026-10-12)

Findings from a code review. Ordered by priority within each group.

### High priority
- [x] **Secret/data file permissions.** `.env` is `664` and `personal_assistant.db` is `644` — readable by any local user. `chmod 600` both. (Host-only; not tracked in git.)
- [x] **Tool errors can poison the history.** In `run_loop` / `_run_reflection`, if `mcp.call_tool` raises, the `tool_call` event is written but no `tool_result`. Every later projection then contains a `tool_use` with no matching `tool_result`, which the API rejects, so every turn fails until the context bound moves past it. Fixed: shared `_execute_tool_calls` always writes a `tool_result`; MCP `isError` is now propagated as `is_error` (ordinary tool exceptions were already returned as error results by the MCP server, so this only bit on transport failures).
- [x] **No iteration cap on agent loops.** Neither `run_loop` nor `_run_reflection` limits rounds, so a looping model can burn API credits. Fixed: both loops capped at `_MAX_API_CALLS` (15); hitting it writes an `error` event.

### Robustness
- [x] **Telegram send failures.** `reply_text` fails on messages over 4096 chars and on empty strings (a tool-only `end_turn` yields `""`). Fixed: `split_message` splits replies at paragraph/line/word boundaries within Telegram's 4096 UTF-16-unit limit; empty replies send a placeholder.
- [x] **Sync client in async code.** `Anthropic` (sync) is called inside async handlers, blocking the event loop for each API call. Switch to `AsyncAnthropic` and `await` the calls.
  - *Concurrency note:* switching does **not** let turns overlap. python-telegram-bot handles one update at a time by default (`concurrent_updates` off), so turns are already serialized by the framework, not by the blocking client. Set `.concurrent_updates(False)` explicitly in `ApplicationBuilder` with a comment, so the guarantee is visible and nobody turns it on without adding a lock. (Concurrent turns would interleave events and race on `context_bound`.)
  - Done (#48): `AsyncAnthropic` everywhere, `concurrent_updates(False)` explicit.
- [x] **`query` read-only enforcement.** The `startswith("SELECT")` check rejects valid `WITH …` queries and isn't a real guarantee. Open a read-only connection (`file:...?mode=ro`, `uri=True`) for `run_query` instead, so SQLite itself refuses writes. Keep the `_MAX_ROWS` cap (200) so a careless `SELECT * FROM events` can't flood the context. Done (#51); SQL errors now go back to Claude as messages instead of logged tool failures.

### Housekeeping
- [x] **Tests.** `tests/` is empty. Start with `project_messages()` (pure logic, tricky edge cases); see Deferred Work → Unit Tests. Do this *first*, so the async and timestamp changes below have something to check against. Done (#47–#49, #51): 33 tests over `project_messages`, `split_message`, `run_loop` (fake async client) and `run_query`; `uv run pytest`.
- [x] **Stale docs.** README says "Stages 1–5 complete"; "Tool Surface" section above still references `add_fact`; Stage 8 is still marked current.
- [x] **Model version.** Upgraded from `claude-sonnet-4-6` to `claude-sonnet-5-5` (cheaper: $2/$10 vs $3/$15 per MTok). Sonnet 5.5 thinks by default, so effort is set explicitly (`medium`) and `max_tokens` raised to 16000. Check `api_call` token counts after a few days to confirm the net saving.
- [x] **Prompt cache invalidated every minute.** `{current_time}` in both system prompts is rendered to the minute, so the cached system prompt is discarded whenever the minute changes.
  - *Fix:* move the time out of the system prompt rather than rounding it. Rounding to the hour would lose the minute precision Claude needs for "remind me in 20 minutes" or "what can I do before 5?". Instead, `project_messages` prefixes each user message with its send time, taken from the event's own (immutable) `timestamp`, so the rendered history is byte-stable across turns. The reflection trigger carries the current time the same way. Both system prompts become fully static.
  - *Also:* there is currently no cache breakpoint on `messages`, so every API call pays full price for the whole history — including the 2nd–15th calls within a single tool-using turn. Add top-level automatic caching (`cache_control={"type": "ephemeral"}` on `messages.create`), and record `cache_read_input_tokens` / `cache_creation_input_tokens` in `api_call` events so cache hits can actually be verified.
  - Done (#49). Caveat: the main turn and reflection have different tools/system prompts, so they never share cache entries, and moving the context bound breaks the cached message history. Check the new counts after a week to see whether caching pays for its write premium at this usage level.
  - *Checked 2026-10-26* (5 turns, 21 calls since #49): caching saves ~39% net of the write premium (~60–65% of input tokens are cache reads, almost all within a turn; the 5-minute TTL usually expires between turns). Cache writes are now 64% of spend. Cost per turn is flat at ~3¢ vs ~2.8¢ on Sonnet 4.6: lower prices and caching were offset by prompts roughly doubling (~4.7K vs ~2.4K tokens per call) and thinking output (~188 vs ~79 output tokens per call). Pre-#49 events didn't record cache tokens, so the old figure is approximate. Total spend since April is ~$2; no further tuning warranted at this volume.

### Work order
1. Tests for `project_messages()` (and `split_message`), with `pytest` as a dev dependency.
2. `AsyncAnthropic` + explicit `concurrent_updates(False)`.
3. Time out of the system prompt + message caching + cache token counts in `api_call` events.
4. Read-only connection for `query`.

Separately: Stage 6c (health checks) is not done; 6b (logging) is. The `api_call` token counts from step 3 also cover the "check token counts" follow-up from the model upgrade.

### Ops (outside the repo)
- [x] **`gh` auth is broken.** `gh` returns 401 Bad credentials. Re-run `gh auth login`.
- [x] **Bot token written to the journal.** `httpx` logged every Telegram API URL at INFO, and those URLs contain the bot token. Fixed: `httpx` logger set to WARNING in `telegram_handler.py`.
- [x] **Rotate the Telegram bot token.** The old token was in past journal entries and was exposed in a Claude Code session. Revoked via @BotFather and replaced in `.env` (2026-10-12).

## Stage 10 (Next) — Core Service & Scheduled Wake-ups

Claude should be able to wake on a timer, get a prompt, and pick from a repertoire of jobs, not only react to Telegram messages. That requires inverting the process: today `telegram_handler.main()` is the entry point and owns the Anthropic client, the MCP session and turn serialization. Instead, the **assistant core is the master** and Telegram is one comms channel plugged into it.

### Design

**Core owns the process.** A `core/` package (name TBD) owns startup, the Anthropic client, the MCP session, the scheduler and a single **turn queue**. `bot/main.py` (or its replacement) starts the core and then registers channels.

**One turn queue serializes everything.** Every turn — user message or wake-up — is submitted to one `asyncio.Queue` drained by a single worker. This replaces the implicit guarantee from `concurrent_updates(False)`, which only serializes Telegram updates; scheduled jobs would otherwise run concurrently with a user turn, interleaving events and racing on the context bound. The queue also gives the Rate Limiting deferred item a natural home (coalesce messages that arrive while a turn is running).

**Channels are thin adapters.** A channel does two things: submits inbound messages to the core, and delivers outbound text. Minimal interface: `core.submit(text, channel)` inbound; `channel.deliver(text)` outbound. Replies go back to the originating channel; unprompted messages from wake-ups go to a configured primary channel (Telegram). This supersedes Stage 5's "no shared channel abstraction" — outbound delivery from the core now needs one. Keep it this small; transport specifics (ack messages, splitting, placeholders) stay in the adapter.

**Same process for now.** Core and Telegram adapter run in one process with a clean module boundary (the adapter imports the core, never the reverse). Split into separate processes with an IPC/HTTP API only when a second channel needs it — until then that adds auth, a protocol and a second systemd unit for no gain.

**Wake-ups are turns.** New turn-root event type `wakeup` (`{"v": 1, "job": "<name>", "prompt": "..."}`), parallel to `user_message`. `run_loop` is generalized to start from either root; everything downstream (api_call / tool_call / tool_result, causality, logging) is unchanged, so the events table is a full audit of what each job did. `project_messages` renders wake-up roots so Claude can see past jobs.

**Wake-ups are silent by default.** A wake-up turn's final text is not sent anywhere. To reach the user, Claude calls a `notify_user(text)` tool (wake-up turns only). This avoids the `PASS`-sentinel failure mode — reflection leaked "…PASS" to the user twice and wrote 13 empty `assistant_message` events. Stage 7's argument against a send-message tool applies to replies, where speaking is the norm; for wake-ups, silence is the norm.

**Scheduler.** In-process, owned by the core — not python-telegram-bot's `JobQueue`, which belongs to the channel. A small asyncio loop over a job table (name, schedule, prompt) is enough; APScheduler if schedules get complex. Jobs are defined in code/config at first.

### Jobs

- **Nightly consolidation** (replaces the reflection step's context management). Claude reviews the day's events, writes anything durable (decisions, preferences, facts) into tables, *then* advances the projection bound. Trimming after saving loses nothing — unlike today, where reflection once trimmed too aggressively and lost the shopping list it had just shown.
- **Hard cap backstop.** `project_messages` also enforces a fixed maximum (e.g. last 30 turns) so a failing consolidation job can't let history grow unbounded.
- Later: morning brief, overdue/stale tasks, symptom pattern review.
- **Cost:** a wake-up costs about what a turn does (~3¢). Daily ≈ $1/month; hourly ≈ $20/month. Start with a few per day.

### Retire the reflection step

Data from 58 reflections: 48 were `set_context_bound` only, ~3 did real tidy-up work, and 7 produced follow-up messages — 3 of them correcting confusion caused by the split itself (the main turn can't see `set_context_bound`, so it tells the user the tool doesn't exist). Once nightly consolidation owns the bound: delete `_run_reflection`, `prompts/reflection.md` and `TurnResult.proactive`; keep `set_context_bound` but expose it only to the consolidation job. Add a line to the main prompt to tidy related items before finishing. Halves API calls per turn and gives one shared prompt-cache prefix.

### Health checks (absorbs Stage 6c)

Every job leaves events, so "no `wakeup` event in 25 hours" is a liveness signal. Pair with systemd `OnFailure=` for crashes. Alerting must not depend only on the process being checked — a cheap external check (cron or a heartbeat service) is needed for "the whole box is down".

### Steps (draft)

**10a.** ✓ Core: owns client, MCP session, turn queue + single worker. Telegram handler becomes an adapter that submits to the queue and implements `deliver`. No behavior change. Done as a module, `bot/core.py`, not a package — `bot/` modules import each other flat, and a package can come when the core grows. In-flight turns finish on shutdown; queued ones are dropped.
**10b.** `wakeup` turn root, `run_loop` generalization, `project_messages` rendering, `notify_user` tool.
**10c.** Scheduler + job table; first job: nightly consolidation. Hard-cap backstop in `project_messages`.
**10d.** Remove reflection step.
**10e.** Liveness check + `OnFailure=`.
**10f.** More jobs.

## Stage 9 (Future) — Security Hardening

Secret management, DB access patterns, file permissions, rate limiting.

## Deferred Work

### Unit Tests
Full automated test coverage for the core agent layer. Priority areas:
- `project_messages()` — event projection logic, edge cases (empty log, trimmed bound, orphaned tool results)
- `_run_reflection()` — PASS handling, tool call path, error event writing, exception propagation
- `run_loop()` — end_turn path, tool_use loop, unexpected stop_reason
- `db/` layer — tasks/shopping CRUD, context_bound read/write, migration idempotency
- `mcp_client.py` — tool list formatting, cache_control attachment, call_tool dispatch

### Structured Logging in agent.py
Add `logging` calls throughout `agent.py` and `mcp_client.py`:
- API call start/end with token counts
- Tool dispatch (name, truncated input)
- Reflection step outcome (PASS vs. follow-up vs. error)
- Context bound changes

Keep the `httpx` logger at WARNING or above: its INFO lines include Telegram API URLs, which contain the bot token.

### Rate Limiting
Protect against rapid Telegram message bursts exhausting API credits:
- One-turn-at-a-time is already provided by python-telegram-bot's default sequential update handling (see the sync-client TODO). What's missing is protection against *volume*: a burst of N messages still queues N full turns.
- Options: coalesce messages that arrive while a turn is running into the next turn, or cap turns per time window.
- Backpressure signal to user if a turn is already running
