"""Explicit snapshot-based rebuilding; no background consumer or core writes."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ear_contracts import SemanticRecord, VersionRef
from ear_storage_client import AssertTypeRevision, Query, Reader, TimeRange, WriteOp, iter_query

from ear_habit.aggregate import period_range
from ear_habit.buckets import Utterance, stats_of
from ear_habit.observations import observations
from ear_habit.store import BUCKET_TYPE, check_scope, scope_key, upsert_bucket_ops


def rebuild_day_ops(reader: Reader, period: str, *, since: datetime | None = None) -> list[WriteOp]:
    """Plan a whole UTC day; old owners are zeroed when attribution changes.

    Source guards detect changed/new inputs. A single writer must serialize
    rebuilds for a day: the SDK cannot guard a type that this batch also writes.
    This function alone does not subscribe to changes.
    """
    start, end = period_range(period, "day")
    scope = scope_key(since)
    guards = [
        AssertTypeRevision(kind, reader.type_revision(kind))
        for kind in ("transcript_segment", "attribution", "person")
    ]
    grouped = defaultdict(list)
    dependencies: dict[str, dict[str, VersionRef]] = defaultdict(dict)
    evidence: dict[str, list[str]] = defaultdict(list)
    for obs in observations(reader, start, end):
        if since is not None and obs.segment.ingested_at < since:
            continue
        if obs.person_id is None or obs.attribution is None:
            continue
        duration_ms = int((obs.segment.event_end - obs.segment.event_start).total_seconds() * 1000)
        grouped[obs.person_id].append(Utterance(obs.segment.payload["text"], duration_ms))
        dependencies[obs.person_id][obs.attribution.id] = VersionRef(
            data_type="attribution", id=obs.attribution.id, version=obs.attribution.version
        )
        evidence[obs.person_id].append(obs.segment.id)
    existing = iter_query(reader, Query(BUCKET_TYPE, time=TimeRange(start, end)))
    people = set(grouped)
    for record in existing:
        if not isinstance(record, SemanticRecord):
            raise ValueError("invalid habit bucket")
        if record.payload.get("scale") == "day" and record.payload.get("period") == period:
            check_scope(record, scope)
            person_id = record.payload.get("person_id")
            if not isinstance(person_id, str):
                raise ValueError("invalid habit owner")
            people.add(person_id)
    ops: list[WriteOp] = []
    for person_id in sorted(people):
        ops.extend(
            upsert_bucket_ops(
                reader,
                person_id,
                "day",
                period,
                stats_of(grouped[person_id]),
                dependencies[person_id].values(),
                evidence_ids=evidence[person_id],
                scope_start=scope,
            )
        )
    return [*guards, *ops] if ops else []
