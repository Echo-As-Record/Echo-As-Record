"""Closed read projections for the source timeline, under the current caller's grants."""

import re
from datetime import datetime, timedelta
from typing import Any

from ear_contracts import RawRecord, is_id
from ear_storage_client import PermissionDenied, Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.remote_errors import ProtocolError

RECORDINGS = ("audio_ref", "transcript_segment")
CHAT = ("chat_session", "chat_message", "chat_attachment")
SOURCES = ("all", "legacy-ear", "librechat")
MAX_PAGE = 16
TEXT_LIMIT = 2000
_UTC_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")


def window(start: str, end: str) -> TimeRange:
    # Same browser day including a 25-hour DST day; reject naive/non-Z times.
    if any(len(s) > 32 or not _UTC_Z.fullmatch(s) for s in (start, end)):
        raise ValueError("invalid period")
    time = TimeRange(datetime.fromisoformat(start), datetime.fromisoformat(end))
    if not timedelta(0) < time.end - time.start <= timedelta(hours=26):
        raise ValueError("invalid period")
    return time


def _audio_source(row: RawRecord) -> str:
    upstream = row.payload.get("upstream")
    service = upstream.get("service") if isinstance(upstream, dict) else None
    if row.source.channel == "octopus":
        if service == "ear":
            return "legacy-ear"
        if service == "ear-native":
            return "native-ear"
    return "other"


def page(
    reader: Reader, kind: str, time: TimeRange, source: str, limit: int, cursor: str | None
) -> dict[str, Any]:
    result = reader.query(Query(kind, time=time, limit=limit, cursor=cursor))
    if len(result.items) > limit or (
        result.next_cursor is not None
        and (
            not result.next_cursor or len(result.next_cursor) > 512 or result.next_cursor == cursor
        )
    ):
        raise ProtocolError("invalid_source_page")
    items: list[dict[str, Any]] = []
    audio_sources: dict[str, str] = {}
    for row in result.items:
        if (
            not isinstance(row, RawRecord)
            or row.data_type != kind
            or not time.overlaps(row.event_start, row.event_end)
        ):
            raise ProtocolError("invalid_source_record")
        payload = row.payload
        if kind in CHAT:
            if row.source.channel != "librechat" or payload.get("source_system") != "librechat":
                continue
            origin = "librechat"
        elif kind == "audio_ref":
            origin = _audio_source(row)
        else:
            audio_id = payload.get("audio_ref_id")
            origin = "other"
            if row.source.channel == "octopus" and isinstance(audio_id, str) and is_id(audio_id):
                if audio_id not in audio_sources:
                    try:
                        audio = reader.get(audio_id)
                    except PermissionDenied:
                        audio = None
                    audio_sources[audio_id] = (
                        _audio_source(audio)
                        if isinstance(audio, RawRecord)
                        and audio.data_type == "audio_ref"
                        and audio.source == row.source
                        else "other"
                    )
                origin = audio_sources[audio_id]
        if source != "all" and source != origin:
            continue
        title = payload.get("title" if kind == "chat_session" else "filename")
        text = payload.get("text") if kind in ("transcript_segment", "chat_message") else None
        role = payload.get("role")
        items.append(
            {
                "id": row.id,
                "kind": kind,
                "source": origin,
                "start": row.event_start.isoformat().replace("+00:00", "Z"),
                "end": row.event_end.isoformat().replace("+00:00", "Z"),
                "title": title[:240]
                if kind in ("chat_session", "chat_attachment") and isinstance(title, str)
                else "",
                "text": text[:TEXT_LIMIT] if isinstance(text, str) else "",
                "truncated": isinstance(text, str)
                and (len(text) > TEXT_LIMIT or payload.get("text_truncated") is True),
                "role": role if kind == "chat_message" and role in ("user", "assistant") else None,
            }
        )
    return {"items": items, "next_cursor": result.next_cursor}
