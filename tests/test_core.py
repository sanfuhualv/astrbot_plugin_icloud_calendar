import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from astrbot_plugin_icloud_calendar.calendar_service import (
    CalendarService,
    ServiceSettings,
)
from astrbot_plugin_icloud_calendar.cursor import decode_cursor, encode_cursor
from astrbot_plugin_icloud_calendar.errors import CalendarPluginError, ResponseTooLarge
from astrbot_plugin_icloud_calendar.ical_codec import (
    build_event_ical,
    parse_occurrences,
    update_event_ical,
)
from astrbot_plugin_icloud_calendar.models import CalendarRecord, EventRecord
from astrbot_plugin_icloud_calendar.store import EventStore


def make_event(number: int) -> EventRecord:
    start = datetime(2026, 9, 22, number, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    return EventRecord(
        id=f"event-{number}",
        calendar_id="cal",
        calendar_name="工作",
        href=f"https://example.test/{number}.ics",
        etag=f'"{number}"',
        uid=f"uid-{number}",
        recurrence_id="",
        start_utc=start.isoformat(),
        end_utc=end.isoformat(),
        start_local=start.isoformat(),
        end_local=end.isoformat(),
        timezone="UTC",
        all_day=False,
        summary=f"项目 {number}",
        description="说明",
        location="",
        status="CONFIRMED",
        transparency="OPAQUE",
        url="",
        organizer="",
        attendees_json=json.dumps([]),
        raw_ical="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        updated_at=datetime.now(UTC).isoformat(),
    )


def test_cursor_round_trip_and_rejects_bad_value():
    payload = {"v": 1, "fingerprint": "abc", "after_id": "事件"}
    assert decode_cursor(encode_cursor(payload)) == payload
    with pytest.raises(CalendarPluginError):
        decode_cursor("not-valid")


def test_store_keyset_pagination(tmp_path):
    store = EventStore(tmp_path / "index.sqlite3")
    store.upsert_calendars([CalendarRecord("cal", "https://example.test/cal/", "工作")])
    store.upsert_events([make_event(1), make_event(2), make_event(3)])
    first, more = store.list_events(
        ["cal"], "2026-09-22T00:00:00+00:00", "2026-09-23T00:00:00+00:00",
        "项目", 2, None, None,
    )
    assert [event.id for event in first] == ["event-1", "event-2"]
    assert more is True
    second, more = store.list_events(
        ["cal"], "2026-09-22T00:00:00+00:00", "2026-09-23T00:00:00+00:00",
        "项目", 2, first[-1].start_utc, first[-1].id,
    )
    assert [event.id for event in second] == ["event-3"]
    assert more is False


def test_ical_create_parse_and_update():
    raw, _ = build_event_ical(
        summary="原题目", start="2026-09-23T09:00:00+08:00",
        end="2026-09-23T10:00:00+08:00", timezone_name="Asia/Shanghai",
        all_day=False,
    )
    calendar = CalendarRecord("cal", "https://example.test/cal/", "工作")
    cached = parse_occurrences(
        calendar, "https://example.test/cal/e.ics", '"etag-1"', raw,
        datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 9, 24, tzinfo=UTC),
        "Asia/Shanghai",
    )[0]
    updated = update_event_ical(
        raw, cached, scope="series", summary="新题目",
        start="2026-09-23T11:00:00+08:00", end="2026-09-23T12:00:00+08:00",
        description=None, location=None, status=None, transparency=None,
        clear_description=False, clear_location=False,
    )
    parsed = parse_occurrences(
        calendar, "https://example.test/cal/e.ics", '"etag-2"', updated,
        datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 9, 24, tzinfo=UTC),
        "Asia/Shanghai",
    )[0]
    assert parsed.summary == "新题目"
    assert parsed.start_utc == "2026-09-23T03:00:00+00:00"


