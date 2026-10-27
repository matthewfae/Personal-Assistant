# Personal-Assistant

A single-user personal assistant: a Python Telegram bot that routes incoming messages through the Claude API, backed by a local SQLite context database. Claude uses MCP tool calls to read and write the DB in a multi-step agent loop, then replies to the user via Telegram.

Not designed for external use or deployment — this is a personal project running on my own host.

## Status

Stages 1–8 and 10a–10d are complete. The agent loop records every API call, tool call, and message as an immutable event row. Conversation history is projected from the events table at the start of each turn. A weekly `trim_context` wake-up lets Claude trim that history (`set_context_bound`), and a hard cap keeps it to the last 30 turns. Claude manages tasks, a shopping list, and symptom logs through MCP tools, and can run read-only SQL over any table. Wake-up turns let Claude act without a message: an HTTPS webhook (`https://pa.mattfae.com/hooks/<name>`, behind Caddy) starts a job such as `arrived_home`, an in-process scheduler starts others (`weekend_plan` on Saturday mornings), and Claude messages the user only if it calls `notify_user`. The bot runs as a `systemd` service. See `PROJECT_PLAN.md` for the stage breakdown and current TODOs.

## Repository layout

```
bot/         Entry point, assistant core, agent loop, Telegram channel, webhooks, scheduler, jobs, MCP client
tools/       MCP server (tool schemas and dispatch)
db/          SQLite access layer (connection, schema, tables, mutation log)
prompts/     System prompt template
docs/        Implementation-level design docs
tests/       Tests
scripts/     systemd unit, Caddyfile and other host config
```

## Docs

- **`PROJECT_PLAN.md`** — architecture decisions, current stage, upcoming stages.
- **`docs/DESIGN.md`** — layout, schema, agent flow, and current tool surface.

## Running tests

```
uv run pytest
```

## Logs

The bot logs to the systemd journal. Each turn writes one summary line (API calls, token counts, duration); problems are logged at WARNING or above.

```
journalctl -u personal-assistant -f            # follow
journalctl -u personal-assistant -p warning    # problems only
```

## Host setup

What lives outside the repo, and how to recreate it on a fresh droplet:

- **systemd:** copy `scripts/personal-assistant.service` to `/etc/systemd/system/`, then `sudo systemctl enable --now personal-assistant`.
- **`.env`:** copy `.env.example`, fill it in, `chmod 600 .env`. Generate `WEBHOOK_TOKEN` with the command in `.env.example`.
- **DNS:** an A record `pa.mattfae.com` → the droplet's public IP (currently `104.236.236.93`). No AAAA record; the droplet has no IPv6.
- **Caddy** (HTTPS for the webhooks): install from Caddy's official apt repo (caddyserver.com/docs/install), copy `scripts/Caddyfile` to `/etc/caddy/Caddyfile`, `sudo systemctl reload caddy`. Caddy gets and renews the certificate itself; ports 80 and 443 must be reachable.
- **iPhone arrival automation:** Shortcuts → Automation → Arrive (home, any time, Run Immediately) → Get Contents of URL: `https://pa.mattfae.com/hooks/arrived-home`, method POST, header `Authorization` = `Bearer <WEBHOOK_TOKEN>`. If the token changes, update it here too.

## Stack

- Python 3.11+, managed with `uv`
- `anthropic`, `mcp`, `python-telegram-bot`, `aiohttp` (webhook listener), `python-dotenv`
- `sqlite3` from stdlib
- Caddy on the host, for HTTPS in front of the webhooks
- Target model: configured in `bot/agent.py`
