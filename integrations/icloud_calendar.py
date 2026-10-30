"""
iCloud calendar access over CalDAV.

Reads ICLOUD_USERNAME and ICLOUD_APP_PASSWORD (an app-specific password from
account.apple.com) from the environment. New events go to
ICLOUD_DEFAULT_CALENDAR (default "Home") unless a calendar is named.

Times in and out are local (TIMEZONE, default America/Chicago). A date without
a time means an all-day event. Recurring events are expanded into their
occurrences when listed, and can't be deleted here (that would delete the
whole series).

Like db/, this layer returns plain dicts; formatting for Claude lives in tools/.
"""

import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import caldav
from icalendar import Alarm, Calendar, Event

_URL = "https://caldav.icloud.com"
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

_client: caldav.DAVClient | None = None


class NotConfigured(RuntimeError):
    pass


def default_calendar() -> str:
    return os.environ.get("ICLOUD_DEFAULT_CALENDAR", "Home")


def _calendars() -> dict[str, caldav.Calendar]:
    """Event calendars by display name. Task-only lists (old Reminders) are left out."""
    global _client
    if _client is None:
        user = os.environ.get("ICLOUD_USERNAME")
        password = os.environ.get("ICLOUD_APP_PASSWORD")
        if not user or not password:
            raise NotConfigured("iCloud calendar is not configured (ICLOUD_USERNAME / ICLOUD_APP_PASSWORD).")
        _client = caldav.DAVClient(_URL, username=user, password=password)
    result = {}
    for cal in _client.principal().calendars():
        if "VEVENT" in cal.get_supported_components():
            result[cal.get_display_name()] = cal
    return result


def _calendar(name: str) -> caldav.Calendar:
    cals = _calendars()
    if name not in cals:
        raise ValueError(f"No calendar named {name!r}. Calendars: {', '.join(cals)}")
    return cals[name]


def parse_when(text: str) -> date | datetime:
    """'2026-11-01' -> a date (all day); '2026-11-01T10:00' -> an aware local datetime."""
    text = text.strip()
    if len(text) == 10:
        return date.fromisoformat(text)
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def _local(value: date | datetime) -> date | datetime:
    """Event times as stored: all-day dates stay dates; datetimes become local."""
    if isinstance(value, datetime):
        # Floating times (no tzinfo) are already local.
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    return value


def event_dict(component, calendar: str) -> dict:
    """A VEVENT component as a plain dict."""
    start = _local(component.decoded("DTSTART"))
    if "DTEND" in component:
        end = _local(component.decoded("DTEND"))
    elif "DURATION" in component:
        end = start + component.decoded("DURATION")
    else:
        end = start + (timedelta(days=1) if not isinstance(start, datetime) else timedelta(0))
    return {
        "uid": str(component.get("UID")),
        "calendar": calendar,
        "title": str(component.get("SUMMARY", "(no title)")),
        "start": start,
        "end": end,
        "all_day": not isinstance(start, datetime),
        "location": str(component["LOCATION"]) if "LOCATION" in component else None,
        "recurring": "RRULE" in component or "RECURRENCE-ID" in component,
    }


def _sort_key(e: dict) -> datetime:
    s = e["start"]
    return s if isinstance(s, datetime) else datetime.combine(s, time.min, tzinfo=TZ)


def list_events(start: date, end: date, calendar: str | None = None) -> list[dict]:
    """Events overlapping [start, end) (local dates), recurring ones expanded, sorted by start."""
    cals = {calendar: _calendar(calendar)} if calendar else _calendars()
    range_start = datetime.combine(start, time.min, tzinfo=TZ)
    range_end = datetime.combine(end, time.min, tzinfo=TZ)
    events = []
    for name, cal in cals.items():
        for obj in cal.search(start=range_start, end=range_end, event=True, expand=True):
            for component in obj.icalendar_instance.walk("VEVENT"):
                events.append(event_dict(component, name))
    return sorted(events, key=_sort_key)


def build_event(
    title: str,
    start: date | datetime,
    end: date | datetime | None = None,
    alert_minutes: int | None = None,
    location: str | None = None,
    notes: str | None = None,
) -> Calendar:
    """An iCalendar object holding one new event. end defaults to 1 hour, or 1 day for all-day."""
    if end is None:
        end = start + (timedelta(hours=1) if isinstance(start, datetime) else timedelta(days=1))
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ValueError("start and end must both be dates or both be date-times")
    if end <= start:
        raise ValueError("end must be after start")

    event = Event()
    event.add("uid", f"{uuid.uuid4()}@personal-assistant")
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", title)
    event.add("dtstart", start)
    event.add("dtend", end)
    if location:
        event.add("location", location)
    if notes:
        event.add("description", notes)
    if alert_minutes is not None:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", title)
        alarm.add("trigger", timedelta(minutes=-alert_minutes))
        event.add_component(alarm)

    ical = Calendar()
    ical.add("prodid", "-//Personal Assistant//EN")
    ical.add("version", "2.0")
    ical.add_component(event)
    return ical


def add_event(title: str, start: date | datetime, end: date | datetime | None = None,
              calendar: str | None = None, alert_minutes: int | None = None,
              location: str | None = None, notes: str | None = None) -> dict:
    """Create an event. Returns it as a dict."""
    name = calendar or default_calendar()
    ical = build_event(title, start, end, alert_minutes, location, notes)
    _calendar(name).add_event(ical.to_ical().decode())
    return event_dict(ical.walk("VEVENT")[0], name)


def delete_event(uid: str) -> dict | None:
    """Delete a one-off event by UID. Returns the deleted event, or None if not found.

    Raises ValueError for a recurring event.
    """
    for name, cal in _calendars().items():
        try:
            obj = cal.event_by_uid(uid)
        except caldav.error.NotFoundError:
            continue
        component = obj.icalendar_instance.walk("VEVENT")[0]
        event = event_dict(component, name)
        if event["recurring"]:
            raise ValueError("That's a recurring event; delete it in the Calendar app.")
        obj.delete()
        return event
    return None
