"""
Main entry point for testing the agent loop.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# Ensure both the project root (for db.*) and the bot/ directory (for agent,
# mcp_client, etc.) are importable regardless of the working directory.
_HERE = Path(__file__).resolve().parent        # bot/
_ROOT = _HERE.parent                           # project root
for _p in (_ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent import run_loop
import db.events as events_db
from db.connection import get_db, init_db
from mcp_client import MCPClient, mcp_client

load_dotenv()

# Absolute path to the system prompt, resolved relative to this file so it
# works regardless of the working directory the process is launched from.
_SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system.md"

_SESSION_TURN_ID = "session-init"


def _git_head() -> str | None:
    """Return the current git HEAD SHA, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _prompt_hash() -> str:
    """Return the first 12 hex chars of the SHA-256 hash of prompts/system.md bytes."""
    data = _SYSTEM_PROMPT_PATH.read_bytes()
    return hashlib.sha256(data).hexdigest()[:12]


def _tool_set_hash(tools: list[dict]) -> str:
    """Return the first 12 hex chars of the SHA-256 hash of the canonical tool schemas.

    Canonical form: sorted tool list (by name), each schema serialised with
    sorted keys and no whitespace. This is stable across Python dict orderings
    and MCP server restarts.

    Note: cache_control fields added by mcp_client are stripped before hashing
    so the hash reflects only the tool schema content, not SDK-side decorations.
    """
    clean_tools = [
        {k: v for k, v in tool.items() if k != "cache_control"}
        for tool in tools
    ]
    clean_tools.sort(key=lambda t: t["name"])
    canonical = json.dumps(clean_tools, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _most_recent_hash(event_type: str) -> str | None:
    """Return the content_hash from the most recent event of the given type, or None."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT payload FROM events
            WHERE type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_type,),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    return payload.get("content_hash")


def write_version_events(mcp: MCPClient, conversation_id: str) -> None:
    """Write prompt_version and tool_set_version events at session start.

    Skips writing an event if its content hash matches the most recent event of
    that type already in the DB (across all conversations) — no change, no event.

    Both events are written with turn_id='session-init' to signal they are
    session-level bookkeeping, not part of any conversational turn.
    """
    git_sha = _git_head()

    # --- prompt_version ---
    p_hash = _prompt_hash()
    if _most_recent_hash("prompt_version") != p_hash:
        payload = {"v": 1, "content_hash": p_hash, "git_sha": git_sha}
        with get_db() as conn:
            events_db.append(
                conn,
                type="prompt_version",
                payload=payload,
                turn_id=_SESSION_TURN_ID,
                conversation_id=conversation_id,
            )

    # --- tool_set_version ---
    t_hash = _tool_set_hash(mcp.tools)
    if _most_recent_hash("tool_set_version") != t_hash:
        payload = {"v": 1, "content_hash": t_hash, "git_sha": git_sha}
        with get_db() as conn:
            events_db.append(
                conn,
                type="tool_set_version",
                payload=payload,
                turn_id=_SESSION_TURN_ID,
                conversation_id=conversation_id,
            )


def print_causality_tree(turn_id: str, label: str) -> None:
    """Print the causality tree for a turn using walk_causality."""
    with get_db() as conn:
        events = events_db.walk_causality(conn, turn_id)
    print(f"\n--- Causality tree: {label} ---")
    for ev in events:
        parent = ev["parent_event_id"]
        print(f"  id={ev['id']:>4}  type={ev['type']:<20}  parent={parent}")
    print("---")


async def main():
    init_db()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        return

    client = AsyncAnthropic(api_key=api_key)

    # First two messages test the single-tool path (already exercised).
    # Third message is the parallel-tool smoke test: asking Claude to remember
    # two facts at once should trigger two simultaneous add_fact calls,
    # exercising the spawning_api_call_event_id path in run_loop.
    test_messages = [
        "Please remember that I like coffee.",
        "What's 2 + 2?",
        "Remember I like coffee and my name is Matt",
    ]

    async with mcp_client() as mcp:
        conversation_id = str(uuid.uuid4())
        write_version_events(mcp, conversation_id)
        turn_ids = []
        for message in test_messages:
            print(f"\nUser: {message}")
            response = await run_loop(client, mcp, conversation_id, message, source="test_harness")
            print(f"Assistant: {response}")
            print("-" * 60)

            # Retrieve the turn_id for the turn we just ran (latest for this conversation)
            with get_db() as conn:
                row = conn.execute(
                    """
                    SELECT turn_id FROM events
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
            if row:
                turn_ids.append((message[:40], row["turn_id"]))

        # Print causality trees for all turns so the parallel-tool path is visible.
        print("\n" + "=" * 60)
        print("CAUSALITY TREES")
        print("=" * 60)
        for label, turn_id in turn_ids:
            print_causality_tree(turn_id, label)


if __name__ == "__main__":
    asyncio.run(main())
