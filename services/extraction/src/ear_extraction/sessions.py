"""Opt-in, transcript-only course session compiler for explicitly selected windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

from ear_contracts import RawRecord, format_utc
from pydantic import Field

from ear_extraction.activity_duration import meets_activity_floor
from ear_extraction.domain import Activity, Interval, Model, Segmentation
from ear_extraction.long_input import Ask
from ear_extraction.model_io import SESSION_SYSTEM as SESSION_SYSTEM
from ear_extraction.segmentation import validate_anonymous_activity

_MAX_WINDOWS = 24
_MAX_WINDOW = timedelta(minutes=15)
_MAX_SEAM_GAP = timedelta(minutes=2)
_EDGE_ROWS = 3
_QUOTE_CHARS = 240
_POINT_TICK = timedelta(microseconds=1)
_UNCERTAIN = "与相邻课堂片段是否属于同一节课尚不确定；仅描述有转写支持的片段。"
_UNCERTAIN_START = "开课边界尚不确定；不将片段开头视为已确认开课。"
_UNCERTAIN_END = "下课边界尚不确定；不将片段结尾视为已确认下课。"
_MISSING = "相邻选定窗口没有可独立划分的转写，不能据此判断课程开始、结束或延续。"
_UNCLASSIFIED = "相邻转写尚未分类，不能据此判断课程开始、结束或延续。"


_MarkerStatus = Literal["absent", "reliable", "discarded"]


class _Quote(Model):
    i: int = Field(strict=True, ge=1)
    quote: str = Field(min_length=1, max_length=_QUOTE_CHARS)


class _FragmentSpec(Model):
    kind: Literal["course", "other"]
    first: int = Field(strict=True, ge=1)
    last: int = Field(strict=True, ge=1)
    title_md: str = Field(min_length=1, max_length=120)
    description_md: str = Field(max_length=500)
    course_start: _Quote | None
    course_end: _Quote | None


class _WindowResult(Model):
    fragments: list[_FragmentSpec]


class _Pair(Model):
    left: _Quote
    right: _Quote


class _Boundary(Model):
    verdict: Literal["same_class", "different_class", "uncertain"]
    pair: _Pair | None


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Unclassified counts unique nonblank rows, not missing/blank windows."""

    segmentation: Segmentation
    merged_boundaries: int
    uncertain_boundaries: int
    course_activities: int
    discarded_boundary_hints: int = 0
    unclassified_rows: int = 0
    below_min_duration: tuple[Activity, ...] = ()


@dataclass(frozen=True, slots=True)
class _Row:
    index: int
    raw: RawRecord
    start: datetime
    end: datetime
    text: str

    @property
    def upper(self) -> datetime:
        # Represent an observed point in a half-open, nonempty interval. This does not
        # extend a singleton to the selected window or imply a class length.
        return self.end if self.end > self.start else self.start + _POINT_TICK

    def wire(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "s": format_utc(self.start),
            "e": format_utc(self.end),
            "text": self.text,
            "crosses_window_end": self.raw.event_end > self.end,
        }


@dataclass(frozen=True, slots=True)
class _Fragment:
    course: bool
    activity: Activity | None  # None is unclassified, not a model-proposed "other".
    rows: list[_Row]
    ordinal: int
    course_start: _MarkerStatus = "absent"
    course_end: _MarkerStatus = "absent"


@dataclass(frozen=True, slots=True)
class _Excerpt:
    row: _Row
    text: str

    def wire(self) -> dict[str, Any]:
        return {"i": self.row.index, "text": self.text, "clipped": self.text != self.row.text}


def validate_session_windows(windows: tuple[Interval, ...]) -> None:
    if not 1 <= len(windows) <= _MAX_WINDOWS:
        raise ValueError("session_window_count_out_of_range")
    for i, window in enumerate(windows):
        if not timedelta(0) < window.end - window.start <= _MAX_WINDOW:
            raise ValueError("session_window_duration_out_of_range")
        if i and windows[i - 1].end > window.start:
            raise ValueError("session_windows_overlap_or_unsorted")


