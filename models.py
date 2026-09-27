from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class CalendarRecord:
    id: str
    href: str
    name: str
    color: str | None = None
    ctag: str | None = None
    sync_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EventRecord:
    id: str
    calendar_id: str
    calendar_name: str
    href: str
    etag: str | None
    uid: str
    recurrence_id: str
    start_utc: str
    end_utc: str
    start_local: str
    end_local: str
    timezone: str
    all_day: bool
    summary: str
    description: str
    location: str
    status: str
    transparency: str
    url: str
    organizer: str
    attendees_json: str
    raw_ical: str
    updated_at: str

    def to_public_dict(self, include_ical: bool = False) -> dict[str, Any]:
        import json

        result: dict[str, Any] = {
            "id": self.id,
            "calendar_id": self.calendar_id,
            "calendar_name": self.calendar_name,
            "etag": self.etag,
            "uid": self.uid,
            "recurrence_id": self.recurrence_id or None,
            "start": self.start_local,
            "end": self.end_local,
            "timezone": self.timezone,
            "all_day": self.all_day,
            "summary": self.summary,
            "description": self.description,
            "location": self.location,
            "status": self.status,
            "transparency": self.transparency,
            "url": self.url,
            "organizer": self.organizer,
            "attendees": json.loads(self.attendees_json or "[]"),
        }
        if include_ical:
            result["ical"] = self.raw_ical
        return result
