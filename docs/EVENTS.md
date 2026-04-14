# Event Payload Schemas — v:1

This document defines the `v:1` payload for each event type written to the `events` table. Every payload is a JSON blob stored in the `payload` column. The `v` field is versioned per event type, not globally.

**Consumer rules:**
- Fail loudly on an unknown `v` value for a known event type.
- Warn and skip on an unknown event type entirely (tolerates rolling deploys where writer is ahead of reader).

---

## Common envelope (events table columns — not in payload)

These are columns on the `events` table row, not fields inside the JSON payload.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-increment primary key |
| `timestamp` | TEXT | ISO-8601 UTC wall-clock time the event was written |
| `turn_id` | TEXT | UUID grouping all events in one turn |
| `conversation_id` | TEXT | UUID grouping all turns in one conversation |
| `correlation_id` | TEXT | UUID for end-to-end distributed tracing. Currently aliased to `turn_id` — populated with the same value. In the current single-bot single-MCP-server architecture, the two concepts are identical. Column kept so the semantics can diverge (e.g. parallel sub-agents) without a schema migration. Aligns with OTel's trace ID concept. |
| `type` | TEXT | Event type name (see below) |
| `parent_event_id` | INTEGER\|NULL | Causal parent event id. See Causality rules below. Null for turn roots. |

---

## Causality rules

`parent_event_id` tracks **strict 1:1 causal lineage** — what directly caused this event. `turn_id` handles **grouping** — all events within a turn share a `turn_id`. These two concerns are kept separate; don't conflate them.

`correlation_id` handles **cross-process tracing** — currently aliased to `turn_id`. No separate propagation machinery needed in the current single-bot single-MCP-server setup. See the Deliberately Simplified section in `PROJECT_PLAN.md`.

Per-type rules:

- **`user_message`** — `null`. Turn root.
- **`api_call`** — parent is the event that provided the last new input: the `user_message` for the first call in a turn; the `tool_result` for subsequent single-tool calls; the common `api_call` ancestor for the call that follows parallel tool results (that earlier `api_call` is the true causal parent — it spawned the tools whose results triggered this follow-up).
- **`tool_call`** — parent is the `api_call` whose response requested it.
- **`tool_result`** — parent is the `tool_call` it came from.
- **`assistant_message`** — parent is the `api_call` that produced the final text.

**Parallel tool calls:** siblings sharing the same `api_call` parent. The next `api_call` after all their results are ready points at the common `api_call` ancestor, not any individual `tool_result`. This keeps parent_event_id a clean tree. Aligns with the OTel GenAI span model: `turn_id` → span, `parent_event_id` chain → trace parent.

---

## Event type payloads

### `user_message`

```json
{
  "v": 1,
  "text": "string",
  "source": "string"
}
```

| Field | Notes |
|---|---|
| `text` | Full text of the incoming message |
| `source` | Origin: `"telegram"` \| `"test_harness"` |

---

### `api_call`

A single event written after the API call completes (success or error). No separate `api_error` type — errors are indicated by `status: "error"` with the `error` field populated. This matches the OTel GenAI span model (one span per API call, with outcome in a status field) and keeps the payload versioning surface minimal.

```json
{
  "v": 1,
  "status": "success | error",
  "model_requested": "string",
  "model_used": "string",
  "request_id": "string | null",
  "beta_headers": ["string"],
  "stop_reason": "string | null",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0
  },
  "cost_usd_millicents": 0,
  "price_table_version": "string | null",
  "error": "string | null",
  "started_at": "string",
  "finished_at": "string"
}
```

