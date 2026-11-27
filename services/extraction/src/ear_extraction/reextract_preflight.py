"""Partition private capture metadata for offline review, never for execution."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Literal

from ear_contracts import Id
from pydantic import ConfigDict, Field, field_validator

from ear_extraction.domain import Interval, Model

_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
TargetType = Literal["activity", "artifact", "rule_selection"]
RetainReason = Literal[
    "held_time",
    "spans_components",
    "outside_component",
    "linked_interval_mismatch",
    "manual_or_edited",
    "protected_action",
    "protection_unknown",
    "component_blocked",
]


class ExactInterval(Interval):
    model_config = ConfigDict(hide_input_in_errors=True)

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_exact_time(cls, value: object) -> object:
        if isinstance(value, datetime) or (isinstance(value, str) and _UTC.fullmatch(value)):
            return value
        raise ValueError("expected_utc_timestamp_with_at_most_microsecond_precision")


class Capture(ExactInterval):
    """One complete, caller-verified capture; use its Storage audio_ref ID."""

    id: Id = Field(repr=False)


class OldTarget(ExactInterval):
    """Current metadata only; no body, evidence links, or write operation."""

    data_type: TargetType
    id: Id = Field(repr=False)
    version: int = Field(strict=True, ge=1)
    activity_id: Id | None = Field(default=None, repr=False)
    protection: Literal["automatic", "manual_or_edited", "unknown"]
    # Required on each activity, including the safety of actions on superseded artifacts.
    action_safety: Literal["clear", "protected_action", "unknown"] | None = None


class PreflightInput(Model):
    model_config = ConfigDict(hide_input_in_errors=True)

    scope: ExactInterval
    captures: tuple[Capture, ...] = Field(max_length=512)
    holds: tuple[ExactInterval, ...] = Field(max_length=512)
    targets: tuple[OldTarget, ...] = Field(max_length=1000)


@dataclass(frozen=True, slots=True)
class ReviewComponent:
    interval: Interval
    captures: tuple[Capture, ...]


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    interval: Interval
    reason: Literal["gap", "hold"]


@dataclass(frozen=True, slots=True)
class VersionPin:
    data_type: TargetType
    id: str = field(repr=False)
    expected_version: int


@dataclass(frozen=True, slots=True)
class ReplacementScope:
    interval: Interval
    pins: tuple[VersionPin, ...]


@dataclass(frozen=True, slots=True)
class RetainedTarget:
    target: OldTarget
    reasons: tuple[RetainReason, ...]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    scope: ExactInterval
    requested_holds: tuple[ExactInterval, ...]
    held_captures: tuple[Capture, ...]
    components: tuple[ReviewComponent, ...]
    coverage_issues: tuple[CoverageIssue, ...]
    replacement_scopes: tuple[ReplacementScope, ...]
    retained_targets: tuple[RetainedTarget, ...]
    status: Literal["preview_only"] = field(default="preview_only", init=False)


def _key(interval: Interval) -> tuple[datetime, datetime]:
    return interval.start, interval.end


def _span_key(interval: Capture | OldTarget) -> tuple[datetime, datetime, str]:
    return (*_key(interval), interval.id)


def _overlap(a: Interval, b: Interval) -> bool:
    return a.start < b.end and b.start < a.end


def _union(intervals: Sequence[Interval]) -> list[Interval]:
    spans: list[Interval] = []
    for span in sorted(intervals, key=_key):
        if spans and spans[-1].end >= span.start:
            prior = spans.pop()
            span = Interval(start=prior.start, end=max(prior.end, span.end))
        spans.append(Interval(start=span.start, end=span.end))
    return spans


def _coverage(
    data: PreflightInput,
) -> tuple[list[ReviewComponent], list[Capture], list[CoverageIssue]]:
    if any(
        not data.scope.start <= span.start < span.end <= data.scope.end
        for span in (*data.captures, *data.holds)
    ):
        raise ValueError("preflight_capture_or_hold_outside_scope")
    if any(a.end > b.start for a, b in pairwise(data.captures)):
        raise ValueError("preflight_captures_unsorted_or_overlapping")
    if any(_key(a) >= _key(b) for a, b in pairwise(data.holds)):
        raise ValueError("preflight_holds_unsorted_or_duplicate")

    held: list[Capture] = []
    groups: list[list[Capture]] = []
    for capture in data.captures:
        if any(_overlap(capture, hold) for hold in data.holds):
            held.append(capture)
        elif groups and groups[-1][-1].end == capture.start:
            groups[-1].append(capture)
        else:
            groups.append([capture])
    components = [
        ReviewComponent(Interval(start=g[0].start, end=g[-1].end), tuple(g)) for g in groups
    ]
    effective = [CoverageIssue(span, "hold") for span in _union([*data.holds, *held])]
    issues: list[CoverageIssue] = []
    cursor = data.scope.start
    for interval, hold in sorted(
        [(c.interval, None) for c in components] + [(h.interval, h) for h in effective],
        key=lambda item: _key(item[0]),
    ):
        if cursor < interval.start:
            issues.append(CoverageIssue(Interval(start=cursor, end=interval.start), "gap"))
        if hold is not None:
            issues.append(hold)
        cursor = interval.end
    if cursor < data.scope.end:
        issues.append(CoverageIssue(Interval(start=cursor, end=data.scope.end), "gap"))
    return components, held, issues


def has_covered_endpoints(span: Interval, components: Sequence[Interval]) -> bool:
    """Check half-open bounds independently; this does not assert interior coverage."""
    return any(c.start <= span.start < c.end for c in components) and any(
        c.start < span.end <= c.end for c in components
    )


def _families(data: PreflightInput) -> dict[str, list[OldTarget]]:
    ids = {c.id for c in data.captures} | {t.id for t in data.targets}
    if len(ids) != len(data.captures) + len(data.targets):
        raise ValueError("preflight_duplicate_object_id")
    if any(_span_key(a) > _span_key(b) for a, b in pairwise(data.targets)):
        raise ValueError("preflight_targets_unsorted")
    families = {t.id: [t] for t in data.targets if t.data_type == "activity"}
    selections: set[str] = set()
    for target in data.targets:
        parent = target.activity_id
        if target.data_type == "activity":
            if parent is not None or target.action_safety is None:
                raise ValueError("preflight_invalid_activity_projection")
        elif parent is None or parent not in families or target.action_safety is not None:
            raise ValueError("preflight_ambiguous_activity_link")
        else:
            if target.data_type == "rule_selection":
                if parent in selections:
                    raise ValueError("preflight_ambiguous_rule_selection")
                selections.add(parent)
            families[parent].append(target)
    return families


def plan_reextract_preflight(data: PreflightInput) -> PreflightResult:
    """Build a private review result from a complete, caller-attested snapshot projection."""
    if data.scope.end - data.scope.start > timedelta(hours=32):
        raise ValueError("preflight_scope_too_long")
    components, held, issues = _coverage(data)
    starts = [c.interval.start for c in components]
    ends = [c.interval.end for c in components]
    held_time = [i.interval for i in issues if i.reason == "hold"]
    provisional: dict[int, list[OldTarget]] = defaultdict(list)
    blocked_components: set[int] = set()
    retained: list[RetainedTarget] = []
    for activity, *children in _families(data).values():
        family = (activity, *children)
        reasons: set[RetainReason] = set()
        touched: set[int] = set()
        for target in family:
            first = bisect_right(ends, target.start)
            stop = bisect_left(starts, target.end)
            touched.update(range(first, stop))
            if stop - first > 1:
                reasons.add("spans_components")
            elif first == stop or target.start < starts[first] or target.end > ends[first]:
                reasons.add("outside_component")
            if any(_overlap(target, h) for h in held_time):
                reasons.add("held_time")
            if _key(target) != _key(activity):
                reasons.add("linked_interval_mismatch")
            if target.protection == "manual_or_edited" or (
                target.data_type != "rule_selection" and target.version > 1
            ):
                reasons.add("manual_or_edited")
            if target.protection == "unknown" or target.action_safety == "unknown":
                reasons.add("protection_unknown")
            if target.action_safety == "protected_action":
                reasons.add("protected_action")
        if reasons:
            blocked_components.update(touched)
            retained.extend(RetainedTarget(t, tuple(sorted(reasons))) for t in family)
        else:
            (component,) = touched
            provisional[component].extend(family)

    scopes: list[ReplacementScope] = []
    for component, targets in sorted(provisional.items()):
        if component in blocked_components:
            retained.extend(RetainedTarget(t, ("component_blocked",)) for t in targets)
        else:
            pins = tuple(
                VersionPin(t.data_type, t.id, t.version) for t in sorted(targets, key=_span_key)
            )
            scopes.append(ReplacementScope(components[component].interval, pins))
    return PreflightResult(
        data.scope,
        data.holds,
        tuple(held),
        tuple(components),
        tuple(issues),
        tuple(scopes),
        tuple(sorted(retained, key=lambda r: _span_key(r.target))),
    )