def pack_session_windows(components: tuple[Interval, ...]) -> tuple[Interval, ...]:
    """Pack verified capture components without using a gap as read coverage."""
    if any(a.end >= b.start for a, b in pairwise(components)):
        raise ValueError("session_components_not_strictly_separated")
    windows: list[Interval] = []
    for component in components:
        start = component.start
        while start < component.end:
            if len(windows) == _MAX_WINDOWS:
                raise ValueError("session_window_count_out_of_range")
            end = start + min(_MAX_WINDOW, component.end - start)
            windows.append(Interval(start=start, end=end))
            start = end
    result = tuple(windows)
    validate_session_windows(result)
    return result


def _window_rows(
    windows: tuple[Interval, ...], raw: list[RawRecord]
) -> tuple[list[list[_Row]], list[int]]:
    # A row may straddle a touching seam, but its text cannot be time-sliced. Send it
    # exactly once, in its starting window, and only if entirely in the selected union.
    covered_until = [w.end for w in windows]
    for i in range(len(windows) - 2, -1, -1):
        if windows[i].end == windows[i + 1].start:
            covered_until[i] = covered_until[i + 1]
    seen: dict[str, RawRecord] = {}
    rows: list[list[_Row]] = [[] for _ in windows]
    blanks = [0] * len(windows)
    for record in sorted(raw, key=lambda r: (r.event_start, r.event_end, r.id)):
        if record.id in seen:
            if seen[record.id] != record:
                raise ValueError("session_conflicting_transcript_record")
            continue
        seen[record.id] = record
        text = record.payload.get("text")
        if record.data_type != "transcript_segment" or not isinstance(text, str):
            raise ValueError("session_transcript_invalid")
        owner = next(
            (i for i, w in enumerate(windows) if w.start <= record.event_start < w.end), -1
        )
        if owner < 0 or not record.event_start <= record.event_end <= covered_until[owner]:
            raise ValueError("session_transcript_outside_selected_windows")
        if not text.strip():
            blanks[owner] += 1
            continue
        group = rows[owner]
        group.append(
            _Row(
                len(group) + 1,
                record,
                record.event_start,
                min(record.event_end, windows[owner].end),
                text,
            )
        )
    return rows, blanks


def _quote_matches(citation: _Quote, excerpt: str) -> bool:
    return any(c.isalnum() for c in citation.quote) and citation.quote in excerpt


def _check_quote(citation: _Quote, excerpt: str) -> None:
    if not _quote_matches(citation, excerpt):
        raise ValueError("session_quote_not_in_excerpt")


def _check_edge(citation: _Quote | None, rows: list[_Row]) -> _MarkerStatus:
    if citation is None:
        return "absent"
    for row in rows:
        if row.index == citation.i:
            if row.raw.event_end == row.end and _quote_matches(citation, row.text):
                return "reliable"
            break
    return "discarded"


def _compile_window(
    window: Interval, result: _WindowResult, rows: list[_Row], ordinal: int
) -> list[_Fragment]:
    compiled: list[_Fragment] = []
    next_i = 1
    for spec in result.fragments:
        if not next_i <= spec.first <= spec.last <= len(rows):
            # Omissions are safe; reversed, duplicate, overlapping or unordered assignments aren't.
            raise ValueError("session_fragment_indices_not_partition")
        if next_i < spec.first:
            compiled.append(
                _Fragment(False, None, rows[next_i - 1 : spec.first - 1], ordinal + len(compiled))
            )
        part = rows[spec.first - 1 : spec.last]
        start, end = part[0].start, max(r.upper for r in part)
        if end > window.end:
            raise ValueError("session_fragment_out_of_range")
        if not spec.title_md.strip():
            raise ValueError("session_fragment_requires_title")
        if spec.kind == "other" and (spec.course_start or spec.course_end):
            raise ValueError("session_non_course_marker")
        activity = Activity(
            start=start,
            end=end,
            title_md=spec.title_md,
            description_md=spec.description_md,
            evidence_ids=[r.raw.id for r in part],
        )
        validate_anonymous_activity(activity)
        course_start = _check_edge(spec.course_start, part[:_EDGE_ROWS])
        course_end = _check_edge(spec.course_end, part[-_EDGE_ROWS:])
        if spec.course_start and spec.course_end and spec.course_start.i > spec.course_end.i:
            course_start = course_end = "discarded"
        compiled.append(
            _Fragment(
                spec.kind == "course",
                activity,
                part,
                ordinal + len(compiled),
                course_start,
                course_end,
            )
        )
        next_i = spec.last + 1
    if next_i <= len(rows):
        compiled.append(_Fragment(False, None, rows[next_i - 1 :], ordinal + len(compiled)))
    return compiled