def test_large_remote_window_is_split_until_bounded(tmp_path):
    class SplittingClient:
        def __init__(self):
            self.windows = []

        async def query_objects(self, _calendar, start, end):
            self.windows.append((start, end))
            if end - start > timedelta(days=1):
                raise ResponseTooLarge("mock response is too large")
            return []

        async def close(self):
            return None

    settings = ServiceSettings(
        username="apple@example.com", app_password="app-password",
        base_url="https://caldav.icloud.com/", default_timezone="Asia/Shanghai",
        creation_calendar="工作", excluded_calendars=(), query_shard_days=30,
        max_query_days=366, max_page_size=200, max_response_bytes=1024,
    )
    client = SplittingClient()
    service = CalendarService(settings, tmp_path / "index.sqlite3", client=client)
    service.store.upsert_calendars(
        [CalendarRecord("cal", "https://example.test/cal/", "工作")]
    )
    result = asyncio.run(service.refresh_index(
        "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z", ["cal"]
    ))
    assert len(client.windows) == 7
    assert sum(end - start <= timedelta(days=1) for start, end in client.windows) == 4
    assert result["occurrences_indexed"] == 0


def test_settings_parse_calendar_target_and_filter_keywords():
    settings = ServiceSettings.from_config({
        "apple_id": "apple@example.com", "app_specific_password": "app-password",
        "creation_calendar": "个人", "excluded_calendars": "自动化，订阅\nSpam;自动化",
    })
    assert settings.creation_calendar == "个人"
    assert settings.excluded_calendars == ("自动化", "订阅", "spam")


def test_filtered_calendar_never_reaches_list_results(tmp_path):
    settings = ServiceSettings(
        username="apple@example.com", app_password="app-password",
        base_url="https://caldav.icloud.com/", default_timezone="Asia/Shanghai",
        creation_calendar="工作", excluded_calendars=("自动化",), query_shard_days=7,
        max_query_days=366, max_page_size=200, max_response_bytes=1024,
    )
    service = CalendarService(
        settings, tmp_path / "index.sqlite3", client=SimpleNamespace(close=lambda: None)
    )
    service.store.upsert_calendars([
        CalendarRecord("work", "https://example.test/work/", "工作"),
        CalendarRecord("auto", "https://example.test/auto/", "家庭自动化"),
    ])
    service.store.upsert_events([
        replace(make_event(1), calendar_id="work", calendar_name="工作"),
        replace(make_event(2), calendar_id="auto", calendar_name="家庭自动化"),
    ])
    calendars = asyncio.run(service.list_calendars(refresh=False))
    result = asyncio.run(service.list_events(
        "2026-09-22T00:00:00Z", "2026-09-23T00:00:00Z", refresh=False
    ))
    assert [calendar["id"] for calendar in calendars] == ["work"]
    assert [event["calendar_id"] for event in result["events"]] == ["work"]


def test_create_always_uses_configured_calendar(tmp_path):
    class CreationClient:
        def __init__(self):
            self.calendar = None

        async def create_object(self, calendar, uid, raw_ical):
            self.calendar = calendar
            return SimpleNamespace(href=f"{calendar.href}{uid}.ics", etag='"new"', ical=raw_ical)

        async def close(self):
            return None

    settings = ServiceSettings(
        username="apple@example.com", app_password="app-password",
        base_url="https://caldav.icloud.com/", default_timezone="Asia/Shanghai",
        creation_calendar="个人", excluded_calendars=(), query_shard_days=7,
        max_query_days=366, max_page_size=200, max_response_bytes=1024,
    )
    client = CreationClient()
    service = CalendarService(settings, tmp_path / "index.sqlite3", client=client)
    service.store.upsert_calendars([
        CalendarRecord("work", "https://example.test/work/", "工作"),
        CalendarRecord("personal", "https://example.test/personal/", "个人"),
    ])
    asyncio.run(service.create_event(
        "固定目标测试", "2026-09-23T09:00:00+08:00", "2026-09-23T10:00:00+08:00"
    ))
    assert client.calendar.id == "personal"
