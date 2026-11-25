"""Bounded lexical episode retrieval with raw-evidence and online-time checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_habit.episodes import MAX_SEGMENTS
from ear_habit.store import EPISODE_TYPE
from ear_habit.tokenize import ChineseTokenizer

Mode = Literal["raw", "summary", "hybrid"]
MAX_EPISODES = 1000


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    segment_id: str
    text: str
    audio_ref_id: str | None
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class EpisodeHit:
    episode_id: str
    episode_version: int
    activity_id: str
    start: datetime
    end: datetime
    title: str
    description: str
    matched_terms: tuple[str, ...]
    evidence: tuple[EvidenceHit, ...]
    freshness: str


def _version_at(
    reader: Reader, current: SemanticRecord, as_of: datetime | None
) -> SemanticRecord | None:
    if as_of is None:
        return None if current.superseded else current
    versions = [item for item in reader.history(current.id) if item.created_at <= as_of]
    if not versions:
        return None
    chosen = max(versions, key=lambda item: item.version)
    return None if chosen.superseded else chosen


def search_episodes(
    reader: Reader,
    query: str,
    *,
    mode: Mode = "hybrid",
    time: TimeRange | None = None,
    as_of: datetime | None = None,
    limit: int = 10,
    include_stale: bool = False,
) -> tuple[EpisodeHit, ...]:
    """Return candidates, not generated answers; no future evidence in as-of queries.

    Current results can still miss newly arrived inputs until rebuilt. 'unverified'
    therefore never claims coverage is complete. Summary mode cannot cite a quote.
    """
    if mode not in {"raw", "summary", "hybrid"} or not 1 <= limit <= 50:
        raise ValueError("invalid retrieval mode or limit")
    if as_of is not None and as_of.utcoffset() != timedelta(0):
        raise ValueError("as_of must be UTC")
    tokenizer = ChineseTokenizer()
    wanted = set(tokenizer.tokens(query))
    if not wanted:
        raise ValueError("query has no searchable terms")
    result: list[tuple[int, EpisodeHit]] = []
    scanned = 0
    scan_time = None if as_of is not None else time
    for current in iter_query(
        reader, Query(EPISODE_TYPE, time=scan_time, include_superseded=as_of is not None)
    ):
        scanned += 1
        if scanned > MAX_EPISODES:
            raise ValueError("episode scan limit exceeded")
        if not isinstance(current, SemanticRecord):
            raise ValueError("invalid episode")
        episode = _version_at(reader, current, as_of)
        if episode is None or episode.event_start is None or episode.event_end is None:
            continue
        if time is not None and not time.overlaps(episode.event_start, episode.event_end):
            continue
        if as_of is not None and episode.event_end > as_of:
            continue
        deps = reader.dependencies_of(episode.id, episode.version)
        freshness = "historical" if as_of is not None else "unverified"
        if as_of is None:
            stale = reader.stale_dependencies([item.depends_on.id for item in deps])
            if any(item.dependent.id == episode.id for item in stale):
                freshness = "stale"
                if not include_stale:
                    continue
        payload = episode.payload
        summary = payload.get("summary")
        if not isinstance(summary, dict) or not all(
            isinstance(summary.get(k), str) for k in ("title", "description")
        ):
            raise ValueError("invalid episode summary")
        title, description = summary["title"], summary["description"]
        summary_terms = set(tokenizer.tokens(title + " " + description))
        indexed = payload.get("terms")
        if not isinstance(indexed, dict) or not all(isinstance(x, str) for x in indexed):
            raise ValueError("invalid episode index")
        available = summary_terms if mode == "summary" else summary_terms | set(indexed)
        if mode != "raw" and not wanted.intersection(available):
            continue
        ids = payload.get("evidence_ids")
        if (
            not isinstance(ids, list)
            or len(ids) > MAX_SEGMENTS
            or any(not isinstance(x, str) for x in ids)
        ):
            raise ValueError("invalid evidence references")
        evidence = []
        raw_terms: set[str] = set()
        if mode != "summary":
            for segment_id in ids:
                segment = reader.get(segment_id)
                if not isinstance(segment, RawRecord) or segment.data_type != "transcript_segment":
                    raise ValueError("source evidence unavailable")
                if as_of is not None and (segment.ingested_at > as_of or segment.event_end > as_of):
                    continue
                text = segment.payload.get("text")
                audio = segment.payload.get("audio_ref_id")
                if not isinstance(text, str) or (audio is not None and not isinstance(audio, str)):
                    raise ValueError("invalid transcript payload")
                matched = wanted.intersection(tokenizer.tokens(text))
                if matched:
                    raw_terms.update(matched)
                    evidence.append(
                        EvidenceHit(segment.id, text, audio, segment.event_start, segment.event_end)
                    )
            if not evidence:
                continue
        matched_terms = wanted.intersection(summary_terms) if mode == "summary" else raw_terms
        activity_id = payload.get("activity_id")
        if not isinstance(activity_id, str):
            raise ValueError("invalid activity reference")
        hit = EpisodeHit(
            episode.id,
            episode.version,
            activity_id,
            episode.event_start,
            episode.event_end,
            title,
            description,
            tuple(sorted(matched_terms)),
            tuple(evidence),
            freshness,
        )
        result.append((len(matched_terms), hit))
    result.sort(key=lambda pair: (-pair[0], -pair[1].start.timestamp(), pair[1].episode_id))
    return tuple(hit for _, hit in result[:limit])
