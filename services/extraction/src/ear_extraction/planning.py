"""Model-driven plans: compute outside transactions, commit through ① afterwards."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from itertools import pairwise
from typing import Any

from ear_contracts import SemanticRecord, VersionRef, new_id
from ear_storage_client import (
    AssertTypeRevision,
    Create,
    Reader,
    RegisterDependencies,
    Supersede,
    TimeRange,
    Update,
    WriteOp,
)

from ear_extraction.activity_duration import (
    DEFAULT_ACTIVITY_MIN_DURATION_MINUTES,
    duration_audit,
    meets_activity_floor,
)
from ear_extraction.artifacts import dependencies, transcript_dependencies, validate_generated
from ear_extraction.automatic_timeline import (
    MAX_RAW_READ_ROWS,
    AutomaticTimeline,
    ReviewWave,
    segment_automatic_timeline,
)
from ear_extraction.course_blocks import segment_course_blocks
from ear_extraction.course_reextract import (
    CourseDraftGuard,
    CourseReplacement,
    ScopedTranscriptGuard,
    compile_course_draft,
    course_draft_sha256,
    course_replacement_targets,
)
from ear_extraction.domain import (
    Activity,
    Blocked,
    Command,
    Generated,
    Interval,
    Rule,
    SessionCoverageBlocked,
)
from ear_extraction.long_input import generate, segment
from ear_extraction.persistence import range_of, records, semantic
from ear_extraction.reevaluation import ReevaluationPlan, terminate, unavailable
from ear_extraction.rules import eligible
from ear_extraction.segmentation import Inputs, inputs, raw_inputs, validate_anonymous_activity
from ear_extraction.selection import (
    create_selection,
    load_rule_candidates,
    validate_summary_rule_config,
)
from ear_extraction.sessions import segment_sessions, validate_session_windows
from ear_extraction.suggestions import create_suggestion, pending_replacements

Ask = Callable[[str, dict[str, Any], type], Any]


def preview_reextract(reader: Reader, interval: Interval) -> dict:
    activities = records(reader, "activity", interval)
    for a in activities:
        r = range_of(a)
        if r.start < interval.start or r.end > interval.end:
            raise Blocked("partial_activity_overlap_select_full_activity")
    ids = {a.id for a in activities}
    artifacts = [r for r in records(reader, "artifact") if r.payload.get("activity_id") in ids]
    modified = {
        r.payload["activity_id"] for r in artifacts if r.version > 1 or r.payload.get("manual")
    }
    suggestions = [
        r
        for r in records(reader, "action_suggestion")
        if r.payload.get("artifact_id") in {a.id for a in artifacts}
        and r.payload.get("state") == "pending"
    ]
    return {
        "versions": {r.id: r.version for r in [*activities, *artifacts, *suggestions]},
        "modified_activities": len(modified),
    }


def _reextract_ops(
    reader: Reader, command: Command, interval: Interval, *, course: CourseReplacement | None = None
) -> list[WriteOp]:
    if course is not None:
        if course.preview_versions != command.preview:
            raise Blocked("reextract_preview_changed")
        return [Supersede(kind, id, version) for (kind, id), version in course.versions.items()]
    ops: list[WriteOp] = []
    preview = preview_reextract(reader, interval)
    if preview["versions"] != command.preview:
        raise Blocked("reextract_preview_changed")
    assert command.preview is not None
    for id, version in command.preview.items():
        r = reader.get(id)
        assert isinstance(r, SemanticRecord)
        ops.append(Supersede(r.data_type, id, version))
    # Preserve the public preview's original version-set meaning (⑥ also
    # computes it). Selections are internal derived state, not user edits;
    # retire their current snapshot versions with CAS in this same batch.
    activity_ids = {r.id for r in records(reader, "activity", interval)}
    ops.extend(
        Supersede("rule_selection", r.id, r.version)
        for r in records(reader, "rule_selection")
        if r.payload.get("activity_id") in activity_ids
    )
    return ops


def extract_plan(
    reader: Reader,
    command: Command,
    run_id: str,
    ask: Ask,
    *,
    activity_min_duration_minutes: int = DEFAULT_ACTIVITY_MIN_DURATION_MINUTES,
    transcript_only: bool = False,
    transcript_summary_rule_id: str | None = None,
    release_snapshot: Callable[[], None] | None = None,
    session_windows: tuple[Interval, ...] = (),
    course_blocks: bool = False,
    course_draft_guard: CourseDraftGuard | None = None,
    scoped_transcript_guard: ScopedTranscriptGuard | None = None,
    automatic_timeline: AutomaticTimeline | None = None,
    timeline_max_calls: int | None = None,
    timeline_review_wave: ReviewWave | None = None,
    timeline_review_concurrency: int = 1,
) -> tuple[list[WriteOp], dict]:
    validate_summary_rule_config(transcript_only, transcript_summary_rule_id)
    assert command.interval is not None
    interval = command.interval
    minimum = timedelta(minutes=activity_min_duration_minutes)
    course_reextract = command.kind == "reextract" and (
        course_draft_guard is not None or automatic_timeline is not None
    )
    if course_draft_guard is not None and not (
        course_reextract and course_blocks and transcript_only
    ):
        raise Blocked("course_reextract_requires_course_blocks")
    if course_blocks and not session_windows:
        raise Blocked("course_blocks_require_session_windows")
    if session_windows:
        validate_session_windows(session_windows)
        if not transcript_only or command.kind not in {"extract", "reextract"}:
            raise Blocked("session_windows_require_transcript_extraction")
        if interval.start != session_windows[0].start or interval.end != session_windows[-1].end:
            raise Blocked("session_windows_must_match_command_range")
        if (
            command.kind == "reextract"
            and not course_reextract
            and any(a.end != b.start for a, b in pairwise(session_windows))
        ):
            # The reextract preview describes all existing activities in the envelope.
            raise Blocked("session_reextract_requires_contiguous_windows")
    if automatic_timeline is not None:
        if not (
            session_windows
            and scoped_transcript_guard is not None
            and course_blocks
            and transcript_summary_rule_id is not None
            and course_draft_guard is None
            and automatic_timeline.interval == interval
            and automatic_timeline.replacing == course_reextract
            and timeline_max_calls is not None
            and timeline_review_wave is not None
        ):
            raise Blocked("automatic_timeline_requires_guarded_exact_scope")
        old = automatic_timeline.live_targets(reader, session_windows)
    else:
        old = (
            course_replacement_targets(reader, interval, session_windows)
            if course_reextract
            else None
        )
    timeline = course_draft_guard.timeline if course_draft_guard is not None else None
    if timeline is not None:
        assert old is not None
        timeline.check_pins(old)
    ops = (
        _reextract_ops(reader, command, interval, course=old)
        if transcript_only and command.kind == "reextract"
        else []
    )
    if session_windows:
        if scoped_transcript_guard is not None:
            scoped_transcript_guard.raw_revision = reader.type_revision("transcript_segment")
        raw, counts = [], []
        for window in session_windows:
            part = raw_inputs(
                reader,
                window,
                transcript_only=True,
                allow_empty_transcript=True,
                max_transcript_records=(MAX_RAW_READ_ROWS - len(raw))
                if automatic_timeline
                else None,
            )
            raw.extend(part)
            counts.append(len(part))
        if scoped_transcript_guard is not None:
            scoped_transcript_guard.raw_counts = tuple(counts)
    else:
        raw = raw_inputs(reader, interval, transcript_only=transcript_only)
    if old is not None:
        old.check_evidence(raw)
    candidates = None
    if transcript_only:
        candidates = load_rule_candidates(reader, summary_rule_id=transcript_summary_rule_id)
        # Ordinary transcript reads are now materialized. A model call may outlast
        # the snapshot lease; the one-time course path takes no snapshot at all.
        if release_snapshot is not None:
            release_snapshot()
    session = None
    draft_spans = course_draft_guard.spans if course_draft_guard is not None else None
    detail = {}
    if automatic_timeline is not None:
        assert old is not None and timeline_max_calls is not None
        assert timeline_review_wave is not None
        session, detail = segment_automatic_timeline(
            automatic_timeline,
            session_windows,
            raw,
            timeline_review_wave,
            old,
            max_calls=timeline_max_calls,
            concurrency=timeline_review_concurrency,
        )
    elif timeline is not None:
        session = timeline.compile(session_windows, raw)
        assert old is not None
        old.require_family_successors(session.segmentation.activities)
    elif draft_spans is not None:
        session = compile_course_draft(draft_spans, session_windows, raw)
    elif course_blocks:
        session = segment_course_blocks(
            ask,
            session_windows,
            raw,
            require_complete_coverage=course_reextract,
            minimum=None if course_reextract else minimum,
        )
    elif session_windows:
        session = segment_sessions(ask, session_windows, raw, minimum=minimum)
    if session is not None:
        detail.update(duration_audit(list(session.below_min_duration)))
        result = session.segmentation
        if result.empty_reason or not result.activities:
            # Never persist partial coverage as success when a selected window is
            # missing, or when no activity can be grounded in the supplied rows.
            reason = (
                "session_windows_without_transcript"
                if result.empty_reason.startswith("session_windows_without_transcript:")
                else result.empty_reason or "session_all_rows_unclassified"
            )
            raise SessionCoverageBlocked(reason, session.unclassified_rows, detail=detail)
        if course_draft_guard is not None and timeline is None:
            if (
                session.unclassified_rows
                or session.uncertain_boundaries
                or session.discarded_boundary_hints
            ):
                raise SessionCoverageBlocked(
                    "course_reextract_incomplete_coverage", session.unclassified_rows
                )
            if course_draft_sha256(result.activities) != course_draft_guard.digest:
                raise Blocked("course_reextract_draft_changed")
        by_id = {r.id: r for r in raw}  # The session compiler already checked duplicates.
        activity_deps = transcript_dependencies(list(by_id.values()))
    else:
        result = segment(ask, interval, raw, transcript_only=transcript_only)
        by_id = {r.id: r for r in raw}
        activity_deps = transcript_dependencies(raw) if transcript_only else []
        kept: list[Activity] = []
        short: list[Activity] = []
        for activity in result.activities:
            validate_anonymous_activity(activity)
            (kept if meets_activity_floor(activity, by_id, minimum) else short).append(activity)
        detail.update(duration_audit(short))
        if short:
            result = result.model_copy(
                update={
                    "activities": kept,
                    "empty_reason": result.empty_reason if kept else "no_supported_activity",
                }
            )
    if command.kind == "reextract" and not course_reextract:
        if not transcript_only:
            ops = _reextract_ops(reader, command, interval)
        # Generic preview permits whole-family retirement, but here there is no
        # successor/citation guard. Keep all old objects if any proposed fragment
        # was withheld or if the replacement is empty; don't partially retire.
        if ops and (not result.activities or "below_min_duration_candidates" in detail):
            raise SessionCoverageBlocked(
                "reextract_incomplete_activity_replacement",
                detail.get("below_min_duration_rows", 0),
                detail=detail,
            )
    for activity in result.activities:
        # Keep segmentation independent from attribution and only validate its raw references.
        validate_anonymous_activity(activity)
        aid = new_id()
        ops.append(
            Create(
                "activity",
                aid,
                activity.model_dump(mode="json"),
                event_start=activity.start,
                event_end=activity.end,
            )
        )
        if transcript_only:
            time = TimeRange(activity.start, activity.end)
            current = Inputs(
                [by_id[id] for id in activity.evidence_ids]
                if session is not None
                else [r for r in raw if time.overlaps(r.event_start, r.event_end)],
                [],
                [],
                transcript_only=True,
            )
            # Boundary placement and any merge used the entire segmentation input,
            # not just the records cited in this activity or consumed by its rules.
            deps = activity_deps
        else:
            current = inputs(reader, activity)
            deps = []
        ops.append(RegisterDependencies("activity", aid, deps))
        rules = candidates if candidates is not None else load_rule_candidates(reader)
        ops.extend(create_selection(rules, aid, activity, current, run_id, ask))
    # In this profile keep only outcome metadata, not free-form audit text that
    # could contain the verbatim transcript. Product Markdown is still persisted.
    if session is not None:
        return ops, {
            **detail,
            "activities": len(result.activities),
            "course_activities": session.course_activities,
            "merged_boundaries": session.merged_boundaries,
            "uncertain_boundaries": session.uncertain_boundaries,
            "discarded_boundary_hints": session.discarded_boundary_hints,
            "unclassified_rows": session.unclassified_rows,
            **(
                {"covered_rows": sum(len(a.evidence_ids) for a in result.activities)}
                if course_reextract or automatic_timeline is not None
                else {}
            ),
        }
    if not transcript_only or (detail and not result.activities):
        detail["empty_reason"] = result.empty_reason
    return ops, {**detail, "activities": len(result.activities)}


def apply_plan(
    reader: Reader, command: Command, run_id: str, ask: Ask
) -> tuple[list[WriteOp], dict]:
    activity = semantic(reader, command.target_id, "activity")
    interval = range_of(activity)
    data = inputs(reader, interval)
    ops: list[WriteOp] = []
    if command.kind == "extract_once":
        assert command.rule is not None
        rule_model = command.rule.model_copy(update={"activity_id": activity.id})
        rule_id, version = new_id(), 1
        rule_payload = rule_model.model_dump(mode="json")
        ops.append(Create("rule", rule_id, rule_payload))
    else:
        rule = semantic(reader, command.rule_id, "rule")
        rule_id, version, rule_payload = rule.id, rule.version, rule.payload
        rule_model = Rule.model_validate(rule_payload)
        if not eligible(rule_model, activity.id):
            raise Blocked("rule_not_enabled_for_activity")
    result = generate(
        ask,
        {
            "activity": activity.model_dump(mode="json"),
            "rule": {"id": rule_id, "version": version, "payload": rule_payload},
            **data.public(),
        },
    )
    validate_generated(result, rule_model, data, rejudge=False)
    id = new_id()
    ops.extend(
        [
            Create(
                "artifact",
                id,
                {
                    "activity_id": activity.id,
                    "rule_id": rule_id,
                    "rule_version": version,
                    "body_md": result.body_md,
                    "evidence_ids": result.evidence_ids,
                    "action_reason": result.action_reason,
                    "run_id": run_id,
                    "manual": True,
                    "source": "manual",
                },
                event_start=interval.start,
                event_end=interval.end,
            ),
            RegisterDependencies(
                "artifact",
                id,
                dependencies(
                    activity, VersionRef(data_type="rule", id=rule_id, version=version), data
                ),
            ),
            *create_suggestion(id, 1, result, interval),
        ]
    )
    return ops, {"artifact_id": id}


def rejudge_plan(reader: Reader, command: Command, ask: Ask) -> ReevaluationPlan:
    artifact = reader.get(command.target_id) if command.target_id else None
    if not isinstance(artifact, SemanticRecord):
        return unavailable(None, "artifact_missing")
    if artifact.superseded:
        return unavailable(artifact, "artifact_terminated")
    guard = AssertTypeRevision("rule", reader.type_revision("rule"))
    rule = reader.get(artifact.payload["rule_id"])
    if not isinstance(rule, SemanticRecord) or rule.superseded:
        return terminate(
            artifact, "rule_unavailable", [guard, *pending_replacements(reader, {artifact.id})]
        )
    activity = reader.get(artifact.payload["activity_id"])
    if not isinstance(activity, SemanticRecord) or activity.superseded:
        return terminate(
            artifact, "activity_unavailable", [guard, *pending_replacements(reader, {artifact.id})]
        )
    data = inputs(reader, range_of(activity))
    result: Generated = ask(
        "reevaluation",
        {
            "activity": activity.model_dump(mode="json"),
            "old_artifact": artifact.payload,
            "rule": rule.model_dump(mode="json"),
            "change_description": command.description,
            **data.public(),
        },
        Generated,
    )
    validate_generated(result, Rule.model_validate(rule.payload), data, rejudge=True)
    rule_changed = rule.version != artifact.payload["rule_version"]
    if result.unchanged and rule_changed:
        # A new rule may change action policy while leaving Markdown identical.
        # Require a complete result rather than copying old pending actions.
        raise ValueError("changed_rule_requires_complete_result")
    # Keep old evidence/text byte-for-byte when unchanged. No reverse parsing of
    # legacy markers and no rewriting historical rows. Rule provenance is current.
    payload = {**artifact.payload, "rule_version": rule.version}
    body_changed = not result.unchanged and result.body_md != artifact.payload["body_md"]
    if body_changed or (rule_changed and not result.unchanged):
        payload.update(
            body_md=result.body_md,
            evidence_ids=result.evidence_ids,
            action_reason=result.action_reason,
        )
    changed = payload != artifact.payload
    ops: list[WriteOp] = [guard, Update("artifact", artifact.id, artifact.version, payload)]
    if changed:
        ops.extend(pending_replacements(reader, {artifact.id}))
        ops.extend(create_suggestion(artifact.id, artifact.version + 1, result, range_of(activity)))
    ops.append(RegisterDependencies("artifact", artifact.id, dependencies(activity, rule, data)))
    outcome = "changed" if changed else "unchanged"
    return ReevaluationPlan(
        ops,
        {"artifact_id": artifact.id, "outcome": outcome},
        artifact.version,
        artifact.version + int(changed),
        outcome,
    )
