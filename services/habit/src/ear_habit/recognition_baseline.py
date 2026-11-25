"""Offline lexical candidate evidence from user-protected historical references.

This is a research baseline, not an attribution decision or a calibrated score.
It does not read plugin summaries, auto-confirmed profiles, or the target recording.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_habit.tokenize import ChineseTokenizer

STOPWORDS = frozenset({"我", "你", "他", "她", "的", "了", "是", "在", "这", "那", "嗯", "啊"})


@dataclass(frozen=True, slots=True)
class ConfirmedReference:
    segment_id: str
    attribution_id: str
    attribution_version: int


@dataclass(frozen=True, slots=True)
class RecognitionCandidate:
    person_id: str
    matched_terms: tuple[str, ...]
    references: tuple[ConfirmedReference, ...]


def _at_time(reader: Reader, current: SemanticRecord, as_of: datetime) -> SemanticRecord | None:
    versions = [record for record in reader.history(current.id) if record.created_at <= as_of]
    if not versions:
        return None
    found = max(versions, key=lambda record: record.version)
    return None if found.superseded else found


def confirmed_candidates(
    reader: Reader,
    target_id: str,
    *,
    as_of: datetime,
    lookback: timedelta,
    min_shared_terms: int = 2,
    limit: int = 10,
) -> tuple[RecognitionCandidate, ...]:
    """Use only earlier, independently user-protected examples available by as_of."""
    if as_of.utcoffset() != timedelta(0) or lookback <= timedelta(0):
        raise ValueError("invalid evaluation time")
    if type(min_shared_terms) is not int or min_shared_terms < 1 or not 1 <= limit <= 50:
        raise ValueError("invalid candidate bounds")
    target = reader.get(target_id)
    if not isinstance(target, RawRecord) or target.data_type != "transcript_segment":
        raise ValueError("target transcript unavailable")
    if target.ingested_at > as_of or target.event_end > as_of:
        raise ValueError("target unavailable at evaluation time")
    audio = target.payload.get("audio_ref_id")
    target_voice = target.payload.get("voiceprint_id")
    text = target.payload.get("text")
    if not isinstance(audio, str) or not isinstance(text, str):
        raise ValueError("target source unavailable")
    tokenizer = ChineseTokenizer()
    wanted = set(tokenizer.tokens(text)) - STOPWORDS
    if len(wanted) < min_shared_terms:
        return ()
    window = TimeRange(target.event_start - lookback, target.event_start)
    references: dict[str, list[ConfirmedReference]] = {}
    matches: dict[str, set[str]] = {}
    scanned = 0
    for segment in iter_query(reader, Query("transcript_segment", time=window)):
        scanned += 1
        if scanned > 1000:
            raise ValueError("reference scan limit exceeded")
        if not isinstance(segment, RawRecord):
            raise ValueError("invalid reference transcript")
        voice = segment.payload.get("voiceprint_id")
        source = segment.payload.get("audio_ref_id")
        ref_text = segment.payload.get("text")
        if (
            segment.id == target.id
            or segment.event_end > target.event_start
            or segment.ingested_at > as_of
            or not isinstance(voice, str)
            or voice == target_voice
            or not isinstance(source, str)
            or source == audio
            or not isinstance(ref_text, str)
        ):
            continue
        overlap = wanted.intersection(set(tokenizer.tokens(ref_text)) - STOPWORDS)
        if not overlap:
            continue
        attrs = list(
            iter_query(
                reader, Query("attribution", where={"subject_id": voice}, include_superseded=True)
            )
        )
        if len(attrs) > 1:
            raise ValueError("ambiguous reference attribution")
        if not attrs:
            continue
        current = attrs[0]
        if not isinstance(current, SemanticRecord):
            raise ValueError("invalid reference attribution")
        attr = _at_time(reader, current, as_of)
        if (
            attr is None
            or attr.payload.get("user_protected") is not True
            or attr.payload.get("target_type") != "person"
        ):
            continue
        person_id = attr.payload.get("target_id")
        if not isinstance(person_id, str):
            raise ValueError("invalid reference person")
        person = reader.get(person_id)
        if not isinstance(person, SemanticRecord) or person.data_type != "person":
            raise ValueError("reference person unavailable")
        if _at_time(reader, person, as_of) is None:
            continue
        matches.setdefault(person_id, set()).update(overlap)
        references.setdefault(person_id, []).append(
            ConfirmedReference(segment.id, attr.id, attr.version)
        )
    candidates = [
        RecognitionCandidate(person_id, tuple(sorted(terms)), tuple(references[person_id]))
        for person_id, terms in matches.items()
        if len(terms) >= min_shared_terms
    ]
    candidates.sort(key=lambda item: (-len(item.matched_terms), item.person_id))
    return tuple(candidates[:limit])