def _overlapping_fragments(fragments: list[_Fragment]) -> set[int]:
    # Classifying either side of a cut through overlapping raw times would be a guess.
    # Discard the whole conflicting component: trimming a candidate would reuse prose
    # that may not be supported by its remaining evidence. Include omitted runs and use
    # original times so clipping at a window edge cannot hide a conflict.
    conflicts: set[int] = set()
    end: datetime | None = None
    for fragment in fragments:
        upper = max(max(r.upper, r.raw.event_end) for r in fragment.rows)
        if end is not None and fragment.rows[0].start < end:
            conflicts.update((fragment.ordinal - 1, fragment.ordinal))
            end = max(end, upper)
        else:
            end = upper
    return conflicts


def _note_unclassified(notes: dict[int, set[str]], left: _Fragment, right: _Fragment) -> None:
    if left.course and right.activity is None:
        notes.setdefault(left.ordinal, set()).add(_UNCLASSIFIED)
    if right.course and left.activity is None:
        notes.setdefault(right.ordinal, set()).add(_UNCLASSIFIED)


def _in_order(left: _Row, right: _Row) -> bool:
    # Compare *original*, not clipped, times. One shared or overlapping observation
    # cannot independently substantiate both sides of a join.
    separation = right.raw.event_start - max(left.raw.event_end, left.upper)
    return left.raw.id != right.raw.id and timedelta(0) <= separation <= _MAX_SEAM_GAP


def _check_boundary(ask: Ask, left: _Fragment, right: _Fragment) -> str:
    suffix = [_Excerpt(r, r.text[-_QUOTE_CHARS:]) for r in left.rows[-_EDGE_ROWS:]]
    prefix = [_Excerpt(r, r.text[:_QUOTE_CHARS]) for r in right.rows[:_EDGE_ROWS]]
    pairs = [(a.row.index, b.row.index) for a in suffix for b in prefix if _in_order(a.row, b.row)]
    if not pairs:
        return "uncertain"
    decision: _Boundary = ask(
        "course_boundary",
        {
            "left": [e.wire() for e in suffix],
            "right": [e.wire() for e in prefix],
            "left_omitted_rows": len(left.rows) - len(suffix),
            "right_omitted_rows": len(right.rows) - len(prefix),
            "eligible_pairs": [{"left": a, "right": b} for a, b in pairs],
        },
        _Boundary,
    )
    if decision.verdict == "uncertain":
        if decision.pair is not None:
            raise ValueError("session_uncertain_with_pair")
        return decision.verdict
    if decision.pair is None:
        raise ValueError("session_boundary_requires_pair")
    a = next((e for e in suffix if e.row.index == decision.pair.left.i), None)
    b = next((e for e in prefix if e.row.index == decision.pair.right.i), None)
    if a is None or b is None or not _in_order(a.row, b.row):
        raise ValueError("session_boundary_pair_out_of_range_or_overlapping")
    _check_quote(decision.pair.left, a.text)
    _check_quote(decision.pair.right, b.text)
    return decision.verdict


