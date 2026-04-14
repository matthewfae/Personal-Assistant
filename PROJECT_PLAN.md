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
- Turns are grouped into conversations via `conversation_id`.
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

## Current Stage: Stage 3 — Events-Driven Rebuild

**3a. Events schema.** Create the `events` table: `id`, `timestamp`, `turn_id`, `conversation_id`, `type`, `parent_event_id`, `payload` (JSON). Indexes on `(conversation_id, timestamp)`, `type`, `parent_event_id`. Append-only enforcement triggers. Event type taxonomy: `user_message`, `api_call`, `tool_call`, `tool_result`, `assistant_message`. Payload shapes at `v: 1`.

**3b. Turn lifecycle.** `turn_id` and `conversation_id` generation, threaded through the agent loop. Initial policy: one conversation per process start.

**3c. Messages-as-projection.** Project events → `messages` array at turn start. Initial policy: chronological, current conversation, all message-like event types.

**3d. Minimal tool rebuild.** Remove existing tools. Reintroduce `add_fact` as the only tool, writing event row + mutation_log + facts row in one transaction.

**3e. Causality wiring.** `parent_event_id` set correctly for every event type. Add a debug query that walks a turn's causality tree.

## Upcoming Stages

**Stage 4:** Add read/search tools only when driven by concrete need.

**Stage 5:** Telegram bot — `python-telegram-bot` with long polling, sender allowlist, route messages through agent loop.

**Stage 6:** Monitoring — `systemd` service, structured logging, basic health checks.

**Stage 7:** Security hardening — secret management, DB access patterns, file permissions, rate limiting.

## Deferred Work

- **Telegram "working…" ack on turn start.** Send a lightweight reply when a turn begins; record as an event. *Deferred until Stage 5.*
