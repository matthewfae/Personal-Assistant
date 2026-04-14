# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database (personal info, projects, lists, calendar, etc.). Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response to the user via Telegram.

## Documentation Map

This project is documented at three levels of abstraction. This file is the highest level — strategy, architectural decisions, and stages. Lower-level docs describe the current implementation in detail.

- **`PROJECT_PLAN.md`** (this file) — goals, architectural decisions, stages, deferred work.
- **`docs/DESIGN.md`** — middle-level description of the current implementation: layout, layering rules, schema, agent flow. Should be kept in sync with the code.
- **`docs/EVENTS.md`** — `v:1` payload schemas for all event types, causality rules, reserved type names, byte-stable projection and cache health notes.
- **`README.md`** — short top-of-repo front door for anyone (or any future Claude) opening the project cold.

If new docs are added, list them here — this map is the authoritative index.

## Core Architecture Decisions

### Foundational
- **Model:** `claude-haiku-4-5-20251001` as primary.
- **Prompt caching:** Applied to system prompt and tool list.
- **Tool architecture:** MCP server/client split. Tools live in `tools/server.py`; the bot is an MCP client.
- **DB access:** Curated tool functions only (e.g. `add_fact`), not raw SQL.
- **Layering rule:** `db/` has no knowledge of MCP or Claude. `tools/server.py` knows about MCP and calls down into `db/`. The agent layer knows about Claude and calls down into the MCP client. Each layer imports only downward. *Note: tool execution — including the SQL a tool performs — runs inside the MCP server process. An earlier phrasing of this rule said "`tools/server.py` has no SQL"; that was a misread of MCP, which hosts tool execution rather than acting as a passive tool registry. SQL executes in the server process via `db/` imports, and that is correct. See Event Write Ownership below.*
- **Error handling:** Low-level code raises naturally; the agent layer catches and forwards errors to the user via Telegram.

### Events-Driven Architecture

The app is modeled as a stream of events. Every meaningful thing that happens — an incoming user message, an outgoing assistant message, an API call to Claude, a tool invocation, a tool result — is recorded as an event in a single append-only `events` table.

**Events are the source of truth.**

- **Append-only.** Events are immutable once written. No `UPDATE`, no `DELETE`, ever. Corrections are expressed as new compensating events. Enforced at the DB level (trigger).
- **Per-type payload versioning.** Every event payload is a JSON blob containing a `v: <int>` field. The `v` is versioned *per event type*, not globally. When an event type's shape changes, bump its `v`. Consumers must check `v` before reading the payload, and fail loudly on unknown versions.
- **Full payload captured.** Log the complete arguments and results of tool calls, the complete user message, the complete assistant response. Storage is cheap; reconstruction after the fact is not.
- **`messages` is a projection of events.** The `messages` array sent to the Anthropic API on each turn is *derived from* the events table by a projection function, not stored as a separate in-memory list. The projection is a policy decision that can evolve (simple chronological → pruned → summarized) without changing the underlying ledger.
- **Facts, notes, and other structured state are state projections.** They are kept in sync by the same tool call that writes the corresponding event. In principle they should be rebuildable from the event log alone. If structured state contains information not derivable from events, the ledger is incomplete and that's a bug.
- **Mutation log stays, at a different layer.** `mutation_log` records row-level physical writes (what changed in the DB). `events` records semantic agent activity (what happened at the tool/message level). Keeping them separate preserves clean layering: events answer "what did the agent do and why?"; mutation_log answers "what did the row look like before?".

### Turn Model

A **turn** is one self-contained cycle: `user_message_in → [api_call / tool_call / tool_result]* → assistant_message_out → optional post-turn cleanup`.

- Every event in a turn shares a single `turn_id`.
- Turns are grouped into conversations via `conversation_id`. For now a conversation can be coarse (e.g. time-bounded or single-always); the column exists from day one so we don't have to retrofit it.
- Wall-clock time between events inside a turn is irrelevant to the architecture. Responsiveness is not a constraint (see below).
- The turn formally ends after post-turn cleanup completes, not when the outgoing message is sent. Post-turn cleanup (e.g. context-retention decisions, summarization) is part of the turn, runs synchronously, and emits its own events under the same `turn_id`.

### Causality: `parent_event_id` and `correlation_id`

Three distinct concepts — keep them separate:

- **`parent_event_id`** — strict 1:1 causal lineage. Points to the event that directly caused this one. Answers "what caused this?" Null for turn roots.
- **`turn_id`** — turn grouping. All events in one turn share a `turn_id`. Answers "what turn does this belong to?" Maps to an OTel span.
- **`correlation_id`** — distributed trace ID. Currently aliased to `turn_id` — populated with the same value. In the current single-bot single-MCP-server architecture, the two concepts are identical. The column is kept (no schema cost, no migration later) but no separate propagation machinery is needed. Differentiate when/if parallel sub-agents or background workers arrive. Maps to an OTel trace ID.

