# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database. Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response via Telegram.

## Core Architecture Decisions

### Foundational
- **Model:** `claude-haiku-4-5-20251001`
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

A **turn** is one cycle: `user_message_in → [api_call / tool_call / tool_result]* → assistant_message_out → optional post-turn cleanup`.

- Every event in a turn shares a single `turn_id`.
- Post-turn cleanup runs synchronously inside the same turn, before the outgoing message is sent.

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

Start minimal. Add tools only when driven by concrete need. Target surface is **`add_fact` only** — reads come for free via events projection. Read/search tools are added only when context can no longer fit the relevant events.

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

## Completed: Stage 5 — Telegram Bot ✓

Each input channel gets its own handler. No shared channel abstraction — each transport is different enough that a common interface would be leaky. Handlers call `run_loop(user_message)` directly; no channel identity leaks into the agent.

**5a.** `bot/telegram_handler.py` — `python-telegram-bot` with long polling, sender allowlist, wire to `run_loop`. Send a "working…" ack at turn start and record it as an event. ✓

`bot/main.py` promoted to true entry point; test harness moved to `bot/harness.py`. ✓

DB migrations added to drop legacy `conversation_id` and `correlation_id` columns from the live DB. ✓

## Current Stage: Stage 6 — Monitoring

**6a.** `systemd` service — unit file at `scripts/personal-assistant.service`, installed and running. ✓

**6b.** Structured logging — consistent log levels and format across all modules.

**6c.** Basic health checks.

## Upcoming Stages

**Stage 7:** Security hardening — secret management, DB access patterns, file permissions, rate limiting.
