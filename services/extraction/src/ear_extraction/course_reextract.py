"""Private, fail-closed scope and draft guards for course-block replacement."""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from itertools import accumulate, pairwise
from typing import Literal, Self

from ear_contracts import RawRecord, SemanticRecord, format_utc
from ear_contracts.base import Id
from ear_storage_client import AssertTypeRevision, Query, Reader, TimeRange, iter_query
from pydantic import ConfigDict, Field, model_validator

from ear_extraction.course_blocks import course_capture_blocks, course_span_interval
from ear_extraction.domain import Activity, Blocked, Interval, Model, Segmentation
from ear_extraction.persistence import range_of, records
from ear_extraction.reextract_preflight import ExactInterval, TargetType, has_covered_endpoints
from ear_extraction.segmentation import validate_anonymous_activity
from ear_extraction.sessions import SessionResult, _Row, _window_rows, validate_session_windows

MAX_COURSE_REEXTRACT_PREDECESSORS = 64
MAX_SCHEMA_DISCARD_ATTESTATIONS = 4
MAX_COURSE_DRAFT_SPANS = 48


class SchemaDiscardAttestation(Model):
    """Operator has independently correlated a terminal schema discard to this intent."""

    model_config = ConfigDict(hide_input_in_errors=True)

    run_id: Id = Field(strict=True)
    call_id: Id = Field(strict=True)
    request_fingerprint: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    proof: Literal["schema_response_received"]
    stage: Literal["extraction_schema"]


class CourseDraftSpan(Interval):
    """Approved activity with inclusive, one-based offsets into the whole batch."""

    model_config = ConfigDict(hide_input_in_errors=True)

    first: int = Field(strict=True, ge=1)
    last: int = Field(strict=True, ge=1)
    title_md: str = Field(strict=True, min_length=1, max_length=120, repr=False)
    description_md: str = Field(strict=True, max_length=360, repr=False)
    kind: Literal["course", "other"] = "course"  # Only used for the audit count.


class ReviewedTimelineSpan(CourseDraftSpan, ExactInterval):
    """A selected preview candidate with explicitly supplied local prose."""


class ReviewedFamilyPin(Model):
    data_type: TargetType
    id: Id = Field(strict=True, repr=False)
    expected_version: int = Field(strict=True, ge=1)


class ReviewedTimelineDraft(Model):
    """Private handoff from complete timeline assembly and trusted preflight."""

    model_config = ConfigDict(hide_input_in_errors=True)

    review_complete: bool = Field(strict=True)
    input_sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    draft_sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    spans: tuple[ReviewedTimelineSpan, ...] = Field(
        min_length=1, max_length=MAX_COURSE_DRAFT_SPANS, repr=False
    )
    old_pins: tuple[ReviewedFamilyPin, ...] = Field(min_length=1, max_length=1000, repr=False)

    @model_validator(mode="after")
    def check_handoff(self) -> Self:
        if not self.review_complete:
            raise ValueError("course_reextract_timeline_requires_complete_review")
        if len({pin.id for pin in self.old_pins}) != len(self.old_pins) or not any(
            pin.data_type == "activity" for pin in self.old_pins
        ):
            raise ValueError("course_reextract_timeline_invalid_family_pins")
        if reviewed_draft_sha256(self.input_sha256, self.spans, self.old_pins) != self.draft_sha256:
            raise ValueError("course_reextract_timeline_draft_changed")
        return self

    def check_pins(self, replacement: CourseReplacement) -> None:
        if replacement.versions != {
            (pin.data_type, pin.id): pin.expected_version for pin in self.old_pins
        }:
            raise Blocked("course_reextract_timeline_old_pins_changed")

    def compile(self, windows: tuple[Interval, ...], raw: list[RawRecord]) -> SessionResult:
        parts = reviewed_timeline_rows(windows, raw)
        if reviewed_input_sha256(windows, parts) != self.input_sha256:
            raise Blocked("course_reextract_timeline_input_changed")
        return _compile_course_draft(self.spans, windows, parts, sparse=True)


