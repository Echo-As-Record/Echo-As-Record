"""Read-only candidate expression patterns, not psychological inference."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from ear_contracts import SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_habit.store import BUCKET_TYPE


@dataclass(frozen=True, slots=True)
class ExpressionCandidate:
    expression: str
    occurrences: int
    days_present: int
    observed_days: int
    bucket_versions: tuple[tuple[str, int], ...]


def expression_candidates(
    reader: Reader,
    person_id: str,
    start: datetime,
    end: datetime,
    *,
    min_days: int,
    min_occurrences: int,
) -> tuple[ExpressionCandidate, ...]:
    """Summarize candidate substring counts in whole UTC day buckets.

    Caller controls thresholds. A candidate is neither a verified filler nor a
    stable habit; bucket references support evidence lookup by the caller.
    """
    window = TimeRange(start, end)
    if start >= end or min_days < 1 or min_occurrences < 1:
        raise ValueError("invalid bounds or thresholds")
    if start.time().isoformat() != "00:00:00" or end.time().isoformat() != "00:00:00":
        raise ValueError("window must cover whole UTC days")
    query = Query(BUCKET_TYPE, time=window, where={"person_id": person_id, "scale": "day"})
    counts: dict[str, int] = defaultdict(int)
    refs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    observed_days = 0
    for bucket in iter_query(reader, query):
        if not isinstance(bucket, SemanticRecord) or bucket.event_start is None:
            raise ValueError("invalid day bucket")
        if not start <= bucket.event_start < end:
            continue
        stats = bucket.payload.get("stats")
        if not isinstance(stats, dict) or type(stats.get("segments")) is not int:
            raise ValueError("invalid day statistics")
        if stats["segments"] <= 0:
            continue
        observed_days += 1
        fillers = stats.get("fillers")
        if not isinstance(fillers, dict):
            raise ValueError("invalid candidate counts")
        for word, count in fillers.items():
            if not isinstance(word, str) or type(count) is not int or count < 1:
                raise ValueError("invalid candidate count")
            counts[word] += count
            refs[word].append((bucket.id, bucket.version))
    return tuple(
        ExpressionCandidate(word, count, len(refs[word]), observed_days, tuple(sorted(refs[word])))
        for word, count in sorted(counts.items())
        if len(refs[word]) >= min_days and count >= min_occurrences
    )
