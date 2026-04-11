"""
Agent loop: handles multi-turn conversation with Claude API and tool use.
Implements prompt caching for cost reduction on repeated context.
"""

import json
from typing import Any, Callable
from datetime import datetime
from anthropic import Anthropic

# Define toy tools
TOOLS = [
    {
        "name": "get_current_time",
        "description": "Get the current date and time",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
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
    },
]

SYSTEM_PROMPT = """You are a helpful personal assistant. You have access to tools to store and retrieve information.
When the user asks you to remember something, use the add_fact tool.
Be concise and helpful in your responses."""


def get_current_time() -> str:
    """Get current date and time."""
    return datetime.now().isoformat()


def add_fact(key: str, value: str, category: str = "general") -> str:
    """Add a fact to the knowledge base (placeholder - will integrate with DB in Stage 3)."""
    return f"Stored fact: {key}={value} (category: {category})"


# Map tool names to functions
TOOL_FUNCTIONS: dict[str, Callable] = {
    "get_current_time": get_current_time,
    "add_fact": add_fact,
}


class Agent:
    """Agent loop with Claude API and tool use, using prompt caching."""

    def __init__(self, api_key: str):
        self.client = Anthropic(api_key=api_key)
        self.conversation_history: list[dict[str, Any]] = []

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result."""
        if tool_name not in TOOL_FUNCTIONS:
            return f"Error: Unknown tool '{tool_name}'"

        try:
            func = TOOL_FUNCTIONS[tool_name]
            result = func(**tool_input)
            return str(result)
        except TypeError as e:
            return f"Error calling {tool_name}: {e}"

    def run(self, user_message: str) -> str:
        """
        Run the agent loop: send message to Claude, handle tool calls, return final response.
        """
        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        # Agentic loop
        while True:
            # Call Claude with prompt caching enabled
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=self.conversation_history,
            )

            # Check stop reason
            if response.stop_reason == "end_turn":
                # Claude finished without tool calls - extract final response
                final_response = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        final_response = block.text
                        break

                # Add assistant response to history
                self.conversation_history.append(
                    {"role": "assistant", "content": response.content}
                )

                return final_response

            elif response.stop_reason == "tool_use":
                # Claude wants to use a tool
                # Add assistant's response (with tool_use blocks) to history
                self.conversation_history.append(
                    {"role": "assistant", "content": response.content}
                )

                # Process all tool calls in the response
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_result = self.process_tool_call(
                            block.name, block.input
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tool_result,
                            }
                        )

                # Add tool results to history
                self.conversation_history.append(
                    {"role": "user", "content": tool_results}
                )

            else:
                # Unexpected stop reason
                return f"Unexpected stop reason: {response.stop_reason}"

    def reset(self):
        """Clear conversation history for a fresh start."""
        self.conversation_history = []
