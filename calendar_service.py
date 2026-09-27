from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .caldav_client import AsyncCalDAVClient
from .cursor import decode_cursor, encode_cursor, query_fingerprint
from .errors import (
    CalendarPluginError,
    ConfigurationError,
    NotFoundError,
    ResponseTooLarge,
)
from .ical_codec import (
    build_event_ical,
    cancel_occurrence_ical,
    parse_occurrences,
    parse_user_datetime,
    update_event_ical,
)
from .models import CalendarRecord, EventRecord
from .store import EventStore


@dataclass(frozen=True)
class ServiceSettings:
    username: str
    app_password: str
    base_url: str
    default_timezone: str
    creation_calendar: str
    excluded_calendars: tuple[str, ...]
    query_shard_days: int
    max_query_days: int
    max_page_size: int
    max_response_bytes: int

    @classmethod
    def from_config(cls, config: dict) -> ServiceSettings:
        username = str(config.get("apple_id", "")).strip()
        password = str(config.get("app_specific_password", "")).strip()
        if not username or not password:
            raise ConfigurationError("请先在插件配置中填写 Apple Account 和 App 专用密码。")
        timezone = str(config.get("default_timezone", "Asia/Shanghai")).strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"未知时区：{timezone}") from exc
        base_url = str(config.get("caldav_base_url", "https://caldav.icloud.com/")).strip()
        if not base_url.startswith("https://"):
            raise ConfigurationError("CalDAV 服务地址必须使用 HTTPS。")
        shard_days = max(1, min(int(config.get("query_shard_days", 7)), 90))
        max_days = max(1, min(int(config.get("max_query_days", 366)), 3660))
        max_page = max(1, min(int(config.get("max_page_size", 200)), 1000))
        max_response_mb = max(1, min(int(config.get("max_response_mb", 16)), 128))
        creation_calendar = str(config.get("creation_calendar", "")).strip()
        excluded_raw = str(config.get("excluded_calendars", ""))
        excluded_calendars = tuple(
            dict.fromkeys(
                part.strip().casefold()
                for part in re.split(r"[,，;；\r\n]+", excluded_raw)
                if part.strip()
            )
        )
        return cls(
            username,
            password,
            base_url.rstrip("/") + "/",
            timezone,
            creation_calendar,
            excluded_calendars,
            shard_days,
            max_days,
            max_page,
            max_response_mb * 1024 * 1024,
        )


