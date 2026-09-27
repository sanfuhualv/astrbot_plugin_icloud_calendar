import asyncio

import httpx
from astrbot_plugin_icloud_calendar.caldav_client import AsyncCalDAVClient


def test_calendar_discovery():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(207, text="""<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>
                <d:current-user-principal><d:href>/123/principal/</d:href></d:current-user-principal>
                </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>""")
        if request.url.path == "/123/principal/":
            return httpx.Response(207, text="""<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
                <d:response><d:propstat><d:prop><c:calendar-home-set><d:href>/123/calendars/</d:href>
                </c:calendar-home-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
                </d:response></d:multistatus>""")
        if request.url.path == "/123/calendars/":
            return httpx.Response(207, text="""<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
                xmlns:cs="http://calendarserver.org/ns/" xmlns:i="http://apple.com/ns/ical/">
                <d:response><d:href>/123/calendars/work/</d:href><d:propstat><d:prop>
                <d:displayname>Work</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
                <cs:getctag>7</cs:getctag><d:sync-token>token-7</d:sync-token>
                </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>""")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        client = AsyncCalDAVClient(
            "apple@example.com", "app-password", "https://caldav.icloud.com/",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.list_calendars()
        finally:
            await client.close()

    calendars = asyncio.run(run())
    assert len(calendars) == 1
    assert calendars[0].name == "Work"
    assert calendars[0].ctag == "7"