| Field | Notes |
|---|---|
| `status` | `"success"` or `"error"` |
| `model_requested` | Model sent in the request (e.g. `"claude-haiku-4-5-20251001"`) |
| `model_used` | Model from the response body — may differ from `model_requested` if an alias was used |
| `request_id` | Value of the `X-Request-Id` response header. Anthropic's support handle for this call. |
| `beta_headers` | List of `anthropic-beta` values sent with the request. Part of the reproducibility envelope. |
| `stop_reason` | `end_turn`, `tool_use`, etc. `null` on error. |
| `usage` | Full usage envelope from the response. `cache_read_input_tokens` is the cache health SLO metric. |
| `cost_usd_millicents` | Computed cost in millicents (1/1000 of a cent) at write time. `null` if price table unavailable. |
| `price_table_version` | Version identifier of the price table used to compute `cost_usd_millicents`. Required for deterministic historical recomputation when prices change. |
| `error` | Error type and message string on `status: "error"`. `null` on success. |
| `started_at` | ISO-8601 UTC timestamp immediately before the API call was made |
| `finished_at` | ISO-8601 UTC timestamp immediately after the response (or error) was received |

Deferred: `response_raw` (verbatim response body for byte-stable projection) is not included. It was designed to preserve cache hits and thinking-block signatures; both are moot — thinking is explicitly disabled and cache optimization is out of scope. Re-evaluate if thinking blocks or aggressive cache optimization become priorities.

**Cache health SLO.** `usage.cache_read_input_tokens` is the smoke detector. Log it on every turn. A value of `0` on the second turn onward means a silent cache invalidator has crept in. Add a trivial check in `scripts/` that yells if it's zero.

---

### `tool_call`

Written by the agent process **before** dispatching to the MCP server.

```json
{
  "v": 1,
  "tool_name": "string",
  "tool_use_id": "string",
  "input": {}
}
```

| Field | Notes |
|---|---|
| `tool_name` | Name of the tool as it appears in the tool schema |
| `tool_use_id` | Exactly as returned by the API. Wire-format correlation key — do not substitute. |
| `input` | Verbatim tool input dict from the API response |

Deferred: `idempotency_key` removed. A UUID for crash-between-commit-and-ack duplicate detection is the right long-term pattern but over-engineering for now. Add back if crash-mid-transaction recovery becomes a real operational concern.

---

### `tool_result`

Written by the **MCP server process** inside the same `BEGIN IMMEDIATE` transaction as the `mutation_log` row and any state-table rows (e.g. `facts`). This is the only event type not written by the agent process.

```json
{
  "v": 1,
  "tool_use_id": "string",
  "tool_call_event_id": 0,
  "is_error": false,
  "content": "string"
}
```

| Field | Notes |
|---|---|
| `tool_use_id` | Matches `tool_call.tool_use_id` — used when building the `tool_result` block for the next API call |
| `tool_call_event_id` | FK to the `tool_call` event row. Received from the agent via `meta`. |
| `is_error` | `true` if the tool raised. Claude receives the error text as the result and can self-correct. |
| `content` | The result string passed back to Claude |

Deferred: `idempotency_key` removed. Same rationale as `tool_call` — add back if crash-mid-transaction recovery becomes a real operational concern.

---

### `assistant_message`

```json
{
  "v": 1,
  "text": "string",
  "api_call_event_id": 0
}
```

| Field | Notes |
|---|---|
| `text` | Final text response sent to the user |
| `api_call_event_id` | FK to the `api_call` that produced this response |

---

## Reserved event types

Not yet implemented. Names reserved to prevent accidental reuse. Sources: OTel GenAI semantic conventions and Anthropic API patterns.

| Type | Description |
|---|---|
| `context_compacted` | Anthropic server-side compaction ran (`compact-2026-01-12` beta). Reserved; won't trigger in normal personal-assistant use (context window won't fill). No design work needed before shipping. |
| `conversation_forked` | Conversation branched (e.g. a retry from a prior point). |
| `eval_run` | An automated evaluation pass was executed against a turn or conversation. |
| `policy_check` | A safety or policy gate was evaluated (pass or block). |
| `prompt_version` | Deployment marker: a new system prompt was loaded. Correlate with cache hit rate drops. |
| `tool_set_version` | Deployment marker: the MCP tool set changed. Correlate with cache hit rate drops. |
| `ack_sent` | Lightweight Telegram "working…" acknowledgment was sent to the user. See Deferred Work in `PROJECT_PLAN.md`. |
