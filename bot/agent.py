"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Implements prompt caching for cost reduction on repeated context.
"""

from typing import Any
from anthropic import Anthropic
from prompt_builder import PromptBuilder

_prompt_builder = PromptBuilder()

TOOLS = [
    {
        "name": "add_fact",
        "description": "Add a fact to the knowledge base",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to store the fact under",
                },
                "value": {
                    "type": "string",
                    "description": "Value of the fact",
                },
                "category": {
                    "type": "string",
                    "description": "Category for organization",
                },
            },
            "required": ["key", "value"],
        },
        "cache_control": {"type": "ephemeral"},
    },
]


def add_fact(key: str, value: str, category: str = "general") -> str:
    """Add a fact to the knowledge base (placeholder - will integrate with DB in Stage 3)."""
    return f"Stored fact: {key}={value} (category: {category})"


_TOOL_FUNCTIONS = {
    "add_fact": add_fact,
}


def _execute_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name not in _TOOL_FUNCTIONS:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        return str(_TOOL_FUNCTIONS[tool_name](**tool_input))
    except TypeError as e:
        return f"Error calling {tool_name}: {e}"


def run_loop(
    client: Anthropic,
    messages: list[dict[str, Any]],
    user_message: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Run the agent loop for one user turn.

    Takes the existing message history and a new user message, runs until
    Claude produces a final text response, and returns that response along
    with the updated message history.
    """
    messages = list(messages)
    messages.append({"role": "user", "content": user_message})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _prompt_builder.build(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = next(
                (block.text for block in response.content if block.type == "text"),
                "",
            )
            return final_text, messages

        elif response.stop_reason == "tool_use":
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _execute_tool(block.name, block.input),
                }
                for block in response.content
                if block.type == "tool_use"
            ]
            messages.append({"role": "user", "content": tool_results})

        else:
            return f"Unexpected stop reason: {response.stop_reason}", messages