`parent_event_id` rules per event type:
- `user_message` — null. Turn root.
- `api_call` — parent is the event that provided the last new input: `user_message` for the first call in a turn; `tool_result` for subsequent single-tool calls; the common `api_call` ancestor for the call following parallel tool results (that `api_call` is the true causal parent — it spawned the tools whose results triggered this follow-up).
- `tool_call` — parent is the `api_call` whose response requested this tool.
- `tool_result` — parent is the `tool_call` it came from.
- `assistant_message` — parent is the `api_call` that produced the final text.
- **Parallel tool calls:** siblings sharing the same `api_call` parent. The next `api_call` after all their results are ready points at the common `api_call` ancestor, not any individual `tool_result`. This keeps the causality tree clean.

See `docs/EVENTS.md` for the full schema including `correlation_id` column definition.

### Event Write Ownership

Which process writes which events, and why. Resolves scratch-doc Q3, Q5, and Q6.

- **Agent process writes:** `user_message`, `api_call` (including error variants), `tool_call`, `assistant_message`, and post-turn cleanup events. These are all things the agent observes or emits directly; it doesn't need the MCP server's cooperation to log them.
- **MCP server process writes:** `tool_result`, bundled with the `mutation_log` row and any state-table rows (e.g. `facts`) inside a single `BEGIN IMMEDIATE` transaction. The server is where the tool actually executes, so it is the only process that can atomically bind "the tool produced this result" to "the DB state now looks like this." Writing `tool_result` from the agent would split the state-change and the event across two processes and lose atomicity.
- **Shared DB file.** Both the agent process and the MCP server process open connections to the same SQLite file. WAL mode (already enabled via `db/connection.py`) makes multi-process access safe.
- **Metadata passthrough via MCP `meta`.** The agent writes the `tool_call` event *before* dispatching, then passes `turn_id`, `conversation_id`, and the freshly-written `tool_call` event id to the server via the `meta` parameter on `call_tool(...)` (MCP SDK 1.27.0: `call_tool(..., meta={...})`). `meta` is MCP's reserved protocol-level metadata channel — distinct from the tool's semantic `input` — so Claude never sees these bookkeeping fields and they don't appear in the tool schema. The server reads them off the incoming request and stamps them onto the `tool_result` event it writes.
- **Crash semantics.** If the server crashes mid-transaction, SQLite rolls back and no state row, `mutation_log` row, or `tool_result` event exists — but the agent has already written a `tool_call` event. A `tool_call` without a matching `tool_result` in the events table therefore means "tool crashed or timed out," and the projection/replay layer must tolerate that shape.

### Responsiveness

**Not a constraint.** The user is the developer and is willing to wait for the full agent loop to complete. Implications:

- Everything in a turn runs synchronously — no background job queues, no async cleanup workers.
- Post-turn cleanup can run *before* the outgoing Telegram message is sent, inside the same handler. Simpler than firing cleanup after.
- A lightweight "working…" acknowledgment on turn start is a desired UX affordance — see Deferred Work below.

### Tool Surface Philosophy

Start minimal. The tool surface should grow only in response to real pressure (either Claude failing to accomplish something, or context bloat forcing a retrieval tool). Speculative tools for hypothetical future needs are out.

The events-driven rebuild (Stage 3 below) strips the surface back to **a single tool: `add_fact`**. Reads come for free at first via context injection — recent events appear in the projected `messages` list, so Claude "remembers" without needing a read tool. Read/search tools are added only when context can no longer fit the relevant events.

## Development Stages

### Stage 1: Python Stack & Environment ✓
- Python 3.11+, `uv` for package management.
- Core packages: `anthropic`, `python-telegram-bot`, `python-dotenv`, `mcp`.
- `sqlite3` from stdlib.
- `.env` for secrets.
- Project structure: `bot/`, `tools/`, `db/`, `tests/`.

### Stage 2: Agent Loop with MCP Tool Use ✓
- MCP server (`tools/server.py`) with tool dispatch.
- MCP client (`bot/mcp_client.py`) that spawns the server, performs the initialization handshake, and fetches the tool list.
- Agent loop (`bot/agent.py`) that drives the Claude conversation and dispatches tool calls through the MCP client.
- Prompt caching on system prompt and tool list.
- System prompt in `prompts/system.md` with dynamic context injected via `PromptBuilder`.
- End-to-end flow validated: user message → Claude → tool_use → MCP client → MCP server → tool_result → Claude → final response.

### Stage 2.5: Exploratory DB + Tool Surface ✓ (superseded in scope)
First-pass SQLite integration that proved the end-to-end pipeline from Claude through the MCP server into the database and back:

- Tables: `facts` (category/key/value, unique per category+key), `notes` (with FTS5), `mutation_log`.
- Eight tools: `add_fact`, `get_fact`, `search_facts`, `list_facts`, `add_note`, `get_note`, `search_notes`, `list_notes`.
- Conversation history held as an in-memory list passed through `run_loop`.

**Superseded by Stage 3.** Retained from this stage: the `facts` table, `mutation_log`, the `db/` layering pattern, and `add_fact`. Not retained:

- `notes` / `notes_fts` and `db/notes.py` — removed as Stage 3 groundwork. If note-taking returns, it will be rebuilt on the events foundation rather than carried forward from this exploratory schema.
- The other tools on the MCP surface (`get_fact`, `search_facts`, `list_facts`, and the three notes tools) — to be stripped in Stage 3d.
- The in-memory conversation history — replaced by the events projection in Stage 3c.

### Stage 3: Events-Driven Rebuild ✓ (complete as of 2026-04-14)

Re-grounded the architecture on the events table and a minimal tool surface. No new features; this was structural work that unlocks everything downstream.

**3a. Events schema. ✓** The `events` table is in `db/connection.py` (`_SCHEMA`) with all seven columns, three indexes (`idx_events_conv_ts`, `idx_events_type`, `idx_events_parent`), and two append-only triggers (`events_no_update`, `events_no_delete`). `db/events.py` provides `append`, `get_tool_result_event_id`, and `walk_causality`. Event type taxonomy and `v:1` payload shapes are defined in `docs/EVENTS.md`.

**3b. Turn lifecycle. ✓** `turn_id` and `conversation_id` generated and threaded. `user_message` event written as the turn root. `conversation_id` policy: one per process start (single UUID generated in `bot/main.py` before the loop). `init_db()` called at startup in both `bot/main.py` and `tools/server.py`. MCP server lifecycle: persistent, spawned once at bot start via the `mcp_client()` async context manager.

**3c. Messages-as-projection. ✓** `build_messages(conn, conversation_id)` in `bot/agent.py` replaces the in-memory history list. Projection runs once at turn start from the `events` table; the current user message and in-turn responses are appended to the local list as before. Handles both orphan shapes: `tool_call` with no `tool_result` (MCP crash — skip tool exchange) and `user_message` with no `assistant_message` (agent crash — skip turn entirely). Chronological, full tool exchanges included. Thinking blocks: disabled (deliberate, default API behavior).

**3d. Minimal tool rebuild. ✓** Single tool: `add_fact`. The MCP server owns the atomic triple-write: `facts` row + `mutation_log` row + `tool_result` event inside one `BEGIN IMMEDIATE` transaction. Agent writes `tool_call` event before dispatching and passes `{turn_id, conversation_id, tool_call_event_id, tool_use_id}` to the server via `call_tool(..., meta={...})`. Error path: rollback, then write `tool_result` event with `is_error: True` on a fresh connection. Old tools (`get_fact`, `search_facts`, `list_facts`, all notes tools) and `db/notes.py` have been removed.

**3e. Causality wiring. ✓** All four causality locals tracked in `run_loop`: `user_event_id`, `last_input_event_id`, `current_api_call_event_id`, `spawning_api_call_event_id`. `api_call` payload is complete (usage, caching tokens, `model_requested`/`model_used`, `request_id`, `started_at`/`finished_at`, `status`, `error`). Parallel-tool causality comment present. `walk_causality` available in `db/events.py` for debugging the causality tree.

**Remaining gap (not blocking):** Parallel tool call path is implemented and commented but has not been exercised with a concrete test prompt. See recommended next steps in `docs/STAGE3_IMPL_PLAN.md`.

**Stage 3 prerequisites (do before other Stage 3 work):**

- [x] **Switch to `AsyncAnthropic`.** `python-telegram-bot` v20+ is fully async; a sync Anthropic client inside an async handler blocks the event loop. Migrate `bot/agent.py` to use `AsyncAnthropic` before more code lands on top of the sync client.
- [x] **Add `PRAGMA busy_timeout = 5000` in `db/connection.py`.** SQLite serializes writes; without a busy timeout the second concurrent writer gets an immediate `SQLITE_BUSY` error instead of waiting. Both the agent process and the MCP server process write to the same DB file, so this is a prerequisite for Stage 3 correctness. Add the pragma to the connection setup in `db/connection.py`.

**DB migration note:** Stage 3 is a hard reset — drop and recreate the schema. No migration tooling needed.

**Stage 3 — Q4 resolution implementation checklist.** The architectural changes above (Layering rule correction + Event Write Ownership) imply concrete work items that cross-cut 3a–3e:

