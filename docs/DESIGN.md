# Current State

## Layout
```
bot/agent.py         Agent class with a run() loop
bot/main.py          test harness that sends scripted messages to Agent
db/                  empty
tools/               empty
tests/               empty
prompts/system.md    system prompt template — not currently read by anything
```

## Flow
`main.py` instantiates an `Agent`, then sends it test messages one at a time. `Agent.run(message)`:
1. Appends the message to `self.conversation_history`.
2. Calls Claude with the history, a hardcoded system prompt, and two tool definitions.
3. On `end_turn`, extracts the text and returns it.
4. On `tool_use`, runs the tool, appends both the tool_use block and tool_result to history, loops.

## Tools
Two toy tools are defined inline in `agent.py`:
- `get_current_time()` — returns `datetime.now().isoformat()`.
- `add_fact(key, value, category)` — returns a formatted string; does not persist anything.

## State
Conversation history is an in-memory list on the `Agent` instance. No database, no persistence, no Telegram, no auth.

## System prompt
The prompt Claude actually sees is a hardcoded string constant in `agent.py`. `prompts/system.md` exists with a `{current_time}` placeholder but nothing reads or renders it.

## Prompt caching
The system prompt block is marked `cache_control: ephemeral`, which caches it along with the tool definitions.

## Config
`.env.example` declares `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DB_PATH`. Only `ANTHROPIC_API_KEY` is currently consumed.
