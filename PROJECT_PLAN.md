# Personal Assistant Project Plan

## Overview
Personal assistant built around the Claude API and a local SQLite database. An assistant core runs turns one at a time: messages from Telegram, and wake-ups from a webhook (iPhone arrival) or the in-process scheduler. In each turn Claude uses MCP tools to read/write the DB in a multi-step agent loop, then replies via Telegram (or, for wake-ups, messages the user only if worthwhile).

## Core Architecture Decisions

### Foundational
- **Model:** Configured in `bot/agent.py`. Expected to change as better models are released.
- **Prompt caching:** Applied to the tool list, system prompt and message history.
- **Tool architecture:** MCP server/client split. Tools live in `tools/server.py`; the bot is an MCP client.
- **DB access:** Curated tool functions for writes; a read-only `query` tool for ad-hoc SQL.
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

A **turn** is one cycle: `user_message | wakeup → [api_call / tool_call / tool_result]* → assistant_message`.

- Every event in a turn shares a single `turn_id`.
- The main response uses text-on-`end_turn` — Claude's natural output mode. Wrapping message-sending in a tool was considered and rejected: it kills streaming, doubles API calls for the common one-message case, creates ambiguity about `end_turn` text, and puts transport errors inside the agent loop.
- Turns never overlap: the core runs them from one queue (Stage 10a).
- History is trimmed by a weekly `trim_context` wake-up, with a 30-turn hard cap (Stage 10c-2). The per-turn reflection step (Stage 7) was removed in 10d.

### Causality: `parent_event_id`

Every event (except turn roots) carries a `parent_event_id` pointing to the event that caused it.

- `user_message`, `wakeup` — no parent. Turn roots.
- `telegram_ack` — no parent; written before the `user_message` under the same `turn_id`.
- `api_call` — parent is the last new input event (first call: `user_message`; later calls: `tool_result`, or shared `api_call` ancestor for parallel results).
- `tool_call` — parent is the `api_call` that requested it.
- `tool_result` — parent is its `tool_call`.
- `assistant_message` — parent is the `api_call` that produced the final text.

### Responsiveness

Not a constraint. API calls are async (`AsyncAnthropic`) so they don't block the process, but turns run one at a time. Telegram gets a "working…" ack as soon as a message arrives.

### Tool Surface

Start minimal. Add tools only when driven by concrete need. Current surface: task, shopping and symptom CRUD tools, a read-only `query` tool for ad-hoc SQL over any table, and `set_context_bound` (`trim_context` job only). Wake-up turns also get `notify_user`. The full list lives in `docs/DESIGN.md` → Tools.

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

Note: The `context_decision` meta-call was superseded by the reflection step in Stage 7, which was itself replaced by the weekly `trim_context` job in Stage 10. The mechanism changes (dedicated meta-call → `set_context_bound` tool in a mini agent loop), but the goal is the same: bound the projection window.

## Completed: Stage 5 — Telegram Bot ✓

*(Superseded in Stage 10a: the core now owns turns, and channels are thin adapters with a `deliver()` method.)* Each input channel gets its own handler. No shared channel abstraction — each transport is different enough that a common interface would be leaky. Handlers call `run_loop(user_message)` directly; no channel identity leaks into the agent.

**5a.** `bot/telegram_handler.py` — `python-telegram-bot` with long polling, sender allowlist, wire to `run_loop`. Send a "working…" ack at turn start and record it as an event. ✓

`bot/main.py` promoted to true entry point; test harness moved to `bot/harness.py`. ✓

DB migrations added to drop legacy `conversation_id` and `correlation_id` columns from the live DB. ✓

## Completed: Stage 6 — Monitoring (partial) ✓

**6a.** `systemd` service — unit file at `scripts/personal-assistant.service`, installed and running. ✓

**6b.** Logging — one `configure_logging()` per process, journald priorities, per-turn summary line, quiet Telegram network blips, MCP server logs formatted. See `docs/DESIGN.md` → Logging. ✓

**6c.** Basic health checks. *(Not started — now Stage 10e.)*

## Completed: Stage 7 — Reflection Step ✓ *(removed in Stage 10d)*

*Historical.* The reflection step was replaced by the weekly `trim_context` job; see Stage 10.

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

*As designed in Stage 8; the current list is in `docs/DESIGN.md` → Tools.*

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

