"""
Logging configuration shared by the bot and the MCP tools server.

Call configure_logging() once, from each process's entry point (bot/main.py,
bot/harness.py, tools/server.py). Library modules only create loggers.

When stderr is the systemd journal, each line is prefixed with a syslog
priority such as <3>, so journald records the real level and
`journalctl -p warning` works. The timestamp is left out there because
journald adds its own. Anywhere else (a terminal, the harness), lines carry a
timestamp and no prefix.
"""

import logging
import os
import sys

_FORMAT = "%(name)s - %(levelname)s - %(message)s"


def _syslog_priority(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return 2
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    if levelno >= logging.INFO:
        return 6
    return 7


class JournalFormatter(logging.Formatter):
    """Prefix every line of a record with its syslog priority.

    journald reads the prefix per line, so traceback lines need it too;
    otherwise they would be recorded at the default (info) priority.
    """

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{_syslog_priority(record.levelno)}>"
        return "\n".join(prefix + line for line in super().format(record).splitlines())


def _is_journal(stream) -> bool:
    """True if stream is connected to the journal.

    systemd sets JOURNAL_STREAM to "<device>:<inode>" of the journal stream it
    connected. Comparing it with the stream's own fd (rather than just checking
    the variable exists) avoids false positives when the variable is inherited
    by a process whose output goes elsewhere.
    """
    value = os.environ.get("JOURNAL_STREAM")
    if not value:
        return False
    try:
        dev, ino = (int(part) for part in value.split(":"))
        st = os.fstat(stream.fileno())
    except (ValueError, OSError):
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


def configure_logging(level: int = logging.INFO) -> None:
    """Send log records to stderr, formatted for the journal or a terminal."""
    handler = logging.StreamHandler(sys.stderr)
    if _is_journal(sys.stderr):
        handler.setFormatter(JournalFormatter(_FORMAT))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s - " + _FORMAT))
    logging.basicConfig(level=level, handlers=[handler])

    # httpx logs every request URL at INFO, and Telegram API URLs contain the bot
    # token. Raise it to WARNING so the token never lands in the journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
