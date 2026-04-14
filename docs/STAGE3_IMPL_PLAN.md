# Stage 3 Implementation Plan

Generated from plan-agent review of all source files and PROJECT_PLAN.md.

---

## Summary

*Completion audit performed 2026-04-14 against all source files.*

### What's done

All seven chunks are fully implemented. Stage 3 is complete as specified:

- **P1** — `AsyncAnthropic` migration done in `bot/agent.py` and `bot/main.py`.
- **P2** — `PRAGMA busy_timeout = 5000` added to `db/connection.py`.
- **3a** — `events` table, three indexes, two append-only triggers, and `db/events.py` (`append`, `get_tool_result_event_id`, `walk_causality`) all present.
- **3b** — `turn_id`/`conversation_id` generated and threaded; `user_message` event written on entry; `init_db()` called in `bot/main.py` (Gap 10 from the gaps list is resolved).
- **3c** — `build_messages` projection in `bot/agent.py` with turn grouping, complete-pair filtering, and both orphan-tolerance shapes (no `tool_result`, no `assistant_message`).
- **3d** — `add_fact` is the sole tool; server uses `isolation_level=None` + `BEGIN IMMEDIATE` for the atomic triple-write (`facts` row + `mutation_log` row + `tool_result` event); `meta` passthrough wired end-to-end; error path writes `tool_result` event on a fresh connection.
- **3e** — All causality locals tracked (`user_event_id`, `last_input_event_id`, `current_api_call_event_id`, `spawning_api_call_event_id`); `api_call` payload complete (usage, caching tokens, `model_requested`/`model_used`, `request_id`, `started_at`/`finished_at`, `status`, `error`); parallel-tool comment present.

All six gap items that were actionable in Stage 3 (gaps 3, 4, 5, 6, 8, 10) are resolved in the code.

### What's not yet done

- **Stage 4–7** — all deferred by design (tool surface growth, Telegram, monitoring, security hardening).
- **Gap 7 (parallel tool verification)** — no test harness prompt forces two simultaneous `add_fact` calls. The agent-side parallel-tool path is implemented and commented, but has not been exercised with a concrete test.
- **`source` field in `user_message` payload** — currently hardcoded to `"test_harness"` in `bot/agent.py`. This will need updating when Stage 5 (Telegram) lands.
- **`prompt_version` / `tool_set_version` events** — deliberately deferred per the plan; reserved names exist. Design decision settled (2026-04-14): write one event of each type at session start in `bot/main.py`. Payload: a truncated SHA-256 content hash (12 hex chars) of the artifact — bytes of `prompts/system.md` for `prompt_version`, canonical `json.dumps` of sorted tool schemas for `tool_set_version` — plus the current `git rev-parse HEAD` SHA as a human-navigable anchor. Skip writing if hash matches the most recent event of that type (no change = no event). Semantic versioning ruled out (requires manual discipline that gets skipped in practice).

### Recommended next steps

1. Add a manual smoke-test that forces parallel tool calls (e.g. `"Remember I like coffee and my name is Matt"`) to exercise the `spawning_api_call_event_id` path and verify the causality tree with `walk_causality`.
2. Begin Stage 4 only when driven by a real need (Claude failing to recall something, or context pressure) — the current `add_fact`-only surface and full-context projection are intentionally minimal.

*`docs/DESIGN.md` was updated 2026-04-14 to reflect Stage 3. The first recommended next step from the original audit list is complete.*

---

## Chunks & Dependency Order

| Chunk | What | Depends On |
|---|---|---|
| **P1** | `AsyncAnthropic` migration (`agent.py`, `main.py`) | — |
| **P2** | `busy_timeout = 5000` pragma (`db/connection.py`) | — |
| **3a** | `events` table + append-only triggers + `db/events.py` | — |
| **3b** | `turn_id`/`conversation_id` threading, `user_message` event writes | P1, 3a |
| **3c** | Messages-as-projection (replace in-memory history) | 3a, 3b |
| **3d** | Strip to `add_fact`, server owns `tool_result` transaction, `meta` passthrough | P1, P2, 3a, 3b |
| **3e** | `parent_event_id` correct for all event types, `api_call`/`assistant_message` events | 3a–3d |