- [x] **`db/events.py`** — new module. `append(conn, type, payload, turn_id, conversation_id, parent_event_id) -> event_id`. Takes an open connection so callers can bundle the append into their own transaction. Used by both the agent process and the MCP server process.
- [x] **`tools/server.py`** — in the `add_fact` dispatch path, open a connection, `BEGIN IMMEDIATE`, write `tool_result` event + `mutation_log` row + `facts` row, commit. Read `turn_id`, `conversation_id`, and `tool_call_event_id` off the incoming request's `meta` field.
- [x] **`bot/agent.py`** — before each `mcp.call_tool`, write the `tool_call` event and capture its id. Pass `{turn_id, conversation_id, tool_call_event_id}` to the dispatch via `meta`.
- [x] **`bot/mcp_client.py`** — pass `meta={...}` to `call_tool(...)`. MCP SDK 1.27.0 supports `call_tool(..., meta={...})` — no blocker. Ensure the wrapper surfaces this parameter.
- [x] **Confirm WAL mode on the server-side connection.** The MCP server opens its own connection with `isolation_level=None` and applies WAL, foreign_keys, and busy_timeout pragmas manually (matching `get_db()`'s pragma order). WAL is confirmed on both sides.
- [x] **`docs/DESIGN.md`** — updated to reflect Stage 3: events table, event-write-ownership split, messages-as-projection, minimal `add_fact` tool surface, and corrected layering rule. Updated 2026-04-14.
- [x] **`docs/EVENTS.md`** (new) — `v:1` payload schemas written. Fields included: `request_id`, full `usage` envelope (including `cache_read_input_tokens` + `cache_creation_input_tokens`), `model_requested` + `model_used`, `beta_headers`, `cost_usd_millicents` + `price_table_version`, `correlation_id` as a table column. Cache health SLO, reserved event type names, and causality rules are all documented there. (`response_raw` and `idempotency_key` deferred — see Deliberately Simplified section.)

### Stage 4: Tool Surface Growth (as needed)
Add read / search tools (`search_facts`, notes, etc.) only when driven by a concrete need: Claude unable to recall something, context budget pressure, or a specific feature. Each addition is evaluated against the "does it earn its place?" bar.

### Stage 5: Telegram Bot
- `python-telegram-bot` with long polling.
- Allowlist check on sender ID on every incoming message.
- Route incoming messages through the agent loop.
- Forward responses and errors back via Telegram.

### Stage 6: Monitoring Service
- `systemd` service on the Linux host.
- Structured logging to a file.
- Basic health checks.

### Stage 7: Security Review & Hardening
- Audit secret management (API keys, Telegram bot token).
- Review DB access patterns and mutation log completeness.
- Test edge cases: malformed input, API outages, DB corruption.
- Lock down file permissions on DB and `.env`.
- Review auth allowlist.
- Rate limiting and cost monitoring.

## Deliberately Simplified / Deferred Complexity

Things that were explicitly considered and consciously simplified out — not missing features, but active design choices. Re-open only when the stated trigger condition applies.

- **`response_raw` / byte-stable serialization** — designed to preserve byte-exact assistant content for cache hit and thinking-block signature fidelity. Both purposes are now moot: thinking is disabled and cache optimization is out of scope. Re-evaluate if thinking blocks or aggressive cache optimization become priorities.
- **`idempotency_key` on tool events** — a UUID to detect crash-between-commit-and-ack duplicates on retry. Real concern but over-engineering for a personal project with no retry machinery. Add back if crash-mid-transaction recovery becomes a real operational concern.
- **`correlation_id` propagation** — currently aliased to `turn_id` (populated with the same value). No separate propagation via `meta` needed. Differentiate when/if parallel sub-agents or background workers arrive and the concepts genuinely diverge.
- **`prompt_version` / `tool_set_version` events** — deployment markers for correlating cache hit rate drops with system prompt or tool set changes. Useful in production; reserved names exist to prevent collision. Implement when there's a real debugging need.
- **Context compaction (`context_compacted` event)** — Anthropic server-side compaction won't trigger in normal personal-assistant use (context window won't fill). The event type name is reserved. No design work needed before shipping.
- **Thinking blocks** — explicitly disabled. Default Anthropic API behavior is off; this is a deliberate choice, not an oversight. Revisit if extended reasoning becomes useful for this use case.
- **Cache TTL optimization** — `ephemeral` cache headers used as-is. Small personal project; cost impact is negligible. No further optimization warranted.

## Planned Features / Deferred Work

Things we want but have explicitly deferred. Each item should name *what* and *why deferred*.

- **Telegram "working…" ack on turn start.** When an incoming user message is received and a turn begins, immediately send a lightweight Telegram reply (e.g. `⏳ working…`) so the user knows the loop has started and the message wasn't dropped. Should be recorded as an event (`ack_sent` or similar) so the ledger remains complete. *Deferred until Stage 5 (Telegram integration) — no transport yet to ack on.*
