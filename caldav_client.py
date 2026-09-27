from __future__ import annotations

import asyncio
import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse

import httpx

from .errors import (
    AuthenticationError,
    CalendarPluginError,
    ConflictError,
    NotFoundError,
    ResponseTooLarge,
)
from .models import CalendarRecord

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"
ICAL = "http://apple.com/ns/ical/"
NS = {"d": DAV, "c": CALDAV, "cs": CS, "i": ICAL}


@dataclass(slots=True)
class CalendarObject:
    href: str
    etag: str | None
    ical: str


@dataclass(slots=True)
class ResponseData:
    status_code: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _xml_text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _multistatus(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise CalendarPluginError("iCloud 返回了无效的 CalDAV XML") from exc


def _successful_props(response: ET.Element) -> ET.Element | None:
    for propstat in response.findall("d:propstat", NS):
        if " 200 " in (_xml_text(propstat, "d:status") or ""):
            return propstat.find("d:prop", NS)
    return None


class AsyncCalDAVClient:
    """异步 CalDAV 客户端，带重试、条件写入和响应内存上限。"""

    def __init__(
        self,
        username: str,
        app_password: str,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = 16 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            auth=(username, app_password),
            headers={"User-Agent": "astrbot_plugin_icloud_calendar/0.2.0"},
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._calendar_home: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: str | bytes | None = None,
        expected: Iterable[int] = (200, 201, 204, 207),
        max_bytes: int | None = None,
    ) -> ResponseData:
        limit = max_bytes or self.max_response_bytes
        expected_set = set(expected)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with self._client.stream(
                    method, url, headers=headers, content=content
                ) as response:
                    if response.status_code in (401, 403):
                        raise AuthenticationError(
                            "iCloud 登录失败，请检查 Apple Account 和 App 专用密码。"
                        )
                    if response.status_code == 412:
                        raise ConflictError(
                            "日程已在其他设备上被修改。请重新读取日程并使用新的 ETag。"
                        )
                    if response.status_code == 404:
                        raise NotFoundError("所请求的 iCloud 日历对象已不存在")
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                        retry_after = response.headers.get("Retry-After")
                        delay = 0.5 * (2**attempt)
                        if retry_after:
                            try:
                                delay = float(retry_after)
                            except ValueError:
                                retry_time = parsedate_to_datetime(retry_after)
                                delay = max(
                                    0.0,
                                    (retry_time - datetime.now().astimezone()).total_seconds(),
                                )
                        await response.aread()
                        await asyncio.sleep(min(delay, 8.0))
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ResponseTooLarge(
                                f"CalDAV 响应超过 {limit} 字节，需要缩小时间范围"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if response.status_code not in expected_set:
                        detail = body[:500].decode("utf-8", errors="replace")
                        raise CalendarPluginError(
                            f"CalDAV {method} 请求失败，HTTP {response.status_code}: {detail}"
                        )
                    return ResponseData(response.status_code, response.headers, body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
        raise CalendarPluginError(f"无法连接 iCloud CalDAV：{last_error}")

    async def _propfind(self, url: str, body: str, depth: str = "0") -> ET.Element:
        response = await self._request(
            "PROPFIND",
            url,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=body,
            expected=(207,),
        )
        return _multistatus(response.content)

    async def discover_calendar_home(self) -> str:
        if self._calendar_home:
            return self._calendar_home
        principal_body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""
        root = await self._propfind(self.base_url, principal_body)
        response = root.find("d:response", NS)
        props = _successful_props(response) if response is not None else None
        principal_href = _xml_text(props, "d:current-user-principal/d:href")
        if not principal_href:
            raise CalendarPluginError("iCloud 未返回 current-user-principal")
        principal_url = urljoin(self.base_url, principal_href)
        home_body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""
        root = await self._propfind(principal_url, home_body)
        response = root.find("d:response", NS)
        props = _successful_props(response) if response is not None else None
        home_href = _xml_text(props, "c:calendar-home-set/d:href")
        if not home_href:
            raise CalendarPluginError("iCloud 未返回 calendar-home-set")
        self._calendar_home = urljoin(principal_url, home_href)
        return self._calendar_home

    async def list_calendars(self) -> list[CalendarRecord]:
        body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
 xmlns:cs="http://calendarserver.org/ns/" xmlns:i="http://apple.com/ns/ical/">
  <d:prop>
    <d:displayname/><d:resourcetype/><cs:getctag/><d:sync-token/><i:calendar-color/>
  </d:prop>
</d:propfind>"""
        root = await self._propfind(await self.discover_calendar_home(), body, depth="1")
        calendars: list[CalendarRecord] = []
        for response in root.findall("d:response", NS):
            props = _successful_props(response)
            if props is None or props.find("d:resourcetype/c:calendar", NS) is None:
                continue
            href = _xml_text(response, "d:href")
            if not href:
                continue
            absolute_href = urljoin(self.base_url, href)
            calendar_id = hashlib.sha256(absolute_href.encode()).hexdigest()[:24]
            calendars.append(
                CalendarRecord(
                    id=calendar_id,
                    href=absolute_href,
                    name=_xml_text(props, "d:displayname")
                    or urlparse(href).path.rstrip("/").split("/")[-1],
                    color=_xml_text(props, "i:calendar-color"),
                    ctag=_xml_text(props, "cs:getctag"),
                    sync_token=_xml_text(props, "d:sync-token"),
                )
            )
        return calendars

    @staticmethod
    def _caldav_time(value: datetime) -> str:
        return value.strftime("%Y%m%dT%H%M%SZ")

    async def query_objects(
        self, calendar: CalendarRecord, start: datetime, end: datetime
    ) -> list[CalendarObject]:
        start_text = self._caldav_time(start)
        end_text = self._caldav_time(end)
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data><c:expand start="{start_text}" end="{end_text}"/></c:calendar-data></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start_text}" end="{end_text}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
        response = await self._request(
            "REPORT",
            calendar.href,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=body,
            expected=(207,),
        )
        root = _multistatus(response.content)
        objects: list[CalendarObject] = []
        for item in root.findall("d:response", NS):
            props = _successful_props(item)
            href = _xml_text(item, "d:href")
            ical = _xml_text(props, "c:calendar-data")
            if href and ical:
                objects.append(
                    CalendarObject(
                        href=urljoin(calendar.href, href),
                        etag=_xml_text(props, "d:getetag"),
                        ical=ical,
                    )
                )
        return objects

    async def get_object(self, href: str) -> CalendarObject:
        response = await self._request("GET", href, expected=(200,))
        return CalendarObject(href, response.headers.get("ETag"), response.text)

    async def create_object(
        self, calendar: CalendarRecord, uid: str, ical: str
    ) -> CalendarObject:
        href = urljoin(calendar.href.rstrip("/") + "/", quote(uid, safe="") + ".ics")
        response = await self._request(
            "PUT",
            href,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            content=ical,
            expected=(201, 204),
        )
        return CalendarObject(href, response.headers.get("ETag"), ical)

    async def update_object(self, href: str, etag: str, ical: str) -> CalendarObject:
        response = await self._request(
            "PUT",
            href,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": etag},
            content=ical,
            expected=(200, 201, 204),
        )
        return CalendarObject(href, response.headers.get("ETag") or etag, ical)

    async def delete_object(self, href: str, etag: str) -> None:
        await self._request("DELETE", href, headers={"If-Match": etag}, expected=(200, 204))
