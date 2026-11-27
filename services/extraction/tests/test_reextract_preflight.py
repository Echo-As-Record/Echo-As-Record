from datetime import UTC, datetime, timedelta

import pytest
from ear_extraction.reextract_preflight import (
    Capture,
    ExactInterval,
    OldTarget,
    PreflightInput,
    TargetType,
    plan_reextract_preflight,
)
from pydantic import ValidationError


def tick(n):
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=n)


def span(start, end):
    return ExactInterval(start=tick(start), end=tick(end))


def ident(n):
    return f"00000000-0000-7000-8000-{n:012x}"


def capture(n, start, end):
    return Capture(id=ident(n), start=tick(start), end=tick(end))


def activity(n, start, end, **overrides):
    values = {"protection": "automatic", "action_safety": "clear", "version": 1} | overrides
    return OldTarget(data_type="activity", id=ident(n), start=tick(start), end=tick(end), **values)


def child(n, owner, kind: TargetType = "artifact", **overrides):
    values = {
        "start": owner.start,
        "end": owner.end,
        "protection": "automatic",
        "activity_id": owner.id,
        "version": 1,
    } | overrides
    return OldTarget(data_type=kind, id=ident(n), **values)


def plan(captures=(), holds=(), targets=()):
    return plan_reextract_preflight(
        PreflightInput(scope=span(0, 40), captures=captures, holds=holds, targets=targets)
    )


def ranges(items):
    return [(x.interval.start, x.interval.end) for x in items]


def pairs(*points):
    return [(tick(a), tick(b)) for a, b in points]


def test_half_open_edges_and_full_capture_fencing():
    clips = (capture(1, 2, 10), capture(2, 10, 20), capture(3, 20, 30), capture(4, 30, 40))
    edge_holds = (span(0, 2), span(13, 14), span(14, 30))
    left = activity(10, 2, 10)
    before_original_hold = activity(11, 10, 12)
    at_hold_end = activity(12, 30, 40)
    result = plan(clips, edge_holds, (left, before_original_hold, at_hold_end))
    assert result.status == "preview_only"
    assert result.requested_holds == edge_holds
    assert result.held_captures == clips[1:3]
    assert [c.captures for c in result.components] == [(clips[0],), (clips[3],)]
    assert ranges(result.components) == pairs((2, 10), (30, 40))
    assert ranges(result.coverage_issues) == pairs((0, 2), (10, 30))
    assert [i.reason for i in result.coverage_issues] == ["hold", "hold"]
    assert ranges(result.replacement_scopes) == ranges(result.components)
    assert [r.target for r in result.retained_targets] == [before_original_hold]
    assert "held_time" in result.retained_targets[0].reasons


def test_touching_verified_captures_share_review_component_not_activity():
    clips = (capture(1, 3, 10), capture(2, 10, 20), capture(3, 21, 30))
    result = plan(clips, (span(32, 34), span(33, 35)))
    assert [c.captures for c in result.components] == [clips[:2], clips[2:]]
    assert ranges(result.components) == pairs((3, 20), (21, 30))
    assert ranges(result.coverage_issues) == pairs((0, 3), (20, 21), (30, 32), (32, 35), (35, 40))
    assert [i.reason for i in result.coverage_issues] == ["gap", "gap", "gap", "hold", "gap"]
    assert not result.replacement_scopes  # No old targets, not a create plan.


@pytest.mark.parametrize("has_held_capture", [False, True])
def test_old_version_spanning_independent_components_is_never_split(has_held_capture):
    clips = (capture(1, 0, 10), capture(3, 20, 30), capture(4, 32, 40))
    if has_held_capture:
        clips = (clips[0], capture(2, 10, 20), *clips[1:])
    spanning = activity(11, 8, 25)
    artifact = child(12, spanning)
    within_same_component = activity(10, 0, 4)
    independent = activity(13, 32, 38)
    result = plan(
        clips,
        (span(14, 16),) if has_held_capture else (),
        (within_same_component, spanning, artifact, independent),
    )
    assert ranges(result.components) == pairs((0, 10), (20, 30), (32, 40))
    assert ranges(result.replacement_scopes) == pairs((32, 40))
    reasons = {r.target.id: r.reasons for r in result.retained_targets}
    assert reasons[within_same_component.id] == ("component_blocked",)
    assert reasons[spanning.id] == reasons[artifact.id]
    assert "spans_components" in reasons[spanning.id]
    assert ("held_time" in reasons[spanning.id]) == has_held_capture


def test_old_target_overlaps_only_one_component_but_extends_into_gap_or_outside_scope():
    clips = (capture(1, 0, 10), capture(2, 20, 40))
    crossing_scope = activity(10, -1, 2)
    crossing_gap = activity(11, 18, 21)
    result = plan(clips, targets=(crossing_scope, crossing_gap))
    assert not result.replacement_scopes
    assert [r.reasons for r in result.retained_targets] == [("outside_component",)] * 2


