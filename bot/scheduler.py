"""
Scheduler: starts wake-up jobs on a clock.

Schedules are defined in code (SCHEDULE below) in local time (TIMEZONE, see
prompt_builder), so "09:00" stays 9 AM across daylight saving changes.

The scheduler ticks every _TICK_SECONDS and fires a job when its most recent
scheduled time has passed and the job hasn't run since. A run is missed while
the bot is down; on the next start it is caught up if it is less than
_CATCH_UP old, and skipped otherwise (a weekend plan at 4 PM isn't useful).

"Hasn't run since" is read from the job's last `wakeup` event at startup, so a
restart neither repeats a run nor forgets one. After that it is tracked in
memory: a submitted job's event is only written when the turn starts.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from core import Core
from db.connection import get_db
from jobs import WEEKEND_PLAN, Job
from prompt_builder import TZ

logger = logging.getLogger(__name__)

_TICK_SECONDS = 30
_CATCH_UP = timedelta(hours=2)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@dataclass(frozen=True)
class Schedule:
    job: Job
    at: time  # local time
    weekdays: frozenset[int] | None = None  # None = every day

    def previous_fire(self, now: datetime) -> datetime:
        """The most recent scheduled time at or before now (aware, local)."""
        local = now.astimezone(TZ)
        for days_back in range(8):
            day = local.date() - timedelta(days=days_back)
            if self.weekdays is not None and day.weekday() not in self.weekdays:
                continue
            fire = datetime.combine(day, self.at, tzinfo=TZ)
            if fire <= local:
                return fire
        raise ValueError(f"Schedule for {self.job.name} has no weekdays")


SCHEDULE = [
    Schedule(WEEKEND_PLAN, at=time(9, 0), weekdays=frozenset({SAT})),
]


def last_run(job_name: str) -> datetime | None:
    """Timestamp of the job's most recent wakeup event, or None if it never ran."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT timestamp FROM events WHERE type = 'wakeup' "
            "AND json_extract(payload, '$.job') = ? ORDER BY id DESC LIMIT 1",
            (job_name,),
        ).fetchone()
    return datetime.fromisoformat(row["timestamp"]) if row else None


class Scheduler:
    def __init__(self, core: Core, schedule: list[Schedule] = SCHEDULE):
        self._core = core
        self._schedule = schedule
        self._last_run = {s.job.name: last_run(s.job.name) for s in schedule}

    def tick(self, now: datetime) -> list[str]:
        """Submit every job that is due at now. Returns the names submitted."""
        fired = []
        for s in self._schedule:
            due = s.previous_fire(now)
            last = self._last_run[s.job.name]
            if last is not None and last >= due:
                continue
            self._last_run[s.job.name] = now
            if now - due > _CATCH_UP:
                logger.info("Scheduler: skipped %s due %s, more than %s ago", s.job.name, due, _CATCH_UP)
                continue
            self._core.submit_wakeup(s.job)
            fired.append(s.job.name)
        return fired

    async def _run(self) -> None:
        while True:
            try:
                self.tick(datetime.now(timezone.utc))
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(_TICK_SECONDS)

    @asynccontextmanager
    async def running(self):
        """Tick in the background until exit."""
        task = asyncio.create_task(self._run(), name="scheduler")
        logger.info("Scheduler started: %s", ", ".join(s.job.name for s in self._schedule))
        try:
            yield self
        finally:
            task.cancel()
