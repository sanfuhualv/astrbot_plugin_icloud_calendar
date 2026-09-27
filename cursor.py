from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .errors import CalendarPluginError


def query_fingerprint(
    calendar_ids: list[str], start: str, end: str, query: str
) -> str:
    payload = json.dumps(
        [sorted(calendar_ids), start, end, query], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(payload, dict):
            raise TypeError("cursor payload is not an object")
        return payload
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalendarPluginError("无效或已损坏的分页游标。") from exc
