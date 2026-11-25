"""Read-only, source-grounded context for future attribution research.

This is not connected to ③ and never reads the habit/social plugin's results.
It makes no identity prediction and does not use a later segment as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ear_contracts import RawRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query


@dataclass(frozen=True, slots=True)
class ContextSegment:
    segment_id: str
    local_speaker_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class RecognitionContext:
    target_id: str
    audio_ref_id: str
    target_local_speaker_id: str | None
    preceding: tuple[ContextSegment, ...]


def recognition_context(
    reader: Reader,
    target_id: str,
    *,
    lookback: timedelta,
    limit: int = 100,
    as_of: datetime | None = None,
) -> RecognitionContext:
    """Gather bounded prior context in the same audio, with no named identities."""
    if lookback <= timedelta(0) or not 1 <= limit <= 1000:
        raise ValueError("invalid context bounds")
    target = reader.get(target_id)
    if not isinstance(target, RawRecord) or target.data_type != "transcript_segment":
        raise ValueError("target transcript unavailable")
    if as_of is not None:
        if as_of.utcoffset() != timedelta(0):
            raise ValueError("as_of must be UTC")
        if target.ingested_at > as_of or target.event_end > as_of:
            raise ValueError("target unavailable at evaluation time")
    audio = target.payload.get("audio_ref_id")
    speaker = target.payload.get("local_speaker_id")
    if not isinstance(audio, str) or (speaker is not None and not isinstance(speaker, str)):
        raise ValueError("invalid source references")
    time = TimeRange(target.event_start - lookback, target.event_start)
    selected: list[ContextSegment] = []
    query = Query("transcript_segment", time=time, where={"audio_ref_id": audio})
    for item in iter_query(reader, query):
        if not isinstance(item, RawRecord):
            raise ValueError("invalid transcript record")
        if (
            item.event_end > target.event_start
            or item.id == target.id
            or (as_of is not None and item.ingested_at > as_of)
        ):
            continue
        text = item.payload.get("text")
        local = item.payload.get("local_speaker_id")
        if not isinstance(text, str) or (local is not None and not isinstance(local, str)):
            raise ValueError("invalid transcript payload")
        if len(selected) >= limit:
            raise ValueError("context limit exceeded")
        selected.append(ContextSegment(item.id, local, text))
    return RecognitionContext(target.id, audio, speaker, tuple(selected))
