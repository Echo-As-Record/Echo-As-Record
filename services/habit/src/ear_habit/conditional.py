"""Evidence-backed conditional speech statistics, without relationship inference."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_habit.buckets import Stats, Utterance, stats_of
from ear_habit.social import activity_observation


@dataclass(frozen=True, slots=True)
class ConditionalProfile:
    person_id: str
    copresent_person_id: str
    activity_versions: tuple[tuple[str, int], ...]
    evidence_ids: tuple[str, ...]
    stats: Stats
    interpretation: str = "co_present_not_directed_speech"


def conditional_profile(
    reader: Reader,
    person_id: str,
    copresent_person_id: str,
    start: datetime,
    end: datetime,
    *,
    activity_ids: frozenset[str] | None = None,
) -> ConditionalProfile:
    if person_id == copresent_person_id or start >= end:
        raise ValueError("invalid conditional profile bounds")
    window = TimeRange(start, end)
    activity_refs = []
    segments: dict[str, RawRecord] = {}
    examined = 0
    for activity in iter_query(reader, Query("activity", time=window)):
        examined += 1
        if examined > 1000:
            raise ValueError("activity scan limit exceeded")
        if not isinstance(activity, SemanticRecord):
            raise ValueError("invalid activity")
        if activity_ids is not None and activity.id not in activity_ids:
            continue
        view = activity_observation(reader, activity.id)
        by_person = {p.person_id: p for p in view.participants}
        if person_id not in by_person or copresent_person_id not in by_person:
            continue
        activity_refs.append((view.activity_id, view.activity_version))
        for segment_id in by_person[person_id].segment_ids:
            segment = reader.get(segment_id)
            if not isinstance(segment, RawRecord) or segment.data_type != "transcript_segment":
                raise ValueError("source transcript unavailable")
            segments[segment.id] = segment
    utterances = [
        Utterance(
            item.payload["text"], int((item.event_end - item.event_start).total_seconds() * 1000)
        )
        for _, item in sorted(segments.items())
    ]
    return ConditionalProfile(
        person_id,
        copresent_person_id,
        tuple(sorted(activity_refs)),
        tuple(sorted(segments)),
        stats_of(utterances),
    )