def test_pins_include_versions_of_all_linked_targets_and_change_with_new_snapshot():
    clips = (capture(1, 2, 10), capture(2, 10, 20))
    owner = activity(10, 5, 15)
    artifact = child(11, owner)
    selection = child(12, owner, "rule_selection", version=3)
    result = plan(clips, targets=(owner, artifact, selection))
    assert not result.retained_targets
    assert ranges(result.replacement_scopes) == pairs((2, 20))
    assert [(p.data_type, p.id, p.expected_version) for p in result.replacement_scopes[0].pins] == [
        ("activity", owner.id, 1),
        ("artifact", artifact.id, 1),
        ("rule_selection", selection.id, 3),
    ]
    latest = plan(clips, targets=(owner, artifact, child(12, owner, "rule_selection", version=4)))
    assert latest.replacement_scopes != result.replacement_scopes
    assert plan(clips, targets=(owner, selection)).replacement_scopes != result.replacement_scopes
    assert all(record.id not in repr(result) for record in (*clips, owner, artifact, selection))


@pytest.mark.parametrize(
    ("owner_flags", "artifact_flags", "reason"),
    [
        ({}, {"protection": "manual_or_edited"}, "manual_or_edited"),
        ({"version": 2}, {}, "manual_or_edited"),
        ({}, {"version": 2}, "manual_or_edited"),
        ({"action_safety": "protected_action"}, {}, "protected_action"),
        ({"action_safety": "unknown"}, {}, "protection_unknown"),
        ({}, {"protection": "unknown"}, "protection_unknown"),
    ],
)
def test_protection_retains_the_whole_family(owner_flags, artifact_flags, reason):
    owner = activity(10, 5, 8, **owner_flags)
    artifact = child(11, owner, **artifact_flags)
    selection = child(12, owner, "rule_selection")
    result = plan((capture(1, 0, 10),), targets=(owner, artifact, selection))
    assert not result.replacement_scopes
    assert [x.target for x in result.retained_targets] == [owner, artifact, selection]
    assert all(x.reasons == (reason,) for x in result.retained_targets)


def test_mismatched_child_geometry_cannot_pin_any_family_member():
    owner = activity(10, 5, 8)
    artifact = child(11, owner, end=tick(22))
    result = plan((capture(1, 0, 10), capture(2, 20, 30)), targets=(owner, artifact))
    assert not result.replacement_scopes
    assert all("linked_interval_mismatch" in x.reasons for x in result.retained_targets)
    assert all("spans_components" in x.reasons for x in result.retained_targets)


@pytest.mark.parametrize("kind", ["orphan", "missing_link", "duplicate_id", "second_selection"])
def test_ambiguous_links_and_duplicate_snapshot_versions_fail_closed(kind):
    owner = activity(10, 5, 8)
    selection = child(11, owner, "rule_selection")
    targets, reason = {
        "orphan": ((selection,), "ambiguous_activity_link"),
        "missing_link": ((owner, child(11, owner, activity_id=None)), "ambiguous_activity_link"),
        "duplicate_id": (
            (owner, selection, child(11, owner, "rule_selection", version=2)),
            "duplicate_object_id",
        ),
        "second_selection": (
            (owner, selection, child(12, owner, "rule_selection")),
            "ambiguous_rule_selection",
        ),
    }[kind]
    with pytest.raises(ValueError, match=reason):
        plan((capture(1, 0, 10),), targets=targets)


@pytest.mark.parametrize(
    ("clips", "holds", "targets", "reason"),
    [
        ((capture(1, -1, 5),), (), (), "outside_scope"),
        ((), (span(38, 41),), (), "outside_scope"),
        ((capture(1, 1, 6), capture(2, 5, 9)), (), (), "unsorted_or_overlapping"),
        ((capture(1, 6, 9), capture(2, 1, 5)), (), (), "unsorted_or_overlapping"),
        ((capture(1, 0, 6), capture(1, 6, 9)), (), (), "duplicate_object_id"),
        ((), (span(6, 9), span(1, 2)), (), "holds_unsorted_or_duplicate"),
        ((), (), (activity(12, 1, 2), activity(11, 1, 2)), "targets_unsorted"),
    ],
)
def test_metadata_must_be_exact_sorted_and_unique(clips, holds, targets, reason):
    with pytest.raises(ValueError, match=reason):
        plan(clips, holds, targets)


@pytest.mark.parametrize("value", [True, 0, -1, "1"])
def test_version_must_be_a_strict_positive_integer(value):
    with pytest.raises(ValidationError, match="version"):
        activity(10, 5, 8, version=value)


@pytest.mark.parametrize("value", [1767225600, "2026-01-01T00:00:00.1234567Z", "2026-01-01T00:00Z"])
def test_timestamp_is_not_silently_coerced_or_truncated(value):
    with pytest.raises(ValidationError, match="expected_utc_timestamp"):
        Capture.model_validate({"id": ident(1), "start": value, "end": tick(6)})


def test_bounded_inputs_and_empty_capture_coverage():
    with pytest.raises(ValueError, match="scope_too_long"):
        plan_reextract_preflight(
            PreflightInput(scope=span(0, 1921), captures=(), holds=(), targets=())
        )
    with pytest.raises(ValidationError, match="at most 512"):
        plan((capture(1, 0, 5),) * 513)
    result = plan(holds=(span(4, 6),))
    assert not result.components and not result.replacement_scopes
    assert ranges(result.coverage_issues) == pairs((0, 4), (4, 6), (6, 40))
