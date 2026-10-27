"""Tests for the scheduler: due times, DST, catch-up window and restart state."""

from datetime import datetime, time, timedelta

from db.connection import get_db
from jobs import Job
from prompt_builder import TZ
from scheduler import SAT, Schedule, Scheduler, last_run

_JOB = Job(name="weekend_plan", prompt="p")
_SAT_9AM = Schedule(_JOB, at=time(9, 0), weekdays=frozenset({SAT}))


class FakeCore:
    def __init__(self):
        self.wakeups = []

    def submit_wakeup(self, job):
        self.wakeups.append(job.name)


def _local(*args):
    return datetime(*args, tzinfo=TZ)


def _write_wakeup(job: str, at: datetime):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO events (timestamp, turn_id, type, payload) VALUES (?, 't', 'wakeup', ?)",
            (at.isoformat(), f'{{"v": 1, "job": "{job}", "prompt": "p"}}'),
        )


def test_previous_fire_finds_last_saturday():
    # Tuesday Oct 27 2026 -> Saturday Oct 24, 9:00 local
    assert _SAT_9AM.previous_fire(_local(2026, 10, 27, 12, 0)) == _local(2026, 10, 24, 9, 0)
    # Saturday before 9 -> the previous Saturday
    assert _SAT_9AM.previous_fire(_local(2026, 10, 31, 8, 59)) == _local(2026, 10, 24, 9, 0)
    # Saturday at 9 exactly -> today
    assert _SAT_9AM.previous_fire(_local(2026, 10, 31, 9, 0)) == _local(2026, 10, 31, 9, 0)


def test_previous_fire_keeps_local_time_across_dst():
    # US DST ends Sun Nov 1 2026: 9 AM is 14:00 UTC before, 15:00 UTC after.
    before = _SAT_9AM.previous_fire(_local(2026, 10, 31, 10, 0))
    after = _SAT_9AM.previous_fire(_local(2026, 11, 7, 10, 0))
    assert before.utcoffset() == timedelta(hours=-5)
    assert after.utcoffset() == timedelta(hours=-6)
    assert (before.hour, after.hour) == (9, 9)


def test_fires_once_when_due():
    core = FakeCore()
    _write_wakeup("weekend_plan", _local(2026, 10, 24, 9, 0))  # last week's run
    scheduler = Scheduler(core, [_SAT_9AM])

    assert scheduler.tick(_local(2026, 10, 31, 8, 59)) == []
    assert scheduler.tick(_local(2026, 10, 31, 9, 0)) == ["weekend_plan"]
    assert scheduler.tick(_local(2026, 10, 31, 9, 1)) == []
    assert core.wakeups == ["weekend_plan"]


def test_catches_up_a_recent_miss_but_skips_a_stale_one():
    _write_wakeup("weekend_plan", _local(2026, 10, 24, 9, 0))

    core = FakeCore()
    assert Scheduler(core, [_SAT_9AM]).tick(_local(2026, 10, 31, 10, 30)) == ["weekend_plan"]

    core = FakeCore()
    assert Scheduler(core, [_SAT_9AM]).tick(_local(2026, 10, 31, 16, 0)) == []


def test_skipped_run_is_not_retried_on_later_ticks():
    core = FakeCore()
    scheduler = Scheduler(core, [_SAT_9AM])  # never ran
    assert scheduler.tick(_local(2026, 10, 27, 12, 0)) == []  # last Saturday is stale
    assert scheduler.tick(_local(2026, 10, 27, 12, 1)) == []
    assert scheduler.tick(_local(2026, 10, 31, 9, 0)) == ["weekend_plan"]


def test_restart_after_a_run_does_not_repeat_it():
    _write_wakeup("weekend_plan", _local(2026, 10, 31, 9, 0, 5))
    core = FakeCore()
    assert Scheduler(core, [_SAT_9AM]).tick(_local(2026, 10, 31, 9, 30)) == []


def test_last_run_ignores_other_jobs():
    _write_wakeup("arrived_home", _local(2026, 10, 31, 9, 0))
    assert last_run("weekend_plan") is None
    _write_wakeup("weekend_plan", _local(2026, 10, 24, 9, 0))
    assert last_run("weekend_plan") == _local(2026, 10, 24, 9, 0)


def test_should_run_false_skips_without_submitting():
    from scheduler import MON

    core = FakeCore()
    s = Schedule(Job(name="trim_context", prompt="p"), at=time(3, 0),
                 weekdays=frozenset({MON}), should_run=lambda: False)
    scheduler = Scheduler(core, [s])
    assert scheduler.tick(_local(2026, 11, 2, 3, 0)) == []
    assert core.wakeups == []


def test_has_turns_since_last_run():
    from scheduler import has_turns_since_last_run

    assert not has_turns_since_last_run("trim_context")  # empty log
    _write_wakeup("arrived_home", _local(2026, 10, 26, 18, 0))
    assert has_turns_since_last_run("trim_context")  # never trimmed, one turn
    _write_wakeup("trim_context", _local(2026, 11, 2, 3, 0))
    assert not has_turns_since_last_run("trim_context")  # nothing since the trim
    _write_wakeup("arrived_home", _local(2026, 11, 2, 18, 0))
    assert has_turns_since_last_run("trim_context")
