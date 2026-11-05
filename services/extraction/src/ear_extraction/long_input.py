"""Bounded divide-and-merge; never truncate evidence or silently discard failed windows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ear_contracts import RawRecord
from ear_storage_client import TimeRange

from ear_extraction.domain import (
    Activity,
    Generated,
    InputBudgetExceeded,
    Interval,
    MergeDecision,
    Segmentation,
)
from ear_extraction.segmentation import validate_segments

Ask = Callable[[str, dict[str, Any], type], Any]


def segment(ask: Ask, interval: Interval, evidence: list[RawRecord]) -> Segmentation:
    data = {
        "interval": interval.model_dump(mode="json"),
        "evidence": [r.model_dump(mode="json") for r in evidence],
    }
    try:
        result = ask("segmentation", data, Segmentation)
        validate_segments(result, interval, evidence)
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
            parts.append(segment(ask, window, subset))
        left, right = parts[0].activities, parts[1].activities
        if left and right and left[-1].end == right[0].start:
            a, b = left[-1], right[0]
            decision: MergeDecision = ask(
                "merge_activities",
                {
                    "left": a.model_dump(mode="json"),
                    "right": b.model_dump(mode="json"),
                },
                MergeDecision,
            )
            if decision.merge:
                merged = Activity(
                    start=a.start,
                    end=b.end,
                    title_md=decision.title_md,
                    description_md=decision.description_md,
                    evidence_ids=list(dict.fromkeys(a.evidence_ids + b.evidence_ids)),
                )
                left, right = [*left[:-1], merged], right[1:]
        result = Segmentation(
            activities=left + right,
            empty_reason="; ".join(p.empty_reason for p in parts if p.empty_reason),
        )
        validate_segments(result, interval, evidence)
        return result


def generate(ask: Ask, data: dict[str, Any], *, depth: int = 0) -> Generated:
    try:
        return ask("generation", data, Generated)
    except InputBudgetExceeded:
        evidence = data.get("evidence", [])
        if len(evidence) < 2 or depth >= 16:
            raise
        middle = len(evidence) // 2
        partials = []
        for part in (evidence[:middle], evidence[middle:]):
            partials.append(generate(ask, {**data, "evidence": part}, depth=depth + 1))
        result: Generated = ask(
            "consolidation",
            {
                "activity": data["activity"],
                "rule": data["rule"],
                "partial_results": [p.model_dump(mode="json") for p in partials],
            },
            Generated,
        )
        if result.unchanged:
            raise ValueError("consolidation_cannot_be_unchanged") from None
        if not set(result.evidence_ids) <= {id for p in partials for id in p.evidence_ids}:
            raise ValueError("consolidation_invented_evidence") from None
        return result
