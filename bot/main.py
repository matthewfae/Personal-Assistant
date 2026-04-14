"""
Main entry point for testing the agent loop.
"""

import asyncio
import os
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
from db.connection import get_db, init_db
import db.events as events_db
from mcp_client import mcp_client

load_dotenv()


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
        turn_ids = []
        for message in test_messages:
            print(f"\nUser: {message}")
            response = await run_loop(client, mcp, conversation_id, message)
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