**Execution waves:**
1. P1 + P2 + 3a (all independent — run in parallel)
2. 3b (needs P1 + 3a)
3. 3c + 3d (both need 3b — run in parallel)
4. 3e (needs everything)

---

## Chunk P1: AsyncAnthropic Migration — ✓ Complete

**Scope:** `bot/agent.py`, `bot/main.py`

`bot/agent.py`:
- `from anthropic import Anthropic` → `from anthropic import AsyncAnthropic`
- `run_loop` signature: `client: Anthropic` → `client: AsyncAnthropic`
- `client.messages.create(...)` → `await client.messages.create(...)`

`bot/main.py`:
- `from anthropic import Anthropic` → `from anthropic import AsyncAnthropic`
- `client = Anthropic(api_key=api_key)` → `client = AsyncAnthropic(api_key=api_key)`

**Verification:** `cd bot && uv run python main.py` — same output, no `RuntimeWarning: coroutine was never awaited`.

**Audit note:** Fully implemented as specified. `bot/agent.py` imports `AsyncAnthropic`, `run_loop` is declared `async def`, and the API call is `await client.messages.create(...)`. `bot/main.py` instantiates `AsyncAnthropic` and drives the loop via `asyncio.run(main())`.

---

## Chunk P2: busy_timeout Pragma — ✓ Complete

**Scope:** `db/connection.py`

In `get_db()`, add after existing pragmas:
```python
conn.execute("PRAGMA busy_timeout = 5000")
```
Order: WAL → foreign_keys → busy_timeout.

**Verification:**
```python
from db.connection import get_db
with get_db() as conn:
    val = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert val == 5000
```

**Audit note:** Fully implemented. `db/connection.py` `get_db()` applies pragmas in the specified order: WAL → foreign_keys → busy_timeout. Pragma is present on both the `get_db()` context manager (used by the agent) and on the manual connection opened in `tools/server.py` for the `BEGIN IMMEDIATE` transaction.

---

## Chunk 3a: Events Table Schema — ✓ Complete

**Scope:** `db/connection.py`, new `db/events.py`

`db/connection.py` — add to `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    turn_id          TEXT    NOT NULL,
    conversation_id  TEXT    NOT NULL,
    correlation_id   TEXT    NOT NULL,
    type             TEXT    NOT NULL,
    parent_event_id  INTEGER REFERENCES events(id),
    payload          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_conv_ts
    ON events(conversation_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events(type);

CREATE INDEX IF NOT EXISTS idx_events_parent
    ON events(parent_event_id);

CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE not permitted');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE not permitted');
END;
```

**Note:** Stage 3 is a hard reset — delete the DB file before running for the first time. No migration tooling needed.

`db/events.py` — new module with:

```python
def append(
    conn: sqlite3.Connection,
    type: str,
    payload: dict,
    turn_id: str,
    conversation_id: str,
    parent_event_id: int | None = None,
) -> int:
    """Insert one event row within the caller's open connection/transaction.
    Sets correlation_id = turn_id (aliased per spec). Returns the new event id."""
```

Also add:
```python
def get_tool_result_event_id(conn: sqlite3.Connection, tool_use_id: str) -> int | None:
    """Return the event id of the tool_result event matching the given tool_use_id."""
    # SELECT id FROM events WHERE type='tool_result'
    # AND json_extract(payload, '$.tool_use_id') = ?
    # ORDER BY id DESC LIMIT 1
```

And a causality debug helper:
```python
def walk_causality(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    """Return all events in a turn ordered by id, with parent chain info."""
```

**Verification:**
```python
from db.connection import get_db, init_db
import db.events as ev
init_db()
with get_db() as conn:
    eid = ev.append(conn, 'user_message', {'v': 1, 'text': 'hi', 'source': 'test_harness'},
                    turn_id='t1', conversation_id='c1')
    assert isinstance(eid, int) and eid > 0
    try:
        conn.execute("UPDATE events SET type='x' WHERE id=?", (eid,))
        assert False
    except Exception as e:
        assert 'append-only' in str(e)
    try:
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
        assert False
    except Exception as e:
        assert 'append-only' in str(e)
print("3a OK")
```

