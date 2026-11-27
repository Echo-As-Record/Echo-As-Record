"""Opt-in display-time projection. Storage event timestamps remain canonical."""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from secrets import token_bytes
from typing import Any

from ear_contracts import RawRecord, is_id
from ear_storage_client import Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.queries import Page
from ear_storage_client.remote_errors import ProtocolError

KINDS = ("audio_ref", "transcript_segment")
# The private Storage coverage wire has the same upper bounds.
MAX_SHIFT_SECONDS = 24 * 60 * 60
MAX_RECORDS = 10_000
_BATCH = 64
_READS_PER_STREAM = 4
_CURSOR_LIMIT = 512


@dataclass(frozen=True, slots=True)
class Position:
    cursor: str | None = None
    index: int = 0


class TimeProjection:
    """An explicit host-supplied scope and uniform offset; no config or data loader."""

    def __init__(self, seconds: int, ids: Mapping[str, Sequence[str]]) -> None:
        if (
            type(seconds) is not int
            or not 0 < abs(seconds) <= MAX_SHIFT_SECONDS
            or not ids
            or not ids.keys() <= set(KINDS)
        ):
            raise ValueError("invalid effective-time scope")
        self._ids: dict[str, frozenset[str]] = {}
        total = 0
        for kind, values in ids.items():
            total += len(values)
            if not values or total > MAX_RECORDS or not all(is_id(rid) for rid in values):
                raise ValueError("invalid effective-time scope")
            selected = frozenset(values)
            if len(selected) != len(values):
                raise ValueError("invalid effective-time scope")
            self._ids[kind] = selected
        self._seconds = seconds
        self._delta = timedelta(seconds=seconds)
        self._key = token_bytes(32)

    def coverage_shift(self) -> dict[str, Any]:
        # Forward only host-supplied identifiers. Storage checks its usual grants,
        # uses actual stored times, and returns occupancy (not records or counts).
        return {"seconds": self._seconds, "ids": {k: sorted(v) for k, v in self._ids.items()}}

    def selected(self, row: RawRecord) -> bool:
        ids = self._ids.get(row.data_type)
        return ids is not None and row.id in ids

    def interval(self, row: RawRecord) -> tuple[datetime, datetime]:
        if not self.selected(row):
            return row.event_start, row.event_end
        try:
            return row.event_start + self._delta, row.event_end + self._delta
        except OverflowError:
            raise ProtocolError("invalid_effective_time_record") from None

    def _signature(self, scope: bytes, body: str) -> str:
        return hmac.digest(self._key, scope + b"\0" + body.encode("ascii"), "sha256").hex()

    def _resume(self, token: str | None, scope: bytes) -> tuple[Position | None, Position | None]:
        if token is None:
            return Position(), Position()
        try:
            if not 0 < len(token) <= _CURSOR_LIMIT:
                raise ValueError
            body, signature = token.split(".")
            if not hmac.compare_digest(signature, self._signature(scope, body)):
                raise ValueError
            state = json.loads(base64.b64decode(body, altchars=b"-_", validate=True))
            # Only tokens emitted by this application instance can reach this point.
            left, right = state
            return (
                None if left is None else Position(*left),
                None if right is None else Position(*right),
            )
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid effective-time cursor") from None

    def _continuation(self, scope: bytes, *positions: Position | None) -> str | None:
        if all(p is None for p in positions):
            return None
        state = [None if p is None else (p.cursor, p.index) for p in positions]
        body = base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()
        token = body + "." + self._signature(scope, body)
        if len(token) > _CURSOR_LIMIT:
            raise ProtocolError("effective_time_cursor_too_long")
        return token

    def page(
        self,
        reader: Reader,
        kind: str,
        time: TimeRange,
        limit: int,
        cursor: str | None,
        *,
        subject: str,
        surface: str,
    ) -> Page:
        if kind not in self._ids:
            return reader.query(Query(kind, time=time, limit=limit, cursor=cursor))
        try:
            shifted = TimeRange(time.start - self._delta, time.end - self._delta)
        except OverflowError:
            raise ValueError("invalid effective-time window") from None
        scope = json.dumps(
            [subject, surface, kind, time.start.isoformat(), time.end.isoformat(), limit]
        ).encode()
        original_pos, shifted_pos = self._resume(cursor, scope)
        ordinary = _Stream(reader, Query(kind, time=time, limit=_BATCH), original_pos, self, False)
        moved = _Stream(reader, Query(kind, time=shifted, limit=_BATCH), shifted_pos, self, True)
        items: list[RawRecord] = []
        while len(items) < limit:
            left, right = ordinary.head(), moved.head()
            if left is not None and (
                moved.position is None
                or (
                    right is not None
                    and (left.event_start, left.id) <= (self.interval(right)[0], right.id)
                )
            ):
                items.append(left)
                ordinary.advance()
            elif right is not None and (ordinary.position is None or left is not None):
                items.append(right)
                moved.advance()
            else:
                # A head may be unknown because of the per-request budget. Continue
                # on the next page rather than emitting the other stream out of order.
                break
        next_cursor = self._continuation(scope, ordinary.position, moved.position)
        if next_cursor is not None and next_cursor == cursor:
            raise ProtocolError("effective_time_cursor_stalled")
        return Page(tuple(items), next_cursor)


class _Stream:
    """Resume using a whole upstream cursor and an index into a fixed-size page."""

    def __init__(
        self,
        reader: Reader,
        query: Query,
        position: Position | None,
        view: TimeProjection,
        moved: bool,
    ) -> None:
        self.reader, self.query, self.position = reader, query, position
        self.view, self.moved = view, moved
        self.rows: tuple[RawRecord, ...] = ()
        self.next_cursor: str | None = None
        self.reads = 0
        self.last_key: tuple[datetime, str] | None = None
        self.seen: set[str | None] = set()

    def advance(self) -> None:
        position = self.position
        assert position is not None
        self.position = (
            replace(position, index=position.index + 1)
            if position.index + 1 < len(self.rows)
            else Position(self.next_cursor)
            if self.next_cursor is not None
            else None
        )
        if self.position is None or self.position.index == 0:
            self.rows = ()

    def _load(self, position: Position) -> None:
        self.seen.add(position.cursor)
        page = self.reader.query(replace(self.query, cursor=position.cursor))
        self.reads += 1
        if len(page.items) > _BATCH or (
            page.next_cursor is not None and (not page.next_cursor or page.next_cursor in self.seen)
        ):
            raise ProtocolError("invalid_effective_time_upstream_page")
        rows: list[RawRecord] = []
        for row in page.items:
            if (
                not isinstance(row, RawRecord)
                or row.data_type != self.query.data_type
                or self.query.time is None
                or not self.query.time.overlaps(row.event_start, row.event_end)
            ):
                raise ProtocolError("invalid_effective_time_upstream_record")
            key = (row.event_start, row.id)
            if self.last_key is not None and key <= self.last_key:
                raise ProtocolError("invalid_effective_time_upstream_order")
            self.last_key = key
            rows.append(row)
        self.rows, self.next_cursor = tuple(rows), page.next_cursor

    def head(self) -> RawRecord | None:
        while (position := self.position) is not None:
            if not self.rows:
                if self.reads >= _READS_PER_STREAM:
                    break
                self._load(position)
            if position.index >= len(self.rows):
                # An empty upstream page can still have a cursor; an out-of-date
                # within-page offset is best-effort under live appends.
                self.advance()
                continue
            row = self.rows[position.index]
            if self.view.selected(row) == self.moved:
                return row
            self.advance()
        return None
