"""Tests for the iCloud calendar layer's offline parts: parsing, building events and formatting."""

from datetime import date, datetime, timedelta

import pytest

from integrations.icloud_calendar import TZ, build_event, event_dict, parse_when
from tools.server import format_event


def _roundtrip(ical):
    """Event dict as read back from the serialized iCalendar text, the way CalDAV returns it."""
    from icalendar import Calendar
    return event_dict(Calendar.from_ical(ical.to_ical()).walk("VEVENT")[0], "Home")


def test_parse_when():
    assert parse_when("2026-11-07") == date(2026, 11, 7)
    assert parse_when("2026-11-07T10:30") == datetime(2026, 11, 7, 10, 30, tzinfo=TZ)
    # An explicit offset is converted to local time.
    assert parse_when("2026-11-07T16:30+00:00") == datetime(2026, 11, 7, 10, 30, tzinfo=TZ)
    with pytest.raises(ValueError):
        parse_when("next tuesday")


def test_timed_event_defaults_to_one_hour_and_keeps_local_time():
    e = _roundtrip(build_event("Mirror", datetime(2026, 11, 7, 10, 0, tzinfo=TZ)))
    assert (e["title"], e["all_day"], e["recurring"]) == ("Mirror", False, False)
    assert e["start"] == datetime(2026, 11, 7, 10, 0, tzinfo=TZ)
    assert e["end"] - e["start"] == timedelta(hours=1)
    assert e["uid"].endswith("@personal-assistant")


def test_all_day_event_defaults_to_one_day():
    e = _roundtrip(build_event("Blood work", date(2026, 11, 5)))
    assert e["all_day"]
    assert (e["start"], e["end"]) == (date(2026, 11, 5), date(2026, 11, 6))


def test_alert_and_location():
    ical = build_event("Call", datetime(2026, 11, 7, 10, 0, tzinfo=TZ), alert_minutes=30, location="Home")
    alarm = ical.walk("VALARM")[0]
    assert alarm.decoded("TRIGGER") == timedelta(minutes=-30)
    assert _roundtrip(ical)["location"] == "Home"


def test_build_event_rejects_bad_ranges():
    start = datetime(2026, 11, 7, 10, 0, tzinfo=TZ)
    with pytest.raises(ValueError, match="after start"):
        build_event("x", start, start)
    with pytest.raises(ValueError, match="both"):
        build_event("x", date(2026, 11, 7), start)


def _event(start, end, all_day=False, **kw):
    return {"uid": "u1", "calendar": "Home", "title": "T", "start": start, "end": end,
            "all_day": all_day, "location": None, "recurring": False, **kw}


def test_format_event():
    t = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=TZ)
    assert format_event(_event(t(7, 10), t(7, 11, 30))) == "Sat Nov 7, 10:00 AM–11:30 AM | T | Home | uid=u1"
    assert format_event(_event(t(7, 22), t(8, 1))).startswith("Sat Nov 7, 10:00 PM – Sun Nov 8, 1:00 AM")
    assert format_event(_event(date(2026, 11, 7), date(2026, 11, 8), all_day=True)).startswith("Sat Nov 7, all day")
    assert format_event(_event(date(2026, 11, 7), date(2026, 11, 9), all_day=True)).startswith("Sat Nov 7–Sun Nov 8, all day")
    assert "| recurring |" in format_event(_event(t(7, 10), t(7, 11), recurring=True, location=None))