**Audit note:** Fully implemented. `db/connection.py` `_SCHEMA` contains the `events` table with all seven columns, all three indexes (`idx_events_conv_ts`, `idx_events_type`, `idx_events_parent`), and both append-only triggers (`events_no_update`, `events_no_delete`). `db/events.py` exists with all three functions: `append` (sets `correlation_id = turn_id` internally, returns `lastrowid`), `get_tool_result_event_id` (queries via `json_extract`), and `walk_causality` (returns list of dicts for a turn).

---

## Chunk 3b: Turn Lifecycle — ✓ Complete

**Scope:** `bot/agent.py`, `bot/main.py`

`bot/agent.py`:
- Add imports: `uuid`, `datetime/timezone`, `db.events`, `db.connection.get_db`
- Change `run_loop` signature — remove `messages` parameter, add `conversation_id: str`, return type simplifies to `str`
- Generate `turn_id = str(uuid.uuid4())` at top of function
- Write `user_message` event immediately on entry
- Stub the projection with empty list for now (replaced in 3c)
- Pass `turn_id` and `conversation_id` forward for use in 3d

`bot/main.py`:
- Call `init_db()` at startup (currently only done in the MCP server — see Gap 10)
- Generate `conversation_id = str(uuid.uuid4())` once before the loop
- Update `run_loop` calls: remove `messages`, pass `conversation_id`, handle `str` return

**Verification:** Two events in DB with same `conversation_id`, different `turn_id`s.

**Audit note:** Fully implemented. `bot/agent.py` `run_loop` generates `turn_id = str(uuid.uuid4())` on entry and writes the `user_message` event immediately with `source: "test_harness"`. `bot/main.py` calls `init_db()` at startup (resolving Gap 10) and generates a single `conversation_id` before the loop. One minor point: the `source` field in the `user_message` payload is hardcoded to `"test_harness"` — this will need updating when Stage 5 (Telegram) lands.

---

## Chunk 3c: Messages-as-Projection — ✓ Complete

**Scope:** `bot/agent.py` (or new `bot/projection.py`)

```python
def build_messages(conn: sqlite3.Connection, conversation_id: str) -> list[dict]:
    """Query all prior events for conversation_id and build the Anthropic messages list."""
```

Implementation:
1. Query `events WHERE conversation_id = ? ORDER BY id ASC`
2. Walk rows, grouping by `turn_id`
3. Emit `user_message` → `{"role": "user", "content": text}`
4. Emit complete `tool_call`+`tool_result` pairs as `tool_use` blocks in assistant message + `tool_result` in user message. Batch consecutive tool calls from one `api_call` response into one assistant message / one user message.
5. Emit `assistant_message` → `{"role": "assistant", "content": [{"type": "text", "text": text}]}`

**Orphan tolerance:**
- `tool_call` with no matching `tool_result` (MCP crash): skip entire incomplete tool exchange for that turn
- `user_message` with no `assistant_message` (agent crash): include user message, emit nothing else for that turn

In `run_loop`: call `build_messages` at turn start to get baseline; append current user message and continue loop as before.

**Verification:** Third message referencing first ("What do I like to drink?") answered correctly from projected history.

**Audit note:** Fully implemented. `build_messages` lives in `bot/agent.py` (not a separate `bot/projection.py` — acceptable, the plan said "or new `bot/projection.py`"). Implementation groups events by `turn_id` preserving encounter order, skips turns missing `user_message` or `assistant_message` (crash-tolerance shape 2), skips orphaned `tool_call` events with no matching `tool_result` (crash-tolerance shape 1), correctly batches multiple tool_use blocks into one assistant message + one user message, and reconstructs the assistant text block as `[{"type": "text", "text": text}]` to match what the API requires. The projection is called once at turn start; the current user message is appended to the local list immediately after, consistent with the spec's description.

---

## Chunk 3d: Minimal Tool Rebuild — ✓ Complete

**Scope:** `tools/server.py`, `db/facts.py`, `bot/mcp_client.py`, `bot/agent.py`

