"""Thin read/write boundary over ①; no private database or registry mutations."""

from __future__ import annotations

from ear_contracts import SemanticRecord, VersionRef
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_extraction.domain import Blocked, Interval


def semantic(reader: Reader, id: str | None, kind: str) -> SemanticRecord:
    item = reader.get(id) if id else None
    if not isinstance(item, SemanticRecord) or item.data_type != kind or item.superseded:
        raise Blocked(f"missing_or_superseded_{kind}")
    return item


def records(reader: Reader, kind: str, interval: Interval | None = None) -> list[SemanticRecord]:
    time = TimeRange(interval.start, interval.end) if interval else None
    return [r for r in iter_query(reader, Query(kind, time=time)) if isinstance(r, SemanticRecord)]


def ref(item: SemanticRecord) -> VersionRef:
    return VersionRef(data_type=item.data_type, id=item.id, version=item.version)


def range_of(item: SemanticRecord) -> Interval:
    if item.event_start is None or item.event_end is None:
        raise Blocked("record_missing_interval")
    return Interval(start=item.event_start, end=item.event_end)