## Completed: Review Findings (2026-10-12) ✓

All findings from the October code review are fixed: secret/DB file permissions, tool errors no longer poison history, API calls capped per turn (15), Telegram message splitting, `AsyncAnthropic`, read-only `query` connection, tests (#47–#51), stale docs, Sonnet 5.5 upgrade, prompt caching with the time moved out of the system prompt (#49), `gh` auth, the bot token kept out of the journal, and the Telegram token rotated.

*Caching check (2026-10-26, 21 calls):* caching saves ~39% net of the write premium; ~3¢ per turn; total spend since April ~$2. No further tuning warranted at this volume. Removing reflection (10d) has since cut one API call from every message.

## Stage 10 (Current) — Core Service & Scheduled Wake-ups

Claude should be able to wake on a timer, get a prompt, and pick from a repertoire of jobs, not only react to Telegram messages. That requires inverting the process: today `telegram_handler.main()` is the entry point and owns the Anthropic client, the MCP session and turn serialization. Instead, the **assistant core is the master** and Telegram is one comms channel plugged into it.

### Design

**Core owns the process.** A `core/` package (name TBD) owns startup, the Anthropic client, the MCP session, the scheduler and a single **turn queue**. `bot/main.py` (or its replacement) starts the core and then registers channels.

**One turn queue serializes everything.** Every turn — user message or wake-up — is submitted to one `asyncio.Queue` drained by a single worker. This replaces the implicit guarantee from `concurrent_updates(False)`, which only serializes Telegram updates; scheduled jobs would otherwise run concurrently with a user turn, interleaving events and racing on the context bound. The queue also gives the Rate Limiting deferred item a natural home (coalesce messages that arrive while a turn is running).

**Channels are thin adapters.** A channel does two things: submits inbound messages to the core, and delivers outbound text. Minimal interface: `core.submit(text, channel)` inbound; `channel.deliver(text)` outbound. Replies go back to the originating channel; unprompted messages from wake-ups go to a configured primary channel (Telegram). This supersedes Stage 5's "no shared channel abstraction" — outbound delivery from the core now needs one. Keep it this small; transport specifics (ack messages, splitting, placeholders) stay in the adapter.

**Same process for now.** Core and Telegram adapter run in one process with a clean module boundary (the adapter imports the core, never the reverse). Split into separate processes with an IPC/HTTP API only when a second channel needs it — until then that adds auth, a protocol and a second systemd unit for no gain.

**Wake-ups are turns.** New turn-root event type `wakeup` (`{"v": 1, "job": "<name>", "prompt": "..."}`), parallel to `user_message`. `run_loop` is generalized to start from either root; everything downstream (api_call / tool_call / tool_result, causality, logging) is unchanged, so the events table is a full audit of what each job did. `project_messages` renders wake-up roots so Claude can see past jobs.

**Wake-ups are silent by default.** A wake-up turn's final text is not sent anywhere. To reach the user, Claude calls a `notify_user(text)` tool (wake-up turns only). This avoids the `PASS`-sentinel failure mode — reflection leaked "…PASS" to the user twice and wrote 13 empty `assistant_message` events. Stage 7's argument against a send-message tool applies to replies, where speaking is the norm; for wake-ups, silence is the norm.

**Scheduler.** In-process, not python-telegram-bot's `JobQueue`, which belongs to the channel. A small asyncio loop over `SCHEDULE` in `bot/scheduler.py`; jobs and schedules are defined in code. A DB-backed schedule (so Claude or Matt could add routines over Telegram) is a possible later step.

### Jobs

| job | trigger | status |
|---|---|---|
| `arrived_home` | iPhone arrival webhook | live (10b) |
| `weekend_plan` | Saturdays 9:00 | live (10c) |
| `trim_context` | Mondays 3:00, skipped if no turns since last run | live (10c-2) |

- Later candidates: morning brief, overdue/stale tasks, symptom pattern review. Add them once the existing jobs have been lived with for a while.
- **Cost:** a wake-up costs about what a turn does (~3¢). Daily ≈ $1/month; hourly ≈ $20/month.
- **Considered and dropped:** nightly consolidation with a notes/facts table. Matt supplies context himself, and at a few turns per active day weekly trimming is enough.

### Health checks (absorbs Stage 6c)

Wake-ups are now too infrequent (weekly jobs plus arrivals) to serve as a liveness signal on their own. Plan:
- **Crash:** systemd `OnFailure=` runs a script that messages Matt via the Telegram API directly, independent of the bot.
- **Box or process down:** the bot pings an external heartbeat service (e.g. healthchecks.io free tier) every few minutes; the service alerts if pings stop. Pings carry no data.
- **Bot up but failing** (e.g. every turn errors): deferred until it actually happens; `error` events and the journal cover it for now.

### Steps

**10a.** ✓ Core: owns client, MCP session, turn queue + single worker. Telegram handler becomes an adapter that submits to the queue and implements `deliver`. No behavior change. Done as a module, `bot/core.py`, not a package — `bot/` modules import each other flat, and a package can come when the core grows. In-flight turns finish on shutdown; queued ones are dropped.
**10b.** ✓ `wakeup` turn root, `run_loop` generalization, `project_messages` rendering, `notify_user` tool. Done, plus a first trigger ahead of the scheduler: a webhook listener (`bot/webhooks.py`) behind Caddy at `https://pa.mattfae.com/hooks/<name>`, bearer-token auth, per-hook cooldown. First job: `arrived_home`, fired by an iPhone arrival automation. Wake-ups skipped the reflection step (since removed).
**10c.** ✓ Scheduler (`bot/scheduler.py`): schedules defined in code, in local time (`TIMEZONE`), ticking every 30s. A run missed while the bot was down is caught up if under 2 hours old, else skipped; the last run is read from `wakeup` events, so restarts neither repeat nor forget a run. First job: `weekend_plan`, Saturdays 9:00.
**10c-2.** ✓ Context trimming moved to a weekly `trim_context` job (Mondays 3:00), not nightly: at ~3–5 turns on an active day, history grows slowly enough that daily trimming saves nothing. No notes table: Matt supplies context himself, so the job only tidies tasks/shopping before trimming. Skipped without an API call if there were no turns since the last trim. Only the trim job sees event ids (`#N` labels on its projected history; ordinary turns don't, so Claude can't copy the labels into replies), so its `from_event_id` is no longer a guess. A bound mid-turn snaps back to the turn's start, since the API rejects a tool call split from its result. Hard cap: projections never include more than the last 30 turns (`_MAX_TURNS`).
**10d.** ✓ Reflection step removed (done with 10c-2). Of 58 reflections, 48 only moved the bound, ~3 did real tidy-up, and 3 of 7 follow-ups corrected confusion caused by the split itself (the main turn couldn't see `set_context_bound`). Now: one API call fewer per message and one shared prompt-cache prefix.
**10e.** ✓ Crash alert + external heartbeat. `bot/heartbeat.py` GETs `HEARTBEAT_URL` (healthchecks.io) every 5 minutes, skipping the ping if the core worker isn't running. `OnFailure=` runs `personal-assistant-alert.service` → `scripts/notify-failure.sh`, which messages Matt via the Telegram Bot API. `OnFailure=` only fires once systemd gives up, so the unit sets `StartLimitBurst=5` in `StartLimitIntervalSec=600`: a crash loop alerts, a single crash restarts silently (the heartbeat misses at most one ping).
**10f.** More jobs, as wanted.

## Stage 9 (Future) — Security Hardening

Secret management, DB access patterns, file permissions, rate limiting.

## Deferred Work

### Unit Tests
87 tests cover projection, message splitting, the agent loop, the core, webhooks, the scheduler, the heartbeat, history trimming, `query`, logging, the `db/` CRUD layer and the MCP client against the real tools server (`uv run pytest`).

### Backups (considered, skipped 2026-10-29)
No backups of `personal_assistant.db`, on purpose: losing the data would be acceptable, and every option adds an inconvenience (nightly file, new account) or a privacy cost (health data stored off the box). Cheapest if wanted later: a nightly `sqlite3 .backup` (not a plain copy, since the DB uses WAL), sent to Matt via Telegram `sendDocument`, optionally encrypted, with a healthchecks.io check.

### Rate Limiting
Protect against rapid Telegram message bursts exhausting API credits:
- Turns already run one at a time (the core's turn queue). What's missing is protection against *volume*: a burst of N messages still queues N full turns.
- Options: coalesce messages that arrive while a turn is running into the next turn (the turn queue is the natural place), or cap turns per time window.
- Backpressure signal to user if a turn is already running
