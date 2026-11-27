"""Read-only projection for the two date-card tracks. No row bodies leave Storage."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import psycopg
from ear_contracts import DataTypeSpec, is_id
from ear_storage_client import InvalidRequest
from psycopg import sql

from ear_storage.auth import Principal
from ear_storage.causality import visible_trace_sql
from ear_storage.repository import registry_from_db, table_name
from ear_storage.schema import table

Day = tuple[datetime, datetime]
SLOT_SECONDS = 300
Shift = tuple[timedelta, dict[str, list[str]]]
EVENT_TYPES = ("audio_ref", "transcript_segment")
CHAT_TYPES = ("chat_session", "chat_message", "chat_attachment")
_UTC_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def parse_days(value: Any) -> list[Day]:
    if type(value) is not list or not 1 <= len(value) <= 7:
        raise InvalidRequest("invalid coverage days")
    days = []
    for raw in value:
        if (
            type(raw) is not dict
            or raw.keys() != {"start", "end"}
            or any(
                type(t) is not str or len(t) != 20 or not _UTC_Z.fullmatch(t) for t in raw.values()
            )
        ):
            raise InvalidRequest("invalid coverage day")
        try:
            start, end = (datetime.fromisoformat(raw[k]) for k in ("start", "end"))
        except ValueError:
            raise InvalidRequest("invalid coverage time") from None
        if not timedelta(0) < end - start <= timedelta(hours=26) or (days and start < days[-1][1]):
            raise InvalidRequest("invalid coverage range")
        days.append((start, end))
    if days[-1][1] - days[0][0] > timedelta(days=8):
        raise InvalidRequest("coverage window too long")
    return days


def parse_shift(value: Any) -> Shift | None:
    """Accept only server-supplied scoped clock metadata on the internal wire."""
    if value is None:
        return None
    if type(value) is not dict or value.keys() != {"seconds", "ids"}:
        raise InvalidRequest("invalid coverage shift")
    seconds, by_type = value["seconds"], value["ids"]
    if (
        type(seconds) is not int
        or not 0 < abs(seconds) <= 86400
        or (type(by_type) is not dict or not by_type or not by_type.keys() <= set(EVENT_TYPES))
    ):
        raise InvalidRequest("invalid coverage shift")
    count = 0
    for ids in by_type.values():
        if (
            type(ids) is not list
            or not ids
            or (count := count + len(ids)) > 10000
            or (
                any(not isinstance(rid, str) or not is_id(rid) for rid in ids)
                or len(set(ids)) != len(ids)
            )
        ):
            raise InvalidRequest("invalid coverage scope")
    return timedelta(seconds=seconds), by_type


def _vector(
    conn: psycopg.Connection,
    days: list[Day],
    parts: list[tuple[str, DataTypeSpec]],
    principal: Principal,
    shift: Shift | None,
) -> list[str]:
    # Aggregate the ranges once, never send individual timestamps or record bodies
    # to the API. A one-microsecond domain point lights its own slot.
    selects: list[sql.Composed] = []
    args: list[Any] = []
    start, end = days[0][0], days[-1][1]
    for kind, spec in parts:
        local_start, local_end = start, end
        first, last = sql.SQL("r.event_start"), sql.SQL("r.event_end")
        join = sql.SQL("")
        if shift and (ids := shift[1].get(kind)):
            delta = shift[0]
            # Include all candidates in both original and translated time. Use
            # the effective interval (not these widened bounds) for the bins.
            try:
                local_start = min(start, start - delta)
                local_end = max(end, end - delta)
            except OverflowError:
                raise InvalidRequest("invalid shifted coverage bounds") from None
            join = sql.SQL("LEFT JOIN unnest(%s::text[]) moved(id) ON moved.id=r.id")
            # Keep this an elapsed-seconds shift even if PostgreSQL's session is
            # in a timezone with DST; a calendar-day interval is not equivalent.
            adjustment = sql.SQL(
                "CASE WHEN moved.id IS NULL THEN interval '0' ELSE make_interval(secs => %s) END"
            )
            first = sql.SQL("r.event_start + ({})").format(adjustment)
            last = sql.SQL("r.event_end + ({})").format(adjustment)
            seconds = int(delta.total_seconds())
            args.extend((seconds, seconds, ids))
        visible = [
            sql.SQL("r.data_type=%s"),
            sql.SQL("r.event_start < %s"),
            sql.SQL("(r.event_end > %s OR (r.event_start=r.event_end AND r.event_start >= %s))"),
        ]
        if kind == "activity":
            visible += [sql.SQL("NOT r.superseded"), visible_trace_sql(principal.read_types)]
        elif kind in CHAT_TYPES:
            visible += [
                sql.SQL("r.record_text::json->'source'->>'channel'='librechat'"),
                sql.SQL("r.payload_text::json->>'source_system'='librechat'"),
            ]
        selects.append(
            sql.SQL("""
            SELECT {first} AS first, {last} AS last
            FROM {table} r JOIN storage.identity i ON i.id=r.id AND i.current_seq=r.seq
            {join} WHERE {visible}
        """).format(
                first=first,
                last=last,
                join=join,
                table=table(spec.writer, table_name(spec.kind)),
                visible=sql.SQL(" AND ").join(visible),
            )
        )
        args.extend((kind, local_end, local_start, local_start))
    args.extend(([d[0] for d in days], [d[1] for d in days]))
    query = sql.SQL("""
        WITH covered AS MATERIALIZED (
            SELECT range_agg(tstzrange(first, CASE WHEN first=last
                THEN last + interval '1 microsecond' ELSE last END, '[)')) AS spans
            FROM ({ranges}) events
        )
        SELECT string_agg(CASE WHEN covered.spans &&
            tstzrange(slot, LEAST(slot + interval '5 minutes', d.last), '[)')
            THEN '1' ELSE '0' END, '' ORDER BY slot)
        FROM covered CROSS JOIN unnest(%s::timestamptz[], %s::timestamptz[])
            WITH ORDINALITY d(first,last,ord)
        CROSS JOIN LATERAL generate_series(d.first, d.last, interval '5 minutes') slot
        WHERE slot < d.last GROUP BY d.ord ORDER BY d.ord
    """).format(ranges=sql.SQL(" UNION ALL ").join(selects))
    return [row[0] for row in conn.execute(query, args)]


def project(
    conn: psycopg.Connection,
    principal: Principal,
    days: list[Day],
    include_chat: bool,
    shift: Shift | None = None,
) -> dict[str, Any]:
    registry = registry_from_db(conn)
    expected = EVENT_TYPES + (CHAT_TYPES if include_chat else ())
    tracks = {}
    for track, requested in (("timeline", expected), ("activity", ("activity",))):
        parts = []
        for kind in requested:
            spec = registry.data_types.get(kind)
            if (
                kind in principal.read_types
                and spec is not None
                and spec.kind == ("semantic" if kind == "activity" else "raw_record")
            ):
                parts.append((kind, spec))
        state = "ready" if len(parts) == len(requested) else "partial" if parts else "unavailable"
        tracks[track] = {
            "state": state,
            "days": _vector(conn, days, parts, principal, shift) if parts else [""] * len(days),
        }
    return {"slot_seconds": SLOT_SECONDS, **tracks}
