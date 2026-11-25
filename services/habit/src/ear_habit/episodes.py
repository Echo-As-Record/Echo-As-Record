"""Derived, bounded episode indexes; original evidence remains in EAR storage."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from ear_contracts import RawRecord, SemanticRecord, VersionRef, new_id
from ear_storage_client import (
    AssertTypeRevision,
    Create,
    Query,
    Reader,
    RegisterDependencies,
    TimeRange,
    Update,
    WriteOp,
    iter_query,
)

from ear_habit.observations import resolve_segments
from ear_habit.store import EPISODE_TYPE, check_scope, scope_key
from ear_habit.tokenize import ChineseTokenizer

MAX_SEGMENTS = 1000
MAX_INDEX_TERMS = 128


def find_episode(reader: Reader, activity_id: str) -> SemanticRecord | None:
    matches = list(iter_query(reader, Query(EPISODE_TYPE, where={"activity_id": activity_id})))
    if len(matches) > 1 or any(not isinstance(item, SemanticRecord) for item in matches):
        raise ValueError("ambiguous episode")
    return matches[0] if matches else None  # type: ignore[return-value]


def episode_ops(
    reader: Reader, activity_id: str, *, since: datetime | None = None
) -> list[WriteOp]:
    """Build an index over current overlapping evidence, never replacing source data."""
    scope = scope_key(since)
    activity = reader.get(activity_id)
    if (
        not isinstance(activity, SemanticRecord)
        or activity.data_type != "activity"
        or activity.superseded
    ):
        raise ValueError("activity unavailable")
    start, end = activity.event_start, activity.event_end
    if start is None or end is None or start >= end:
        raise ValueError("invalid activity time")
    guards = [
        AssertTypeRevision(kind, reader.type_revision(kind))
        for kind in ("activity", "transcript_segment", "attribution", "person")
    ]
    segments = []
    for item in iter_query(reader, Query("transcript_segment", time=TimeRange(start, end))):
        if not isinstance(item, RawRecord):
            raise ValueError("invalid transcript")
        if since is not None and item.ingested_at < since:
            continue
        segments.append(item)
        if len(segments) > MAX_SEGMENTS:
            raise ValueError("episode evidence limit exceeded")
    resolved = resolve_segments(reader, segments)
    counts: Counter[str] = Counter()
    tokenizer = ChineseTokenizer()
    for item in resolved:
        counts.update(tokenizer.tokens(item.segment.payload["text"]))
    selected = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:MAX_INDEX_TERMS]
    title = activity.payload.get("title_md", "")
    description = activity.payload.get("description_md", "")
    if not isinstance(title, str) or not isinstance(description, str):
        raise ValueError("invalid activity text")
    deps: dict[str, VersionRef] = {
        activity.id: VersionRef(data_type="activity", id=activity.id, version=activity.version)
    }
    for item in resolved:
        if item.attribution is not None:
            a = item.attribution
            deps[a.id] = VersionRef(data_type="attribution", id=a.id, version=a.version)
    payload = {
        "schema": 1,
        "scope_start": scope,
        "activity_id": activity.id,
        "activity_version": activity.version,
        "summary": {"title": title, "description": description},
        "index_tokenizer": tokenizer.identifier,
        "terms": dict(selected),
        "index_truncated": len(counts) > MAX_INDEX_TERMS,
        "evidence_ids": [item.segment.id for item in resolved],
        "person_ids": sorted({item.person_id for item in resolved if item.person_id is not None}),
        "unknown_evidence_ids": [item.segment.id for item in resolved if item.person_id is None],
    }
    current = find_episode(reader, activity_id)
    if current is None:
        episode_id = new_id()
        write: WriteOp = Create(
            EPISODE_TYPE,
            episode_id,
            payload,
            dedup_key=activity_id,
            event_start=start,
            event_end=end,
        )
    else:
        check_scope(current, scope)
        episode_id = current.id
        write = Update(
            EPISODE_TYPE, episode_id, current.version, payload, event_start=start, event_end=end
        )
    return [*guards, write, RegisterDependencies(EPISODE_TYPE, episode_id, list(deps.values()))]
