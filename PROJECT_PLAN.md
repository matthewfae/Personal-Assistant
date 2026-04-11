# Personal Assistant Project Plan

## Overview
Python Telegram bot that routes user messages through the Claude API with access to a local SQLite context database (personal info, projects, lists, calendar, etc.). Claude uses tool calls to read/write the DB in a multi-step agent loop, then returns a final response to the user via Telegram.

## Documentation Map

This project is documented at three levels of abstraction. This file is the highest level — strategy, architectural decisions, and stages. Lower-level docs describe the current implementation in detail.

- **`PROJECT_PLAN.md`** (this file) — goals, architectural decisions, stages, deferred work.
- **`docs/DESIGN.md`** — middle-level description of the current implementation: layout, layering rules, schema, agent flow. Should be kept in sync with the code.
- **`README.md`** — short top-of-repo front door for anyone (or any future Claude) opening the project cold.

If new docs are added, list them here — this map is the authoritative index.

## Core Architecture Decisions

### Foundational
- **Model:** `claude-haiku-4-5-20251001` as primary.
- **Prompt caching:** Applied to system prompt and tool list.
- **Tool architecture:** MCP server/client split. Tools live in `tools/server.py`; the bot is an MCP client.
- **DB access:** Curated tool functions only (e.g. `add_fact`), not raw SQL.
- **Layering rule:** `tools/server.py` has no SQL; `db/` has no knowledge of MCP or Claude. Each layer only imports downward.
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

### Causality: `parent_event_id`

Every event (except turn roots) carries a `parent_event_id` pointing to the event that directly caused it. This makes causality a tree — one tree per turn — and lets us reconstruct "what did the agent see at the moment it made this decision?" without relying on timestamp ordering.

Rules:
- `user_message` — no parent. Turn root.
- `api_call` — parent is the event that provided the last new input. First call in a turn: the `user_message`. Later calls: the `tool_result` (or, if multiple parallel tool_results, the shared `api_call` ancestor — see below).
- `tool_call` — parent is the `api_call` whose response requested this tool.
- `tool_result` — parent is the `tool_call` it came from.
- `assistant_message` — parent is the `api_call` that produced the final text.
- **Parallel tool calls:** when a single `api_call` requests multiple tools, they are siblings with the same `api_call` parent. The next `api_call` (triggered by all results being ready) points its parent at the common `api_call` ancestor, not any individual `tool_result`. This preserves a clean tree.

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

**Superseded by Stage 3.** The DB code, schema, and `mutation_log` from this stage are retained. The eight-tool surface is not: it will be stripped back and rebuilt on the events foundation.

### Stage 3: Events-Driven Rebuild (current focus)

Re-ground the architecture on the events table and a minimal tool surface. No new features; this is structural work that unlocks everything downstream.

**3a. Events schema.** Create the `events` table with `id`, `timestamp`, `turn_id`, `conversation_id`, `type`, `parent_event_id`, `payload` (JSON). Add indexes for `(conversation_id, timestamp)`, `type`, and `parent_event_id`. Add `BEFORE UPDATE` and `BEFORE DELETE` triggers that raise to enforce append-only. Define and document the initial event type taxonomy (`user_message`, `api_call`, `tool_call`, `tool_result`, `assistant_message`, plus post-turn cleanup types as they arise) and the payload shape for each at `v: 1`.

**3b. Turn lifecycle.** Implement `turn_id` and `conversation_id` generation. Thread them through the agent loop so every event emitted during a turn carries them correctly. Decide the simplest viable `conversation_id` policy (likely: one conversation per run, or time-bucketed).

**3c. Messages-as-projection.** Replace the in-memory history list with a function that queries the events table and builds the `messages` array for the next Claude call. Start with the simplest possible projection: chronological, current conversation, all message-like event types. This is the point where Stage 2b's former "context management" goal lives — it becomes "improve the projection policy" rather than a separate subsystem.

**3d. Minimal tool rebuild.** Remove the existing eight tools. Reintroduce `add_fact` as the single tool, now wired so that its invocation writes: (1) the event row, (2) the mutation_log row, and (3) the `facts` table row, all within one transaction. Verify the full loop end-to-end: user message → events projection → Claude → `add_fact` tool call → event + mutation + state all written → next projection includes the new event → Claude's follow-up "remembers."

**3e. Causality wiring.** Ensure `parent_event_id` is set correctly for every event type per the rules above. Add a small debug view or query that walks a turn's causality tree so we can sanity-check it.

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

## Planned Features / Deferred Work

Things we want but have explicitly deferred. Each item should name *what* and *why deferred*.

- **Telegram "working…" ack on turn start.** When an incoming user message is received and a turn begins, immediately send a lightweight Telegram reply (e.g. `⏳ working…`) so the user knows the loop has started and the message wasn't dropped. Should be recorded as an event (`ack_sent` or similar) so the ledger remains complete. *Deferred until Stage 5 (Telegram integration) — no transport yet to ack on.*
