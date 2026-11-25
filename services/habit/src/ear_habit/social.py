"""L2 activity observations. Co-occurrence is not evidence of conversation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations, pairwise

from ear_contracts import SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_habit.observations import resolve_segments


@dataclass(frozen=True, slots=True)
class Participant:
    person_id: str
    segment_ids: tuple[str, ...]
    attribution_versions: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class AdjacentTurns:
    """An ordering observation; not proof that either speaker addressed the other."""

    first_person_id: str
    second_person_id: str
    first_segment_id: str
    second_segment_id: str
    gap_ms: int
    first_has_question_mark: bool


@dataclass(frozen=True, slots=True)
class ActivityObservation:
    activity_id: str
    activity_version: int
    participants: tuple[Participant, ...]
    unresolved_segment_ids: tuple[str, ...]
    adjacent_turns: tuple[AdjacentTurns, ...] = ()


@dataclass(frozen=True, slots=True)
class Cooccurrence:
    person_ids: tuple[str, str]
    activities: tuple[ActivityObservation, ...]


def activity_observation(
    reader: Reader, activity_id: str, *, max_gap: timedelta = timedelta(seconds=30)
) -> ActivityObservation:
    """Return people with segments overlapping the activity, not inferred relations."""
    activity = reader.get(activity_id)
    if (
        not isinstance(activity, SemanticRecord)
        or activity.data_type != "activity"
        or activity.superseded
    ):
        raise ValueError("activity unavailable")
    start, end = activity.event_start, activity.event_end
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start >= end:
        raise ValueError("activity interval unavailable")
    # observations() assigns by start; use a direct overlap read in this layer so
    # a segment beginning before the activity is not silently omitted.
    if max_gap < timedelta(0):
        raise ValueError("invalid turn gap")
    window = TimeRange(start, end)
    segments = iter_query(reader, Query("transcript_segment", time=window))
    people: dict[str, list[str]] = {}
    attrs: dict[str, set[tuple[str, int]]] = {}
    unresolved: list[str] = []
    resolved = resolve_segments(reader, segments)
    if len(resolved) > 1000:
        raise ValueError("activity evidence limit exceeded")
    resolved.sort(key=lambda item: (item.segment.event_start, item.segment.id))
    for item in resolved:
        if item.person_id is None or item.attribution is None:
            unresolved.append(item.segment.id)
        else:
            people.setdefault(item.person_id, []).append(item.segment.id)
            attrs.setdefault(item.person_id, set()).add(
                (item.attribution.id, item.attribution.version)
            )
    participants = tuple(
        Participant(pid, tuple(sorted(ids)), tuple(sorted(attrs[pid])))
        for pid, ids in sorted(people.items())
    )
    turns = []
    for first, second in pairwise(resolved):
        if (
            first.person_id is None
            or second.person_id is None
            or first.person_id == second.person_id
        ):
            continue
        gap = second.segment.event_start - first.segment.event_end
        if gap < timedelta(0) or gap > max_gap:
            continue
        if (
            first.segment.payload.get("overlap") is True
            or second.segment.payload.get("overlap") is True
        ):
            continue
        text = first.segment.payload["text"].rstrip()
        turns.append(
            AdjacentTurns(
                first.person_id,
                second.person_id,
                first.segment.id,
                second.segment.id,
                int(gap.total_seconds() * 1000),
                text.endswith(("?", "？")),
            )
        )
    return ActivityObservation(
        activity.id, activity.version, participants, tuple(sorted(unresolved)), tuple(turns)
    )


def social_window(reader: Reader, start: datetime, end: datetime) -> tuple[Cooccurrence, ...]:
    """Return activity co-occurrences, not exchanges or relationship labels."""
    window = TimeRange(start, end)
    if start >= end:
        raise ValueError("window must be non-empty")
    pairs: dict[tuple[str, str], list[ActivityObservation]] = {}
    for record in iter_query(reader, Query("activity", time=window)):
        if not isinstance(record, SemanticRecord):
            raise ValueError("invalid activity")
        view = activity_observation(reader, record.id)
        for left, right in combinations(view.participants, 2):
            pairs.setdefault((left.person_id, right.person_id), []).append(view)
    return tuple(Cooccurrence(pair, tuple(items)) for pair, items in sorted(pairs.items()))
