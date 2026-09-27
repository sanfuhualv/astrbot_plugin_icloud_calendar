from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar, Event

from .models import CalendarRecord, EventRecord


def _as_datetime(value: date | datetime, timezone: ZoneInfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        return value, False
    return datetime.combine(value, time.min, tzinfo=timezone), True


def _property_text(component: Event, name: str) -> str:
    value = component.get(name)
    return "" if value is None else str(value)


def _attendees(component: Event) -> list[str]:
    values = component.get("ATTENDEE")
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return [str(item) for item in values]


def _event_id(calendar_id: str, href: str, recurrence_id: str, start_utc: str) -> str:
    raw = f"{calendar_id}\0{href}\0{recurrence_id}\0{start_utc}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def parse_occurrences(
    calendar_record: CalendarRecord,
    href: str,
    etag: str | None,
    raw_ical: str,
    start: datetime,
    end: datetime,
    default_timezone: str,
) -> list[EventRecord]:
    """将一个 CalDAV iCalendar 资源展开为给定窗口内的发生项。"""
    parsed = Calendar.from_ical(raw_ical)
    timezone = ZoneInfo(default_timezone)
    try:
        components = recurring_ical_events.of(parsed).between(start, end)
    except Exception:  # noqa: BLE001 - malformed remote recurrence falls back to VEVENTs
        components = [component for component in parsed.walk("VEVENT")]
    now = datetime.now(UTC).isoformat()
    records: list[EventRecord] = []
    for component in components:
        if not isinstance(component, Event):
            continue
        dtstart_prop = component.get("DTSTART")
        if dtstart_prop is None:
            continue
        start_dt, all_day = _as_datetime(dtstart_prop.dt, timezone)
        dtend_prop = component.get("DTEND")
        if dtend_prop is not None:
            end_dt, _ = _as_datetime(dtend_prop.dt, timezone)
        elif component.get("DURATION") is not None:
            end_dt = start_dt + component.decoded("DURATION")
        else:
            end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
        start_utc = start_dt.astimezone(UTC).isoformat()
        end_utc = end_dt.astimezone(UTC).isoformat()
        recurrence_prop = component.get("RECURRENCE-ID")
        recurrence_id = ""
        if recurrence_prop is not None:
            recurrence_dt, _ = _as_datetime(recurrence_prop.dt, timezone)
            recurrence_id = recurrence_dt.astimezone(UTC).isoformat()
        timezone_name = getattr(start_dt.tzinfo, "key", None) or str(
            start_dt.tzinfo or default_timezone
        )
        records.append(
            EventRecord(
                id=_event_id(calendar_record.id, href, recurrence_id, start_utc),
                calendar_id=calendar_record.id,
                calendar_name=calendar_record.name,
                href=href,
                etag=etag,
                uid=_property_text(component, "UID"),
                recurrence_id=recurrence_id,
                start_utc=start_utc,
                end_utc=end_utc,
                start_local=start_dt.isoformat(),
                end_local=end_dt.isoformat(),
                timezone=timezone_name,
                all_day=all_day,
                summary=_property_text(component, "SUMMARY"),
                description=_property_text(component, "DESCRIPTION"),
                location=_property_text(component, "LOCATION"),
                status=_property_text(component, "STATUS"),
                transparency=_property_text(component, "TRANSP"),
                url=_property_text(component, "URL"),
                organizer=_property_text(component, "ORGANIZER"),
                attendees_json=json.dumps(_attendees(component), ensure_ascii=False),
                raw_ical=raw_ical,
                updated_at=now,
            )
        )
    return records


