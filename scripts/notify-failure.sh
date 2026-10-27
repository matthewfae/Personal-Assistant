#!/bin/sh
# Sends a Telegram message straight through the Bot API when the bot's systemd
# unit fails. Run by personal-assistant-alert.service (OnFailure= in
# personal-assistant.service), so it works even though the bot itself is down.
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID from .env.
set -eu
host=$(hostname)
text="Personal Assistant crashed and systemd gave up restarting it on $host. Check: journalctl -u personal-assistant -n 50"
curl -sS --fail --max-time 15 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
    --data-urlencode "text=${text}" > /dev/null
