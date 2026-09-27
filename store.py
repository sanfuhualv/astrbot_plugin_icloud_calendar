from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import CalendarRecord, EventRecord


class EventStore:
    """SQLite 本地索引；凭据不会进入数据库。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS calendars (
                    id TEXT PRIMARY KEY,
                    href TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    color TEXT,
                    ctag TEXT,
                    sync_token TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    calendar_id TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
                    calendar_name TEXT NOT NULL,
                    href TEXT NOT NULL,
                    etag TEXT,
                    uid TEXT NOT NULL,
                    recurrence_id TEXT NOT NULL DEFAULT '',
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    start_local TEXT NOT NULL,
                    end_local TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    all_day INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    status TEXT NOT NULL,
                    transparency TEXT NOT NULL,
                    url TEXT NOT NULL,
                    organizer TEXT NOT NULL,
                    attendees_json TEXT NOT NULL,
                    raw_ical TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_range
                    ON events(calendar_id, start_utc, end_utc, id);
                CREATE INDEX IF NOT EXISTS idx_events_href
                    ON events(calendar_id, href);
                CREATE INDEX IF NOT EXISTS idx_events_uid
                    ON events(calendar_id, uid);
                """
            )

    def upsert_calendars(self, calendars: list[CalendarRecord]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO calendars(id, href, name, color, ctag, sync_token, updated_at)
                VALUES(:id, :href, :name, :color, :ctag, :sync_token, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    href=excluded.href, name=excluded.name, color=excluded.color,
                    ctag=excluded.ctag, sync_token=excluded.sync_token,
                    updated_at=excluded.updated_at
                """,
                [{**calendar.to_dict(), "updated_at": now} for calendar in calendars],
            )

    def list_calendars(self) -> list[CalendarRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, href, name, color, ctag, sync_token FROM calendars ORDER BY name, id"
            ).fetchall()
        return [CalendarRecord(**dict(row)) for row in rows]

    def get_calendar(self, calendar_id: str) -> CalendarRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, href, name, color, ctag, sync_token FROM calendars WHERE id=?",
                (calendar_id,),
            ).fetchone()
        return CalendarRecord(**dict(row)) if row else None

    @staticmethod
    def _event_params(events: list[EventRecord]) -> list[dict]:
        return [{**asdict(event), "all_day": int(event.all_day)} for event in events]

    def replace_range(
        self,
        calendar_id: str,
        start_utc: str,
        end_utc: str,
        events: list[EventRecord],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM events WHERE calendar_id=? AND end_utc>? AND start_utc<?",
                (calendar_id, start_utc, end_utc),
            )
            self._insert_events(conn, events)

    def upsert_events(self, events: list[EventRecord]) -> None:
        with self.connect() as conn:
            self._insert_events(conn, events)

    def _insert_events(self, conn: sqlite3.Connection, events: list[EventRecord]) -> None:
        if not events:
            return
        conn.executemany(
            """
            INSERT OR REPLACE INTO events(
                id, calendar_id, calendar_name, href, etag, uid, recurrence_id,
                start_utc, end_utc, start_local, end_local, timezone, all_day,
                summary, description, location, status, transparency, url,
                organizer, attendees_json, raw_ical, updated_at
            ) VALUES(
                :id, :calendar_id, :calendar_name, :href, :etag, :uid, :recurrence_id,
                :start_utc, :end_utc, :start_local, :end_local, :timezone, :all_day,
                :summary, :description, :location, :status, :transparency, :url,
                :organizer, :attendees_json, :raw_ical, :updated_at
            )
            """,
            self._event_params(events),
        )

    def get_event(self, event_id: str) -> EventRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["all_day"] = bool(data["all_day"])
        return EventRecord(**data)

    def delete_href(self, calendar_id: str, href: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM events WHERE calendar_id=? AND href=?", (calendar_id, href))

    def list_events(
        self,
        calendar_ids: list[str],
        start_utc: str,
        end_utc: str,
        query: str,
        limit: int,
        after_start: str | None,
        after_id: str | None,
    ) -> tuple[list[EventRecord], bool]:
        placeholders = ",".join("?" for _ in calendar_ids)
        params: list[object] = [*calendar_ids, start_utc, end_utc]
        where = [
            f"calendar_id IN ({placeholders})",
            "end_utc > ?",
            "start_utc < ?",
        ]
        if query:
            needle = f"%{query.casefold()}%"
            where.append(
                "(lower(summary) LIKE ? OR lower(description) LIKE ? OR lower(location) LIKE ?)"
            )
            params.extend([needle, needle, needle])
        if after_start is not None and after_id is not None:
            where.append("(start_utc > ? OR (start_utc = ? AND id > ?))")
            params.extend([after_start, after_start, after_id])
        params.append(limit + 1)
        sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY start_utc, id LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        records: list[EventRecord] = []
        for row in rows[:limit]:
            data = dict(row)
            data["all_day"] = bool(data["all_day"])
            records.append(EventRecord(**data))
        return records, has_more

    def status(self) -> dict[str, object]:
        with self.connect() as conn:
            calendar_count = conn.execute("SELECT count(*) FROM calendars").fetchone()[0]
            event_count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            range_row = conn.execute(
                "SELECT min(start_utc), max(end_utc), max(updated_at) FROM events"
            ).fetchone()
        return {
            "database": str(self.path),
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "calendar_count": calendar_count,
            "indexed_occurrence_count": event_count,
            "earliest_start": range_row[0],
            "latest_end": range_row[1],
            "last_event_refresh": range_row[2],
        }