**`db/facts.py`:** Refactor `add_fact` to accept an open `conn` parameter instead of opening its own `get_db()`. Remove `get_fact`, `search_facts`, `list_facts`.

**`tools/server.py`:**
- Remove all tools except `add_fact`
- In `add_fact` handler: read `turn_id`, `conversation_id`, `tool_call_event_id`, `tool_use_id` from `meta`
- Open connection in `isolation_level=None` (autocommit off, manual transaction control)
- Execute `BEGIN IMMEDIATE`, then atomically:
  1. Write `facts` row via `facts_db.add_fact(conn, ...)`
  2. Write `mutation_log` row via `mutation_log.log(conn, ...)`
  3. Write `tool_result` event via `events_db.append(conn, 'tool_result', payload, ...)`
  4. `COMMIT`
- Error path: set `is_error: True`, still write `tool_result` event, commit

`tool_result` payload:
```python
{"v": 1, "tool_use_id": tool_use_id, "tool_call_event_id": tool_call_event_id,
 "is_error": False, "content": result_string}
```

**`bot/mcp_client.py`:** Add `meta: dict | None = None` to `call_tool`, pass to `session.call_tool`.

**`bot/agent.py`:** Before each `mcp.call_tool`:
- Write `tool_call` event, capture its id
- Pass `{turn_id, conversation_id, tool_call_event_id, tool_use_id}` via `meta`

**Verification:** After `"Please remember that I like coffee."` — verify `tool_call` event, `tool_result` event (with matching `tool_call_event_id`), `facts` row, and `mutation_log` row all exist in DB.

**Audit note:** Fully implemented. Deviations from the spec are minor and all correct:

- `db/facts.py` `add_fact` accepts an open `conn` and does NOT call `mutation_log.log` internally — the mutation log write is done by the server handler directly after `facts_db.add_fact`. This is a clean separation of concerns and matches the intent; the spec said "refactor `add_fact` to accept an open `conn`" without mandating it call mutation_log itself.
- `tools/server.py` opens its connection with `isolation_level=None` and executes `BEGIN IMMEDIATE` manually — exactly as Gap 4 required. The three writes (facts row, mutation_log row, tool_result event) happen inside that single transaction before `COMMIT`.
- Error path: on exception, the server rolls back and writes a `tool_result` event with `is_error: True` on a fresh `get_db()` connection. The spec said "still write `tool_result` event" on error — this is done correctly. The implementation wraps the error event write in its own try/except with `pass` to avoid masking the original error.
- `bot/mcp_client.py` `call_tool` accepts `meta: dict | None = None` and passes it to `session.call_tool` (Gap 6 resolved; `mcp>=1.27.0` is in `pyproject.toml`).
- `bot/agent.py` writes the `tool_call` event before dispatching and passes `{turn_id, conversation_id, tool_call_event_id, tool_use_id}` via `meta`.

---

## Chunk 3e: Causality Wiring — ✓ Complete

**Scope:** `bot/agent.py`, `db/events.py`

Track these locals in `run_loop`:
```python
user_event_id: int
last_input_event_id: int       # starts = user_event_id; updates after each tool_result
current_api_call_event_id: int | None = None
spawning_api_call_event_id: int | None = None
```

**Per-loop-iteration:**

1. Write `api_call` event after the API call completes with full payload (started_at/finished_at, usage, model_used, request_id, stop_reason, status). Set `current_api_call_event_id`.

   `parent_event_id` rules:
   - First iteration: `user_event_id`
   - After single tool: `last_input_event_id` (the tool_result event id)
   - After parallel tools: `spawning_api_call_event_id`

2. On `tool_use`: set `spawning_api_call_event_id = current_api_call_event_id`. Write each `tool_call` event with `parent_event_id = current_api_call_event_id`. After dispatch, look up `tool_result` event id via `events_db.get_tool_result_event_id(conn, tool_use_id)`. For single tool: `last_input_event_id = tool_result_event_id`. For parallel: don't update `last_input_event_id`.

3. On `end_turn`: write `assistant_message` event with `parent_event_id = current_api_call_event_id`.