def _digest(domain: bytes, payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(domain + b"\0" + canonical.encode("ascii")).hexdigest()


def reviewed_input_sha256(windows: tuple[Interval, ...], parts: list[list[_Row]]) -> str:
    """Hash all ordered immutable rows, including omissions and original seam ends."""
    return _digest(
        b"ear.course-reextract.timeline-input/1",
        {
            "row_count": sum(map(len, parts)),
            "windows": [
                {
                    "start": format_utc(w.start),
                    "end": format_utc(w.end),
                    "count": len(rows),
                    "rows": [
                        [r.raw.id, format_utc(r.start), format_utc(r.raw.event_end), r.text]
                        for r in rows
                    ],
                }
                for w, rows in zip(windows, parts, strict=True)
            ],
        },
    )


def reviewed_draft_sha256(
    input_sha256: str,
    spans: tuple[ReviewedTimelineSpan, ...],
    pins: tuple[ReviewedFamilyPin, ...],
) -> str:
    return _digest(
        b"ear.course-reextract.timeline-draft/1",
        {
            "input_sha256": input_sha256,
            "spans": [s.model_dump(mode="json") for s in spans],
            "old_pins": [
                pin.model_dump(mode="json") for pin in sorted(pins, key=lambda pin: pin.id)
            ],
        },
    )


def validate_timeline_windows(windows: tuple[Interval, ...]) -> None:
    validate_session_windows(windows)
    if any(a.end != b.start for a, b in pairwise(windows)):
        raise ValueError("course_reextract_timeline_requires_contiguous_windows")


def reviewed_timeline_rows(
    windows: tuple[Interval, ...],
    raw: list[RawRecord],
    *,
    gapped: bool = False,
    allow_empty_windows: bool = False,
) -> list[list[_Row]]:
    if gapped:
        validate_session_windows(windows)
    else:
        validate_timeline_windows(windows)
    if gapped or allow_empty_windows:
        # Packing can produce a remainder window with no row *starting* in it.
        # Actual capture/ASR coverage is checked by the timeline planner instead.
        parts, blanks = _window_rows(windows, raw)
        if any(blanks):
            raise Blocked("course_reextract_blank_transcript_rows")
    else:
        parts = _course_draft_rows(windows, raw)
    if any(row.raw.event_end <= row.start for part in parts for row in part):
        raise Blocked("course_reextract_timeline_unreliable_row_time")
    return parts


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def course_draft_sha256(activities: list[Activity]) -> str:
    """Pin the reviewed boundaries and entire row assignment, not regenerated prose."""
    draft = [
        {"start": format_utc(a.start), "end": format_utc(a.end), "evidence_ids": a.evidence_ids}
        for a in activities
    ]
    return hashlib.sha256(
        json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compile_course_draft(
    spans: tuple[CourseDraftSpan, ...], windows: tuple[Interval, ...], raw: list[RawRecord]
) -> SessionResult:
    """Rebuild the reviewed evidence locally; no model, sampling, or fallback."""
    validate_session_windows(windows)
    return _compile_course_draft(spans, windows, _course_draft_rows(windows, raw))


def _course_draft_rows(windows: tuple[Interval, ...], raw: list[RawRecord]) -> list[list[_Row]]:
    by_window, blanks = _window_rows(windows, raw)
    if any(not part for part in by_window):
        raise Blocked("session_windows_without_transcript")
    if any(blanks):
        raise Blocked("course_reextract_blank_transcript_rows")
    return by_window


def _compile_course_draft(
    spans: tuple[CourseDraftSpan, ...],
    windows: tuple[Interval, ...],
    by_window: list[list[_Row]],
    *,
    sparse: bool = False,
) -> SessionResult:
    # _window_rows deduplicates seam reads and orders by (start, end, id).
    rows = [row for part in by_window for row in part]
    window_ends = list(accumulate(map(len, by_window)))
    block_ends = [window_ends[i - 1] for i in accumulate(map(len, course_capture_blocks(windows)))]
    activities: list[Activity] = []
    consumed = covered = courses = merged = 0
    if sparse and not spans:
        raise Blocked("course_reextract_timeline_empty_draft")
    for span in spans:
        if not consumed < span.first <= span.last <= len(rows) or (
            not sparse and span.first != consumed + 1
        ):
            raise Blocked("course_reextract_draft_not_complete_partition")
        if span.last > block_ends[bisect_left(block_ends, span.first)]:
            raise Blocked("course_reextract_draft_crosses_capture_block")
        part = rows[span.first - 1 : span.last]
        if sparse:
            observed_until = part[0].start
            for row in part:
                if row.start - observed_until > timedelta(minutes=5):
                    raise Blocked("course_reextract_timeline_crosses_unobserved_gap")
                observed_until = max(observed_until, row.raw.event_end)
        try:
            interval = course_span_interval(part)
        except ValueError:
            raise Blocked("course_reextract_draft_anchor_time_mismatch") from None
        if span.start != interval.start or span.end != interval.end:
            raise Blocked("course_reextract_draft_anchor_time_mismatch")
        if activities and span.start < activities[-1].end:
            raise Blocked("course_reextract_draft_times_overlap")
        if not span.title_md.strip():
            raise Blocked("course_reextract_draft_requires_title")
        activity = Activity(
            start=span.start,
            end=span.end,
            title_md=span.title_md,
            description_md=span.description_md,
            evidence_ids=[row.raw.id for row in part],
        )
        validate_anonymous_activity(activity)
        activities.append(activity)
        if span.kind == "course":
            courses += 1
            merged += sum(span.first <= end < span.last for end in window_ends)
        consumed = span.last
        covered += len(part)
        if sparse and any(
            r.start < span.end and r.raw.event_end > span.start
            for r in (*rows[: span.first - 1], *rows[span.last :])
        ):
            raise Blocked("course_reextract_timeline_overlapping_unassigned_row")
    if not sparse and consumed != len(rows):
        raise Blocked("course_reextract_draft_not_complete_partition")
    return SessionResult(
        Segmentation(activities=activities),
        merged,
        0,
        courses,
        unclassified_rows=len(rows) - covered,
    )


def within_capture_coverage(interval: Interval, windows: tuple[Interval, ...]) -> bool:
    # Both ends must be captured. Crossing a short gap *within* a block is OK.
    return any(
        any(w.start <= interval.start < w.end for w in block)
        and any(w.start < interval.end <= w.end for w in block)
        for block in course_capture_blocks(windows)
    )


def _linked(
    reader: Reader, kind: str, field: str, id: str, *, history: bool = False
) -> list[SemanticRecord]:
    return [
        r
        for r in iter_query(reader, Query(kind, where={field: id}, include_superseded=history))
        if isinstance(r, SemanticRecord)
    ]


@dataclass(slots=True)
class ScopedTranscriptGuard:
    raw_revision: int | None = None
    raw_counts: tuple[int, ...] = ()

    def live_raw_assertion(
        self, reader: Reader, windows: tuple[Interval, ...]
    ) -> AssertTypeRevision:
        assert self.raw_revision is not None and len(self.raw_counts) == len(windows)
        revision = reader.type_revision("transcript_segment")
        # Transcript is append-only. If its global revision moved, scoped counts
        # detect unseen in-window rows without rereading any raw bodies. The batch
        # assertion closes the race after (or in between) these live reads.
        if revision != self.raw_revision and any(
            reader.count(Query("transcript_segment", time=TimeRange(w.start, w.end))) != count
            for w, count in zip(windows, self.raw_counts, strict=True)
        ):
            raise Blocked("course_reextract_transcript_changed")
        return AssertTypeRevision("transcript_segment", revision)


@dataclass(slots=True)
class CourseDraftGuard:
    digest: str
    spans: tuple[CourseDraftSpan, ...] | None = None
    timeline: ReviewedTimelineDraft | None = None


@dataclass(frozen=True, slots=True)
class CourseReplacement:
    activities: list[SemanticRecord]
    artifacts: list[SemanticRecord]
    selections: list[SemanticRecord]

    @property
    def preview_versions(self) -> dict[str, int]:
        # No pending suggestions can pass the private guard below. For permitted
        # targets this is exactly the public preview, which omits selections.
        return {r.id: r.version for r in [*self.activities, *self.artifacts]}

    @property
    def versions(self) -> dict[tuple[str, str], int]:
        return {
            (r.data_type, r.id): r.version
            for r in self.activities + self.artifacts + self.selections
        }

    def require_family_successors(self, activities: list[Activity]) -> None:
        for old in self.activities:
            evidence = set(old.payload["evidence_ids"])  # Checked by check_evidence().
            span = range_of(old)
            if not any(
                span.start < new.end
                and new.start < span.end
                and not evidence.isdisjoint(new.evidence_ids)
                for new in activities
            ):
                raise Blocked("course_reextract_timeline_old_family_without_successor")

    def require_all_citations(self, activities: list[Activity]) -> None:
        # check_evidence() has already validated the old rows and their target times.
        # Use a union, not one required successor per family or one token overlap.
        evidence = {id for activity in activities for id in activity.evidence_ids}
        for target in self.activities + self.artifacts:
            if not evidence.issuperset(target.payload["evidence_ids"]):
                raise Blocked("automatic_timeline_old_family_uncertain")

    def check_evidence(self, raw: list[RawRecord]) -> None:
        by_id = {r.id: r for r in raw}
        for target in self.activities + self.artifacts:
            interval = range_of(target)
            evidence = target.payload.get("evidence_ids")
            if not isinstance(evidence, list) or not evidence:
                raise Blocked("course_reextract_old_evidence_out_of_scope")
            for id in evidence:
                row = by_id.get(id) if isinstance(id, str) else None
                if row is None or not TimeRange(interval.start, interval.end).overlaps(
                    row.event_start, row.event_end
                ):
                    raise Blocked("course_reextract_old_evidence_out_of_scope")


def course_replacement_targets(
    reader: Reader,
    interval: Interval,
    windows: tuple[Interval, ...],
    *,
    require_activity: bool = True,
    gapped_components: tuple[Interval, ...] = (),
) -> CourseReplacement:
    # The public preview covers the whole envelope. Do not silently ignore a
    # gap occupant from that approval or retire it merely because it was listed.
    activities = records(reader, "activity", interval)
    if require_activity and not activities:
        raise Blocked("course_reextract_no_old_activities")
    artifacts, selections = [], []
    for activity in activities:
        span = range_of(activity)
        covered = (
            has_covered_endpoints(span, gapped_components)
            if gapped_components
            else within_capture_coverage(span, windows)
        )
        if not covered:
            raise Blocked("course_reextract_old_activity_out_of_scope")
        if _protected(activity):
            raise Blocked("course_reextract_protected_manual_or_edited")
        for artifact in _linked(reader, "artifact", "activity_id", activity.id, history=True):
            # A live action can outlive its source artifact. Never change any
            # action object, including those attached to already retired notes.
            for action in _linked(reader, "action_suggestion", "artifact_id", artifact.id):
                if action.payload.get("state") not in ("executed", "rejected"):
                    raise Blocked("course_reextract_protected_action")
            if artifact.superseded:
                continue
            if _protected(artifact):
                raise Blocked("course_reextract_protected_manual_or_edited")
            if range_of(artifact) != span:
                raise Blocked("course_reextract_old_artifact_out_of_scope")
            artifacts.append(artifact)
        selections.extend(_linked(reader, "rule_selection", "activity_id", activity.id))
    return CourseReplacement(activities, artifacts, selections)


def _protected(record: SemanticRecord) -> bool:
    return (
        record.version != 1
        or (record.payload.get("manual") is not None and record.payload["manual"] is not False)
        or record.payload.get("source") not in (None, "automatic")
    )
