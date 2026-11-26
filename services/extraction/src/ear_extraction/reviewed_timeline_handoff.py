"""Build a private owner handoff offline, from complete review and trusted metadata."""

from __future__ import annotations

from ear_contracts import RawRecord

from ear_extraction.course_reextract import (
    ReviewedFamilyPin,
    ReviewedTimelineDraft,
    ReviewedTimelineSpan,
    reviewed_draft_sha256,
    reviewed_input_sha256,
    reviewed_timeline_rows,
)
from ear_extraction.domain import Interval
from ear_extraction.reextract_preflight import PreflightInput, plan_reextract_preflight
from ear_extraction.timeline_preview import (
    CompletedReview,
    Timeline,
    Transcript,
    assemble_timeline,
    unsafe_review_rows,
)


def project_timeline(
    windows: tuple[Interval, ...], raw: list[RawRecord], preflight: PreflightInput
) -> Timeline:
    """Project the exact owner rows, using actual capture boundaries for review seams."""
    return _prepare(windows, raw, preflight)[0]


def _prepare(windows: tuple[Interval, ...], raw: list[RawRecord], preflight: PreflightInput):
    parts = reviewed_timeline_rows(windows, raw)
    scope = Interval(start=windows[0].start, end=windows[-1].end)
    plan = plan_reextract_preflight(preflight)
    if (
        (scope.start, scope.end) != (plan.scope.start, plan.scope.end)
        or plan.requested_holds
        or plan.held_captures
        or plan.coverage_issues
        or plan.retained_targets
        or len(plan.components) != 1
        or len(plan.replacement_scopes) != 1
        or plan.replacement_scopes[0].interval != scope
    ):
        raise ValueError("course_reextract_timeline_preflight_not_clear_for_exact_scope")
    timeline = Timeline(
        scope=scope,
        coverage=tuple(Interval(start=c.start, end=c.end) for c in plan.components[0].captures),
        transcripts=tuple(
            Transcript(start=r.start, end=r.raw.event_end, text=r.text)
            for window in parts
            for r in window
        ),
        withheld=(),
    )
    return timeline, parts, plan.replacement_scopes[0].pins


def build_reviewed_timeline_draft(
    windows: tuple[Interval, ...],
    raw: list[RawRecord],
    preflight: PreflightInput,
    completed: CompletedReview,
    spans: tuple[ReviewedTimelineSpan, ...],
) -> ReviewedTimelineDraft:
    """Validate operator-selected candidates and prose; make no I/O or model calls."""
    timeline, parts, pins = _prepare(windows, raw, preflight)
    if completed.timeline != timeline or completed.allow_partial:
        raise ValueError("course_reextract_timeline_requires_exact_complete_review")
    result = assemble_timeline(timeline, completed.reviews)
    candidates = {(c["first"], c["last"]): c["engagement"] for c in result["candidates"]}
    unsafe_rows = unsafe_review_rows(result)
    for s in spans:
        # Unknown/background and structurally unresolved candidates are never
        # silently upgraded. A clean span can border truly unclassified text.
        if candidates.get((s.first, s.last)) not in {"participation", "watching_listening"} or (
            any(i in unsafe_rows for i in range(s.first, s.last + 1))
        ):
            raise ValueError("course_reextract_timeline_candidate_not_safe")
    old_pins = tuple(
        ReviewedFamilyPin(data_type=p.data_type, id=p.id, expected_version=p.expected_version)
        for p in pins
    )
    digest = reviewed_input_sha256(windows, parts)
    draft = ReviewedTimelineDraft(
        review_complete=True,
        input_sha256=digest,
        draft_sha256=reviewed_draft_sha256(digest, spans, old_pins),
        old_pins=old_pins,
        spans=spans,
    )
    draft.compile(windows, raw)  # Recheck exact times and evidence, including omitted overlaps.
    return draft