def parse_user_datetime(value: str, timezone_name: str, all_day: bool = False) -> date | datetime:
    timezone = ZoneInfo(timezone_name)
    if all_day:
        return date.fromisoformat(value[:10])
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def build_event_ical(
    *,
    summary: str,
    start: str,
    end: str,
    timezone_name: str,
    all_day: bool,
    description: str = "",
    location: str = "",
    status: str = "CONFIRMED",
    transparency: str = "OPAQUE",
) -> tuple[str, str]:
    uid = f"{uuid.uuid4()}@astrbot.local"
    calendar = Calendar()
    calendar.add("PRODID", "-//astrbot_plugin_icloud_calendar//CalDAV//EN")
    calendar.add("VERSION", "2.0")
    event = Event()
    now = datetime.now(UTC)
    event.add("UID", uid)
    event.add("DTSTAMP", now)
    event.add("CREATED", now)
    event.add("LAST-MODIFIED", now)
    event.add("SEQUENCE", 0)
    event.add("SUMMARY", summary)
    event.add("DTSTART", parse_user_datetime(start, timezone_name, all_day))
    event.add("DTEND", parse_user_datetime(end, timezone_name, all_day))
    if description:
        event.add("DESCRIPTION", description)
    if location:
        event.add("LOCATION", location)
    event.add("STATUS", status.upper())
    event.add("TRANSP", transparency.upper())
    calendar.add_component(event)
    return calendar.to_ical().decode(), uid


def _master_event(calendar: Calendar) -> Event:
    events = [component for component in calendar.walk("VEVENT") if isinstance(component, Event)]
    for event in events:
        if event.get("RECURRENCE-ID") is None:
            return event
    if not events:
        raise ValueError("Calendar object contains no VEVENT")
    return events[0]


def _replace_property(component: Event, name: str, value: Any) -> None:
    if name in component:
        del component[name]
    component.add(name, value)


def update_event_ical(
    raw_ical: str,
    cached: EventRecord,
    *,
    scope: str,
    summary: str | None,
    start: str | None,
    end: str | None,
    description: str | None,
    location: str | None,
    status: str | None,
    transparency: str | None,
    clear_description: bool,
    clear_location: bool,
) -> str:
    calendar = Calendar.from_ical(raw_ical)
    master = _master_event(calendar)
    target = master
    if scope == "occurrence" and cached.recurrence_id:
        target = None
        for component in calendar.walk("VEVENT"):
            recurrence = component.get("RECURRENCE-ID")
            if recurrence is None:
                continue
            recurrence_dt, _ = _as_datetime(recurrence.dt, ZoneInfo(cached.timezone))
            if recurrence_dt.astimezone(UTC).isoformat() == cached.recurrence_id:
                target = component
                break
        if target is None:
            target = deepcopy(master)
            for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                if key in target:
                    del target[key]
            target.add("RECURRENCE-ID", datetime.fromisoformat(cached.recurrence_id))
            calendar.add_component(target)
    if summary is not None:
        _replace_property(target, "SUMMARY", summary)
    if start is not None:
        _replace_property(target, "DTSTART", parse_user_datetime(start, cached.timezone, cached.all_day))
    if end is not None:
        _replace_property(target, "DTEND", parse_user_datetime(end, cached.timezone, cached.all_day))
    if description is not None:
        _replace_property(target, "DESCRIPTION", description)
    elif clear_description and "DESCRIPTION" in target:
        del target["DESCRIPTION"]
    if location is not None:
        _replace_property(target, "LOCATION", location)
    elif clear_location and "LOCATION" in target:
        del target["LOCATION"]
    if status is not None:
        _replace_property(target, "STATUS", status.upper())
    if transparency is not None:
        _replace_property(target, "TRANSP", transparency.upper())
    _replace_property(target, "LAST-MODIFIED", datetime.now(UTC))
    _replace_property(target, "SEQUENCE", int(target.get("SEQUENCE", 0)) + 1)
    return calendar.to_ical().decode()


def cancel_occurrence_ical(raw_ical: str, cached: EventRecord) -> str:
    return update_event_ical(
        raw_ical,
        cached,
        scope="occurrence",
        summary=None,
        start=None,
        end=None,
        description=None,
        location=None,
        status="CANCELLED",
        transparency=None,
        clear_description=False,
        clear_location=False,
    )
