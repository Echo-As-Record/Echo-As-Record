"""Bounded live evidence reads, adapted from PR a7baf9e; never creates a snapshot.

Only Reader.query/get are used, under the caller's current authorization. Joins and
pages are live_best_effort, NOT a consistent view, exhaustive search or stable
traversal under concurrent writes. Signed, short-lived process-local cursors bind
the query (including limit), not an identity or grant; every read still uses the
supplied Reader. Restart/another worker invalidates cursors. No payload is cached.

Limits apply per search: 100 scanned segments, 20 query pages, 64 Reader calls,
256 KiB budget-accounted row JSON, 64 KiB response JSON, 10 seconds cooperative
wall time. Reader's synchronous I/O cannot be interrupted: a blocking call may
exceed the deadline or materialize more bytes before returning. These are NOT
transport/DB-work/memory limits; upstream must separately bound I/O and decoding.
Row accounting includes all returned rows and extra fields, never dereferences
file_ref, and stops traversing at the budget. report.row_bytes is the accounted
prefix, not a claim about wire bytes. Response accounting conservatively uses
ASCII-escaped JSON (at least as large as the API's compact UTF-8 JSON).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from datetime import UTC, datetime, timedelta
from secrets import token_bytes
from time import monotonic
from time import time as timestamp
from typing import Any

from ear_contracts import RawRecord, SemanticRecord, is_id
from ear_storage_client import Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.queries import Page
from ear_storage_client.remote_errors import ProtocolError
from pydantic import BaseModel

MAX_SEGMENTS = 100
MAX_PAGES = 20
MAX_CALLS = 64
MAX_ROW_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
DEADLINE_SECONDS = 10.0
MAX_CURSOR_BYTES = 512
MAX_STORAGE_CURSOR_BYTES = 160
CURSOR_TTL_SECONDS = 900
_CURSOR_KEY = token_bytes(32)
_UTC_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", re.ASCII)


def _window(time: TimeRange) -> None:
    if not isinstance(time, TimeRange) or not timedelta(0) < time.end - time.start <= timedelta(
        hours=24
    ):
        raise ValueError("invalid_input")
    for value in (time.start, time.end):
        if value.utcoffset() != timedelta(0):
            raise ValueError("invalid_input")


def validate_window(start: str, end: str) -> TimeRange:
    """Require explicit RFC3339 UTC Z endpoints and a nonempty window <=24h."""
    try:
        if any(type(v) is not str or len(v) > 32 or not _UTC_Z.fullmatch(v) for v in (start, end)):
            raise ValueError
        time = TimeRange(datetime.fromisoformat(start), datetime.fromisoformat(end))
        _window(time)
        return time
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid_input") from None


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _binding(time: TimeRange, phrase: str, limit: int) -> str:
    return hashlib.sha256(_json([_utc(time.start), _utc(time.end), phrase, limit])).hexdigest()


def _encode_cursor(raw: str, binding: str) -> str | None:
    # The storage cursor is opaque: no numeric conversion, offset arithmetic or slicing.
    if type(raw) is not str or not raw or len(raw) > MAX_STORAGE_CURSOR_BYTES:
        return None
    try:
        if len(raw.encode("utf-8")) > MAX_STORAGE_CURSOR_BYTES:
            return None
    except UnicodeError:
        return None
    body = _json([int(timestamp()) + CURSOR_TTL_SECONDS, binding, raw])
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    signature = hmac.new(_CURSOR_KEY, payload.encode(), hashlib.sha256).hexdigest()
    token = payload + "." + signature
    return token if len(token) <= MAX_CURSOR_BYTES else None


def _decode_cursor(token: str | None, binding: str) -> str | None:
    if token is None:
        return None
    try:
        if type(token) is not str or not 1 <= len(token) <= MAX_CURSOR_BYTES:
            raise ValueError
        payload, signature = token.split(".")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", payload) or not re.fullmatch(
            r"[0-9a-f]{64}", signature
        ):
            raise ValueError
        expected = hmac.new(_CURSOR_KEY, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != payload:
            raise ValueError
        values = json.loads(decoded)
        if type(values) is not list or len(values) != 3:
            raise ValueError
        expiry, previous, raw = values
        now = timestamp()
        if (
            type(expiry) is not int
            or not now < expiry <= now + CURSOR_TTL_SECONDS + 1
            or previous != binding
            or type(raw) is not str
            or not raw
            or len(raw.encode("utf-8")) > MAX_STORAGE_CURSOR_BYTES
        ):
            raise ValueError
        return raw
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_input") from None


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Budget:
    def __init__(self, reader: Reader) -> None:
        self.reader = reader
        self.deadline = monotonic() + DEADLINE_SECONDS
        self.calls = 0
        self.pages = 0
        self.read_rows = 0
        self.row_bytes = 0

    def check(self) -> None:
        if monotonic() >= self.deadline:
            raise _Stop("deadline")

    def _add(self, size: int) -> None:
        if size > MAX_ROW_BYTES - self.row_bytes:
            raise _Stop("row_bytes")
        self.row_bytes += size

    def charge(self, value: Any, depth: int = 0) -> None:
        """Bounded conservative JSON accounting, without copying whole payloads."""
        self.check()
        if depth > 32:
            raise _Stop("row_bytes")
        if isinstance(value, BaseModel):
            self.charge(vars(value), depth + 1)
            self.charge(value.__pydantic_extra__, depth + 1)
        elif isinstance(value, dict):
            self._add(2)
            for key, child in value.items():
                self.charge(key, depth + 1)
                self._add(2)
                self.charge(child, depth + 1)
        elif isinstance(value, list | tuple):
            self._add(2)
            for child in value:
                self._add(1)
                self.charge(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > MAX_ROW_BYTES - self.row_bytes:
                raise _Stop("row_bytes")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ProtocolError("invalid_evidence_row") from None
            self._add(len(_json(value)))
        elif isinstance(value, datetime):
            self._add(len(_json(_utc(value))))
        elif value is None or type(value) in (bool, int, float):
            try:
                self._add(len(_json(value)))
            except (ValueError, OverflowError):
                raise ProtocolError("invalid_evidence_row") from None
        else:
            raise ProtocolError("invalid_evidence_row")

    def before_call(self) -> None:
        self.check()
        if self.calls >= MAX_CALLS:
            raise _Stop("call_budget")
        self.calls += 1

    def query(self, query: Query) -> Page:
        self.check()
        if self.pages >= MAX_PAGES:
            raise _Stop("page_budget")
        self.before_call()
        self.pages += 1
        page = self.reader.query(query)
        if not isinstance(page, Page) or not isinstance(page.items, tuple):
            raise ProtocolError("invalid_evidence_page")
        self.read_rows += len(page.items)
        self.check()
        if len(page.items) > query.limit or (
            page.next_cursor is not None
            and (type(page.next_cursor) is not str or not page.next_cursor)
        ):
            raise ProtocolError("invalid_evidence_page")
        for row in page.items:
            self.charge(row)
        return page

    def get(self, id: str):
        self.before_call()
        row = self.reader.get(id)
        self.read_rows += row is not None
        self.check()
        if row is not None:
            self.charge(row)
        return row


def _id(value: Any) -> str | None:
    return value if is_id(value) else None


def _candidate(budget: _Budget, segment: RawRecord, text: str, position: int) -> dict[str, Any]:
    data = segment.payload
    voice = _id(data.get("voiceprint_id"))
    audio_id = _id(data.get("audio_ref_id"))
    excerpt_start = max(0, position - 80)
    item: dict[str, Any] = {
        "transcript_segment_id": segment.id,
        "transcript_segment_version": segment.version,
        "start": _utc(segment.event_start),
        "end": _utc(segment.event_end),
        "text": text[excerpt_start : excerpt_start + 400],
        "local_speaker_id": _id(data.get("local_speaker_id")),
        "voiceprint_id": voice,
        "audio_ref_id": audio_id,
        "playback": None,
        "playback_status": "unavailable",
        "attribution": None,
        "attribution_status": "no_voiceprint" if voice is None else "unavailable",
    }
    # Only read the authorized audio_ref RECORD, never its file_ref/path/URL.
    audio = budget.get(audio_id) if audio_id is not None else None
    if isinstance(audio, RawRecord) and audio.id == audio_id and audio.data_type == "audio_ref":
        start = (segment.event_start - audio.event_start).total_seconds()
        end = (segment.event_end - audio.event_start).total_seconds()
        duration = (audio.event_end - audio.event_start).total_seconds()
        if all(math.isfinite(v) for v in (start, end, duration)) and 0 <= start <= end <= duration:
            item["playback"] = {
                "audio_ref_id": audio_id,
                "start_seconds": start,
                "end_seconds": end,
            }
            item["playback_status"] = "available"
        else:
            item["playback_status"] = "invalid_offsets"
    if voice is None:
        return item
    matches = budget.query(Query("attribution", where={"subject_id": voice}, limit=2))
    if len(matches.items) > 1 or matches.next_cursor is not None:
        item["attribution_status"] = "ambiguous"
        return item
    attr = matches.items[0] if matches.items else None
    if not (
        isinstance(attr, SemanticRecord)
        and attr.data_type == "attribution"
        and not attr.superseded
        and attr.payload.get("subject_id") == voice
        and attr.payload.get("subject_type", "voiceprint") == "voiceprint"
        and attr.payload.get("target_type") == "person"
    ):
        return item
    pid = _id(attr.payload.get("target_id"))
    if pid is None:
        return item
    attribution = {
        "id": attr.id,
        "version": attr.version,
        "person_id": pid,
        "person_version": None,
        "person_status": "unavailable",
        "name": None,
    }
    item["attribution"] = attribution
    person = budget.get(pid)
    if not (
        isinstance(person, SemanticRecord) and person.id == pid and person.data_type == "person"
    ):
        return item
    attribution["person_version"] = person.version
    attribution["person_status"] = person.status
    if person.superseded:
        item["attribution_status"] = "person_superseded"
        return item
    name = person.payload.get("name")
    if name is None or (isinstance(name, str) and len(name) <= 256):
        attribution["name"] = name
        item["attribution_status"] = "resolved"
    else:
        item["attribution_status"] = "invalid_name"
    return item


def search(
    reader: Reader, time: TimeRange, phrase: str, cursor: str | None, limit: int
) -> dict[str, Any]:
    """Closed projection; truncated also means budget-limited, not just more hits.

    next_cursor advances ONLY fully consumed pages. Query pages never exceed the
    remaining result slots, so there is no precise-resume re-fetch. A mid-page
    budget failure drops continuation rather than skipping/repeating evidence.
    Empty pages with a cursor do not mean exhaustion. Pagination may repeat/omit
    rows under concurrent writes; callers must not infer complete/stable coverage.
    Permission/transport errors propagate, never become a false empty success.
    """
    _window(time)
    if (
        type(phrase) is not str
        or not 1 <= len(phrase) <= 256
        or type(limit) is not int
        or not 1 <= limit <= 10
    ):
        raise ValueError("invalid_input")
    binding = _binding(time, phrase, limit)
    raw_cursor = _decode_cursor(cursor, binding)
    budget = _Budget(reader)
    items: list[dict[str, Any]] = []
    scanned = 0
    seen: set[str] = {raw_cursor} if raw_cursor is not None else set()
    reason = "exhausted"
    safe = True
    try:
        while scanned < MAX_SEGMENTS and len(items) < limit:
            budget.check()
            if budget.pages >= MAX_PAGES:
                raise _Stop("page_budget")
            if budget.calls >= MAX_CALLS:
                raise _Stop("call_budget")
            safe = False
            page = budget.query(
                Query(
                    "transcript_segment",
                    time=time,
                    cursor=raw_cursor,
                    limit=min(limit - len(items), MAX_SEGMENTS - scanned),
                )
            )
            for segment in page.items:
                budget.check()
                scanned += 1
                if not (
                    isinstance(segment, RawRecord)
                    and segment.data_type == "transcript_segment"
                    and time.overlaps(segment.event_start, segment.event_end)
                ):
                    raise ProtocolError("invalid_evidence_segment")
                text = segment.payload.get("text")
                if isinstance(text, str) and (position := text.find(phrase)) >= 0:
                    items.append(_candidate(budget, segment, text, position))
            raw_cursor = page.next_cursor
            safe = True
            if raw_cursor is None:
                break
            if _encode_cursor(raw_cursor, binding) is None:
                safe = False
                raise _Stop("cursor_unavailable")
            if raw_cursor in seen:
                safe = False
                raise _Stop("cursor_stalled")
            seen.add(raw_cursor)
        else:
            if raw_cursor is not None:
                reason = "result_limit" if len(items) == limit else "segment_limit"
        budget.check()
    except _Stop as exc:
        reason = exc.reason
    next_cursor = _encode_cursor(raw_cursor, binding) if safe and raw_cursor is not None else None
    truncated = reason != "exhausted" or raw_cursor is not None
    result: dict[str, Any] = {
        "items": items,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "scanned": scanned,
        "consistency": "live_best_effort",
        "report": {
            "stop_reason": reason,
            "continuation": "available"
            if next_cursor
            else "unavailable"
            if truncated
            else "exhausted",
            "pages": budget.pages,
            "calls": budget.calls,
            "read_rows": budget.read_rows,
            "row_bytes": budget.row_bytes,
            "deadline_mode": "cooperative",
        },
    }
    while len(_json(result)) > MAX_RESPONSE_BYTES:
        result["truncated"] = True
        result["next_cursor"] = None
        result["report"]["stop_reason"] = "response_bytes"
        result["report"]["continuation"] = "unavailable"
        if not items:
            raise ProtocolError("evidence_response_budget_too_small")
        items.pop()
    return result