Add inline comment at parallel-tool parent assignment:
```python
# Parallel tool calls: the api_call that follows their results points at the
# api_call that spawned them, not any individual tool_result. This keeps the
# causality tree a clean tree (no fan-in edges). See PROJECT_PLAN.md §3e.
```

`api_call` payload fields (from EVENTS.md):
- `status`, `model_requested`, `model_used`, `request_id` (from `response._request_id` — private attr, acceptable for personal project)
- `beta_headers: []`, `stop_reason`, `usage` (including `cache_read_input_tokens`, `cache_creation_input_tokens`)
- `cost_usd_millicents: null`, `price_table_version: null` (price table deferred per spec)
- `error: null`, `started_at`, `finished_at`

**Verification:** Run `walk_causality` on a turn with a tool call. Expected tree:
```
user_message       parent=null
api_call           parent=user_message.id
tool_call          parent=api_call.id
tool_result        parent=tool_call.id      (written by MCP server)
api_call           parent=tool_result.id    (single-tool case)
assistant_message  parent=api_call.id
```

**Audit note:** Fully implemented. All four causality locals are declared and managed correctly in `run_loop`:

- `user_event_id` — set once on `user_message` write; used as first `api_call`'s parent.
- `last_input_event_id` — starts as `user_event_id`; updates to `tool_result` event id after each single-tool step via `events_db.get_tool_result_event_id`.
- `current_api_call_event_id` — set after every `api_call` write; used as parent for `tool_call` and `assistant_message` events.
- `spawning_api_call_event_id` — set to `current_api_call_event_id` when parallel tools are dispatched; consumed (cleared to `None`) by the next `api_call` parent-selection logic.

The `api_call` payload is complete: `status`, `model_requested`, `model_used`, `request_id` (via `getattr(response, "_request_id", None)`), `beta_headers: []`, `stop_reason`, full `usage` dict (including `cache_read_input_tokens` and `cache_creation_input_tokens` via `getattr` with 0 default), `cost_usd_millicents: None`, `price_table_version: None`, `error: None`, `started_at`, `finished_at`. The error-path `api_call` event is also written before re-raising. The required inline comment for the parallel-tool parent assignment is present verbatim. `assistant_message` event is written on `end_turn` with `parent_event_id = current_api_call_event_id` and includes `api_call_event_id` in the payload.

One minor deviation: the `is_first_api_call` flag is used as a boolean guard rather than checking `last_input_event_id == user_event_id` — functionally equivalent and slightly cleaner.

---

## Gaps & Risks

1. **`db.*` imports in MCP server subprocess** — currently works (confirmed by existing `db.connection` import in server). New `db.events` import follows same pattern; verify it works in the subprocess context.

2. **`executescript` in `init_db()` auto-commits** — called only at startup, not a runtime hazard. Add a comment warning against calling it inside a handler.

3. **`add_fact` conn refactor is required** (not optional) — the current version opens its own `get_db()` which would create a separate transaction, breaking atomicity with the `tool_result` event write.

4. **`BEGIN IMMEDIATE` in `get_db()`** — Python's sqlite3 may have already issued a `BEGIN DEFERRED`. For the server's tool transaction, open connection with `isolation_level=None` and manage `BEGIN IMMEDIATE` / `COMMIT` manually.

5. **`response._request_id` is private** — present in current SDK, could be removed. Add a comment.

6. **`pyproject.toml` MCP version** — tighten to `mcp>=1.27.0` (needs `meta` kwarg on `call_tool`).

7. **Parallel tool call verification** — add a test prompt that forces two simultaneous `add_fact` calls (e.g. `"Remember I like coffee and my name is Matt"`).

8. **`correlation_id` aliased to `turn_id`** — `events_db.append` sets this internally; no caller should pass a separate value.

9. **Multi-block `end_turn` responses** — text extraction logic must be identical between `run_loop` (writing event payload) and `build_messages` (projecting from event). A mismatch creates a history discrepancy.

10. **`init_db()` must be called in agent process** — currently only called in `tools/server.py`. Add to `bot/main.py` startup.
