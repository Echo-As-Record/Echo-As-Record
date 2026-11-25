"""Read-only L1/L2 observations over a single storage snapshot.

The attribution payload is the current ③ implementation, not a public contract.
An unresolved segment stays unresolved; its local speaker label is not an identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query


@dataclass(frozen=True, slots=True)
class Observation:
    segment: RawRecord
    attribution: SemanticRecord | None
    person_id: str | None


def observations(reader: Reader, start: datetime, end: datetime) -> list[Observation]:
    """Assign segments to windows by start time; never invent an identity.

    Call under a snapshot when a coherent view or a subsequent write is required.
    A segment crossing midnight belongs wholly to the day it starts.
    """
    window = TimeRange(start, end)
    if start >= end:
        raise ValueError("window must be non-empty")
    segments = []
    for item in iter_query(reader, Query("transcript_segment", time=window)):
        if not isinstance(item, RawRecord):
            raise ValueError("invalid transcript record")
        if start <= item.event_start < end:
            segments.append(item)
    return resolve_segments(reader, segments)


def resolve_segments(reader: Reader, segments: Iterable[object]) -> list[Observation]:
    """Resolve only current ③ records; missing attributions remain unknown."""
    result = []
    cache: dict[str, tuple[SemanticRecord | None, str | None]] = {}
    for item in segments:
        if not isinstance(item, RawRecord):
            raise ValueError("invalid transcript record")
        if item.data_type != "transcript_segment":
            raise ValueError("invalid transcript record")
        if not isinstance(item.payload.get("text"), str):
            raise ValueError("invalid transcript text")
        voice = item.payload.get("voiceprint_id")
        if voice is None:
            result.append(Observation(item, None, None))
            continue
        if not isinstance(voice, str):
            raise ValueError("invalid voiceprint reference")
        if voice not in cache:
            matches = list(iter_query(reader, Query("attribution", where={"subject_id": voice})))
            if len(matches) > 1:
                raise ValueError("ambiguous attribution")
            if not matches:
                cache[voice] = (None, None)
            else:
                attr = matches[0]
                if not isinstance(attr, SemanticRecord) or attr.superseded:
                    raise ValueError("invalid attribution")
                target = attr.payload.get("target_id")
                if attr.payload.get("target_type") != "person" or not isinstance(target, str):
                    raise ValueError("invalid person attribution")
                person = reader.get(target)
                if (
                    not isinstance(person, SemanticRecord)
                    or person.data_type != "person"
                    or person.superseded
                ):
                    raise ValueError("attributed person unavailable")
                cache[voice] = (attr, target)
        attr, target = cache[voice]
        result.append(Observation(item, attr, target))
    return result