def parse_boundary(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CalendarPluginError("时间范围必须包含时区偏移或 Z。")
    return parsed.astimezone(UTC)


class CalendarService:
    def __init__(
        self,
        settings: ServiceSettings,
        database_path: Path,
        client: AsyncCalDAVClient | None = None,
    ):
        self.settings = settings
        self.store = EventStore(database_path)
        self.client = client or AsyncCalDAVClient(
            settings.username,
            settings.app_password,
            settings.base_url,
            max_response_bytes=settings.max_response_bytes,
        )
        self._refresh_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.close()

    async def list_calendars(self, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            calendars = await self.client.list_calendars()
            await asyncio.to_thread(self.store.upsert_calendars, calendars)
        calendars = await asyncio.to_thread(self.store.list_calendars)
        return [
            calendar.to_dict()
            for calendar in calendars
            if not self._is_excluded(calendar)
        ]

    def _is_excluded(self, calendar: CalendarRecord) -> bool:
        calendar_id = calendar.id.casefold()
        calendar_name = calendar.name.casefold()
        return any(
            item == calendar_id or item in calendar_name
            for item in self.settings.excluded_calendars
        )

    async def _all_calendars(self, refresh_if_empty: bool = True) -> list[CalendarRecord]:
        calendars = await asyncio.to_thread(self.store.list_calendars)
        if not calendars and refresh_if_empty:
            remote = await self.client.list_calendars()
            await asyncio.to_thread(self.store.upsert_calendars, remote)
            calendars = await asyncio.to_thread(self.store.list_calendars)
        return calendars

    async def _calendars(self, calendar_ids: list[str] | None) -> list[CalendarRecord]:
        calendars = [
            calendar
            for calendar in await self._all_calendars()
            if not self._is_excluded(calendar)
        ]
        if calendar_ids:
            wanted = set(calendar_ids)
            selected = [calendar for calendar in calendars if calendar.id in wanted]
            missing = wanted - {calendar.id for calendar in selected}
            if missing:
                raise NotFoundError(f"未知或已过滤的日历 ID：{', '.join(sorted(missing))}")
            calendars = selected
        if not calendars:
            raise NotFoundError("没有可查询的日历；请检查“过滤日历”插件设置。")
        return calendars

    async def _creation_target(self) -> CalendarRecord:
        target = self.settings.creation_calendar.strip()
        if not target:
            raise ConfigurationError(
                "请先在插件配置中填写“新建日程目标日历”（日历名称或 ID）。"
            )
        target_folded = target.casefold()

        def resolve(calendars: list[CalendarRecord]) -> CalendarRecord | None:
            by_id = [
                calendar
                for calendar in calendars
                if calendar.id.casefold() == target_folded
            ]
            if by_id:
                return by_id[0]
            by_name = [
                calendar
                for calendar in calendars
                if calendar.name.casefold() == target_folded
            ]
            if len(by_name) == 1:
                return by_name[0]
            if len(by_name) > 1:
                raise ConfigurationError(
                    f"存在多个名为“{target}”的日历，请在配置中改填唯一 calendar_id。"
                )
            return None

        calendars = await self._all_calendars()
        if match := resolve(calendars):
            return match
        await self.list_calendars(refresh=True)
        calendars = await self._all_calendars(refresh_if_empty=False)
        if match := resolve(calendars):
            return match
        raise NotFoundError(f"找不到配置的新建日程目标日历：{target}")

    def _validate_range(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise CalendarPluginError("end 必须晚于 start。")
        if end - start > timedelta(days=self.settings.max_query_days):
            raise CalendarPluginError(
                f"单次查询最多 {self.settings.max_query_days} 天，请拆分为连续时间窗口。"
            )

    async def _refresh_slice(
        self,
        calendar: CalendarRecord,
        start: datetime,
        end: datetime,
        depth: int = 0,
    ) -> tuple[int, int]:
        try:
            objects = await self.client.query_objects(calendar, start, end)
        except ResponseTooLarge:
            if end - start <= timedelta(hours=1) or depth >= 16:
                raise CalendarPluginError(
                    "一小时日程片段仍超过响应上限，请缩小范围或只选择一个日历。"
                )
            middle = start + (end - start) / 2
            left = await self._refresh_slice(calendar, start, middle, depth + 1)
            right = await self._refresh_slice(calendar, middle, end, depth + 1)
            return left[0] + right[0], left[1] + right[1]
        events: list[EventRecord] = []
        for item in objects:
            occurrences = await asyncio.to_thread(
                parse_occurrences,
                calendar,
                item.href,
                item.etag,
                item.ical,
                start,
                end,
                self.settings.default_timezone,
            )
            events.extend(occurrences)
        await asyncio.to_thread(
            self.store.replace_range,
            calendar.id,
            start.isoformat(),
            end.isoformat(),
            events,
        )
        return len(objects), len(events)

    async def refresh_index(
        self,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        start_dt, end_dt = parse_boundary(start), parse_boundary(end)
        self._validate_range(start_dt, end_dt)
        calendars = await self._calendars(calendar_ids)
        resources = occurrences = slices = 0
        shard = timedelta(days=self.settings.query_shard_days)
        async with self._refresh_lock:
            for calendar in calendars:
                current = start_dt
                while current < end_dt:
                    shard_end = min(end_dt, current + shard)
                    resource_count, occurrence_count = await self._refresh_slice(
                        calendar, current, shard_end
                    )
                    resources += resource_count
                    occurrences += occurrence_count
                    slices += 1
                    current = shard_end
        return {
            "calendar_count": len(calendars),
            "time_slices": slices,
            "resources_received": resources,
            "occurrences_indexed": occurrences,
            "range_start": start_dt.isoformat(),
            "range_end": end_dt.isoformat(),
        }

    async def list_events(
        self,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
        query: str = "",
        limit: int = 50,
        cursor: str | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        start_dt, end_dt = parse_boundary(start), parse_boundary(end)
        self._validate_range(start_dt, end_dt)
        if limit < 1 or limit > self.settings.max_page_size:
            raise CalendarPluginError(
                f"limit 必须在 1 到 {self.settings.max_page_size} 之间。"
            )
        calendars = await self._calendars(calendar_ids)
        ids = [calendar.id for calendar in calendars]
        normalized_query = query.strip()
        fingerprint = query_fingerprint(
            ids, start_dt.isoformat(), end_dt.isoformat(), normalized_query
        )
        after_start = after_id = None
        if cursor:
            payload = decode_cursor(cursor)
            if payload.get("v") != 1 or payload.get("fingerprint") != fingerprint:
                raise CalendarPluginError("该游标不属于当前查询条件。")
            after_start = payload.get("after_start")
            after_id = payload.get("after_id")
        if refresh and not cursor:
            await self.refresh_index(start_dt.isoformat(), end_dt.isoformat(), ids)
        events, has_more = await asyncio.to_thread(
            self.store.list_events,
            ids,
            start_dt.isoformat(),
            end_dt.isoformat(),
            normalized_query,
            limit,
            after_start,
            after_id,
        )
        next_cursor = None
        if has_more and events:
            last = events[-1]
            next_cursor = encode_cursor(
                {
                    "v": 1,
                    "fingerprint": fingerprint,
                    "after_start": last.start_utc,
                    "after_id": last.id,
                }
            )
        return {
            "events": [event.to_public_dict() for event in events],
            "count": len(events),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "refreshed": bool(refresh and not cursor),
        }

    async def _cached_event(self, event_id: str) -> EventRecord:
        event = await asyncio.to_thread(self.store.get_event, event_id)
        if not event:
            raise NotFoundError("未知日程 ID。请先查询包含该日程的时间范围。")
        calendar = await asyncio.to_thread(self.store.get_calendar, event.calendar_id)
        if calendar and self._is_excluded(calendar):
            raise NotFoundError("该日程来自已过滤的日历，不能返回给 AI。")
        return event

    async def get_event(self, event_id: str, include_ical: bool = False) -> dict[str, Any]:
        cached = await self._cached_event(event_id)
        remote = await self.client.get_object(cached.href)
        calendar = await asyncio.to_thread(self.store.get_calendar, cached.calendar_id)
        if not calendar:
            raise NotFoundError("该日程所属日历已不在本地索引中。")
        start = datetime.fromisoformat(cached.start_utc) - timedelta(days=1)
        end = datetime.fromisoformat(cached.end_utc) + timedelta(days=1)
        current = await asyncio.to_thread(
            parse_occurrences,
            calendar,
            remote.href,
            remote.etag,
            remote.ical,
            start,
            end,
            self.settings.default_timezone,
        )
        match = next(
            (
                item
                for item in current
                if item.recurrence_id == cached.recurrence_id
                or item.start_utc == cached.start_utc
            ),
            None,
        )
        if not match:
            raise NotFoundError("所选循环日程发生项已不存在。")
        await asyncio.to_thread(self.store.upsert_events, [match])
        return match.to_public_dict(include_ical)

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        timezone: str | None = None,
        all_day: bool = False,
        description: str = "",
        location: str = "",
        status: str = "CONFIRMED",
        transparency: str = "OPAQUE",
    ) -> dict[str, Any]:
        calendar = await self._creation_target()
        timezone_name = timezone or self.settings.default_timezone
        raw_ical, uid = await asyncio.to_thread(
            build_event_ical,
            summary=summary,
            start=start,
            end=end,
            timezone_name=timezone_name,
            all_day=all_day,
            description=description,
            location=location,
            status=status,
            transparency=transparency,
        )
        async with self._mutation_lock:
            created = await self.client.create_object(calendar, uid, raw_ical)

        def as_utc(value: str) -> datetime:
            parsed = parse_user_datetime(value, timezone_name, all_day)
            if isinstance(parsed, datetime):
                return parsed.astimezone(UTC)
            return datetime.combine(parsed, time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)

        start_dt, end_dt = as_utc(start), as_utc(end)
        occurrences = await asyncio.to_thread(
            parse_occurrences,
            calendar,
            created.href,
            created.etag,
            raw_ical,
            start_dt - timedelta(seconds=1),
            end_dt + timedelta(seconds=1),
            timezone_name,
        )
        await asyncio.to_thread(self.store.upsert_events, occurrences)
        return {
            "created": True,
            "event": occurrences[0].to_public_dict() if occurrences else {"uid": uid},
        }

    async def update_event(
        self,
        event_id: str,
        etag: str,
        scope: str = "series",
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        status: str | None = None,
        transparency: str | None = None,
        clear_description: bool = False,
        clear_location: bool = False,
    ) -> dict[str, Any]:
        cached = await self._cached_event(event_id)
        if scope not in {"series", "occurrence"}:
            raise CalendarPluginError("scope 只能是 series 或 occurrence。")
        if scope == "occurrence" and not cached.recurrence_id:
            raise CalendarPluginError("非循环日程不能使用 occurrence 范围。")
        remote = await self.client.get_object(cached.href)
        updated_ical = await asyncio.to_thread(
            update_event_ical,
            remote.ical,
            cached,
            scope=scope,
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            status=status,
            transparency=transparency,
            clear_description=clear_description,
            clear_location=clear_location,
        )
        async with self._mutation_lock:
            result = await self.client.update_object(cached.href, etag, updated_ical)
        await asyncio.to_thread(self.store.delete_href, cached.calendar_id, cached.href)
        return {
            "updated": True,
            "scope": scope,
            "uid": cached.uid,
            "etag": result.etag,
            "message": "本地缓存已失效，下次查询会重新获取。",
        }

    async def delete_event(
        self, event_id: str, etag: str, scope: str = "series"
    ) -> dict[str, Any]:
        cached = await self._cached_event(event_id)
        if scope not in {"series", "occurrence"}:
            raise CalendarPluginError("scope 只能是 series 或 occurrence。")
        async with self._mutation_lock:
            if scope == "occurrence":
                if not cached.recurrence_id:
                    raise CalendarPluginError("非循环日程不能删除单次发生项。")
                remote = await self.client.get_object(cached.href)
                cancelled = await asyncio.to_thread(
                    cancel_occurrence_ical, remote.ical, cached
                )
                result = await self.client.update_object(cached.href, etag, cancelled)
                response = {"deleted": True, "scope": scope, "etag": result.etag}
            else:
                await self.client.delete_object(cached.href, etag)
                response = {"deleted": True, "scope": scope, "uid": cached.uid}
        await asyncio.to_thread(self.store.delete_href, cached.calendar_id, cached.href)
        return response

    async def index_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.status)
