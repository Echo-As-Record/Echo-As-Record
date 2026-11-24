"""Bounded divide-and-merge; never truncate evidence or silently discard failed windows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ear_contracts import RawRecord
from ear_storage_client import TimeRange

from ear_extraction.artifacts import plain_markdown
from ear_extraction.domain import (
    Activity,
    Generated,
    InputBudgetExceeded,
    Interval,
    MergeDecision,
    Segmentation,
)
from ear_extraction.segmentation import (
    evidence_rows,
    validate_anonymous_activity,
    validate_segments,
)

Ask = Callable[[str, dict[str, Any], type], Any]


def segment(
    ask: Ask, interval: Interval, evidence: list[RawRecord], *, transcript_only: bool = False
) -> Segmentation:
    data = {
        "interval": interval.model_dump(mode="json"),
        "evidence": evidence_rows(evidence, transcript_only=transcript_only),
    }
    try:
        result = ask("segmentation", data, Segmentation)
        validate_segments(result, interval, evidence)
        if transcript_only:
            for activity in result.activities:
                validate_anonymous_activity(activity)
        return result
    except InputBudgetExceeded:
        # Do not cut through speech. Single oversized records require upstream chunking.
        boundaries = sorted(
            {
                r.event_start
                for r in evidence
                if interval.start < r.event_start < interval.end
                and not any(
                    s.event_start < r.event_start < s.event_end
                    for s in evidence
                    if s.data_type == "transcript_segment"
                )
            }
        )
        if transcript_only and evidence:
            # A boundary at the first row only peels off an empty window and
            # doesn't reduce the oversized input.
            first = min(r.event_start for r in evidence)
            boundaries = [b for b in boundaries if b > first]
        if not boundaries:
            raise
        middle = boundaries[len(boundaries) // 2]
        ranges = [
            Interval(start=interval.start, end=middle),
            Interval(start=middle, end=interval.end),
        ]
        parts = []
        for window in ranges:
            subset = [
                r
                for r in evidence
                if TimeRange(window.start, window.end).overlaps(r.event_start, r.event_end)
            ]
            parts.append(segment(ask, window, subset, transcript_only=transcript_only))
        left, right = parts[0].activities, parts[1].activities
        if left and right and left[-1].end == right[0].start:
            a, b = left[-1], right[0]
            merge_input = {"left": a.model_dump(mode="json"), "right": b.model_dump(mode="json")}
            if transcript_only:
                # The merge decision cannot cite new evidence. Preserve original links locally.
                for item in merge_input.values():
                    del item["evidence_ids"]
            decision: MergeDecision = ask("merge_activities", merge_input, MergeDecision)
            if decision.merge:
                merged = Activity(
                    start=a.start,
                    end=b.end,
                    title_md=decision.title_md,
                    description_md=decision.description_md,
                    evidence_ids=list(dict.fromkeys(a.evidence_ids + b.evidence_ids)),
                )
                if transcript_only:
                    validate_anonymous_activity(merged)
                left, right = [*left[:-1], merged], right[1:]
        result = Segmentation(
            activities=left + right,
            empty_reason="; ".join(p.empty_reason for p in parts if p.empty_reason),
        )
        validate_segments(result, interval, evidence)
        return result


def _validate_transcript_generation(result: Generated, evidence_ids: set[str]) -> None:
    if result.unchanged or result.action is not None or result.action_reason:
        raise ValueError("transcript_only_invalid_generation")
    if not result.body_md.strip() or not result.evidence_ids:
        raise ValueError("artifact_requires_text_and_evidence")
    plain_markdown(result.body_md)
    if not set(result.evidence_ids) <= evidence_ids:
        raise ValueError("unknown_evidence_reference")


def generate(
    ask: Ask, data: dict[str, Any], *, depth: int = 0, transcript_only: bool = False
) -> Generated:
    try:
        result: Generated = ask("generation", data, Generated)
        if transcript_only:
            # Validate each window before reusing model outputs in a consolidation call.
            _validate_transcript_generation(result, {r["id"] for r in data["evidence"]})
        return result
    except InputBudgetExceeded:
        evidence = data.get("evidence", [])
        if len(evidence) < 2 or depth >= 16:
            raise
        middle = len(evidence) // 2
        partials = []
        for part in (evidence[:middle], evidence[middle:]):
            partials.append(
                generate(
                    ask,
                    {**data, "evidence": part},
                    depth=depth + 1,
                    transcript_only=transcript_only,
                )
            )
        partial_results = (
            [{"body_md": p.body_md, "evidence_ids": p.evidence_ids} for p in partials]
            if transcript_only
            else [p.model_dump(mode="json") for p in partials]
        )
        result: Generated = ask(
            "consolidation",
            {
                "activity": data["activity"],
                "rule": data["rule"],
                "partial_results": partial_results,
            },
            Generated,
        )
        if result.unchanged:
            raise ValueError("consolidation_cannot_be_unchanged") from None
        ids = {id for p in partials for id in p.evidence_ids}
        if transcript_only:
            _validate_transcript_generation(result, ids)
        if not set(result.evidence_ids) <= ids:
            raise ValueError("consolidation_invented_evidence") from None
        return result