def segment_sessions(
    ask: Ask,
    windows: tuple[Interval, ...],
    raw: list[RawRecord],
    *,
    minimum: timedelta | None = None,
) -> SessionResult:
    """Cover every nonblank row, then resolve adjacent eligible course seams.

    Uncovered rows and ambiguous time cuts remain unclassified, not activities.
    Invalid required output and call failures still abort, without retries. Activity
    times and raw links come from the supplied source records.
    """
    validate_session_windows(windows)
    rows, blanks = _window_rows(windows, raw)
    parts: list[list[_Fragment]] = []
    fragments: list[_Fragment] = []
    missing: list[int] = []
    for i, window in enumerate(windows):
        # Full eligible transcript text or explicit budget failure from ask; no sampling.
        output: _WindowResult = ask(
            "session_fragments",
            {
                "interval": window.model_dump(mode="json"),
                "evidence": [r.wire() for r in rows[i]],
                "blank_rows": blanks[i],
            },
            _WindowResult,
        )
        part = _compile_window(window, output, rows[i], len(fragments))
        parts.append(part)
        fragments.extend(part)
        if not rows[i]:
            missing.append(i + 1)

    conflicts = _overlapping_fragments(fragments)
    discarded = sum(
        (f.course_start == "discarded") + (f.course_end == "discarded") for f in fragments
    )
    notes: dict[int, set[str]] = {}
    for part in parts:
        for j, fragment in enumerate(part):
            if fragment.ordinal in conflicts and fragment.activity is not None:
                part[j] = _Fragment(False, None, fragment.rows, fragment.ordinal)
            else:
                if fragment.course_start == "discarded":
                    notes.setdefault(fragment.ordinal, set()).add(_UNCERTAIN_START)
                if fragment.course_end == "discarded":
                    notes.setdefault(fragment.ordinal, set()).add(_UNCERTAIN_END)
        for left, right in pairwise(part):
            _note_unclassified(notes, left, right)
            if left.course and right.course:
                markers = (left.course_end, right.course_start)
                if markers == ("absent", "absent"):
                    raise ValueError("session_course_split_requires_explicit_marker")
                if "reliable" not in markers:
                    for fragment in (left, right):
                        notes.setdefault(fragment.ordinal, set()).add(_UNCERTAIN)

    joins: set[int] = set()
    uncertain = 0
    for i, (lpart, rpart) in enumerate(pairwise(parts)):
        left, right = (lpart[-1] if lpart else None), (rpart[0] if rpart else None)
        if left is None or right is None:
            # Missing text is not evidence that a class ended. Do not jump these windows.
            if left and left.course and left.course_end != "reliable":
                notes.setdefault(left.ordinal, set()).add(_MISSING)
            if right and right.course and right.course_start != "reliable":
                notes.setdefault(right.ordinal, set()).add(_MISSING)
            continue
        _note_unclassified(notes, left, right)
        if not left.course or not right.course or left.activity is None or right.activity is None:
            continue
        seam_markers = (left.course_end, right.course_start)
        if "discarded" in seam_markers:
            verdict = "uncertain"
        elif "reliable" in seam_markers:
            continue
        elif (
            windows[i + 1].start - windows[i].end > _MAX_SEAM_GAP
            or right.activity.start - left.activity.end > _MAX_SEAM_GAP
        ):
            verdict = "uncertain"
        else:
            verdict = _check_boundary(ask, left, right)
        if verdict == "same_class":
            joins.add(right.ordinal)
        elif verdict == "uncertain":
            uncertain += 1
            for fragment in (left, right):
                notes.setdefault(fragment.ordinal, set()).add(_UNCERTAIN)

    groups: list[list[tuple[_Fragment, Activity]]] = []
    unclassified = 0
    for part in parts:
        for fragment in part:
            if fragment.activity is None:
                unclassified += len(fragment.rows)
                continue
            item = (fragment, fragment.activity)
            if fragment.ordinal in joins:
                groups[-1].append(item)
            else:
                groups.append([item])
    activities: list[Activity] = []
    short: list[Activity] = []
    courses = 0
    by_id = {r.id: r for r in raw}
    for group in groups:
        first, last = group[0][1], group[-1][1]
        descriptions = dict.fromkeys(
            a.description_md.strip() for _, a in group if a.description_md.strip()
        )
        annotations = sorted({n for f, _ in group for n in notes.get(f.ordinal, ())})
        activity = Activity(
            start=first.start,
            end=last.end,
            title_md=first.title_md,
            description_md="\n\n".join([*descriptions, *annotations]),
            evidence_ids=list(dict.fromkeys(ref for _, a in group for ref in a.evidence_ids)),
        )
        if minimum is not None and not meets_activity_floor(activity, by_id, minimum):
            short.append(activity)
            unclassified += len(activity.evidence_ids)
        else:
            activities.append(activity)
            courses += group[0][0].course
    empty_reason = ""
    if missing:
        empty_reason = "session_windows_without_transcript:" + ",".join(map(str, missing))
    elif not activities:
        empty_reason = "no_supported_activity" if short else "session_all_rows_unclassified"
    return SessionResult(
        Segmentation(activities=activities, empty_reason=empty_reason),
        len(joins),
        uncertain,
        courses,
        discarded,
        unclassified,
        tuple(short),
    )
