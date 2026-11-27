"""Only synthetic text, an offline model double, and in-memory storage."""

from datetime import timedelta
from typing import Any

import pytest
from ear_contracts import NewRawRecord, RawRecord, new_id
from ear_extraction.demo import fixture_registry
from ear_extraction.domain import Blocked, Command, InputBudgetExceeded, Interval, Rule
from ear_extraction.planning import extract_plan
from ear_extraction.sessions import segment_sessions
from ear_llm_gateway.models import GatewayError
from ear_llm_gateway.output_json import OutputSchema
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, RegisterDependencies
from ear_storage_client.testing import make_raw, ts
from pydantic import BaseModel, ValidationError

BASE = ts("2026-11-05T09:00:00Z")


def at(seconds: float):
    return BASE + timedelta(seconds=seconds)


def window(start: float, end: float):
    return Interval(start=at(start), end=at(end))


def row(start: float, end: float, text: Any) -> RawRecord:
    source = make_raw(
        "transcript_segment",
        {"text": text, "audio": "UNSENT_AUDIO", "speaker_id": "UNSENT_SPEAKER"},
        start=at(start),
        end=at(end),
        device="UNSENT_DEVICE",
    )
    return RawRecord(**source.model_dump(), ingested_at=BASE)


def quote(i: int, text: str):
    return {"i": i, "quote": text}


def course(first: int, last: int, **kw: Any):
    return {
        "kind": "course",
        "first": first,
        "last": last,
        "title_md": "Math class",
        "description_md": "Observed remarks",
        "course_start": None,
        "course_end": None,
        **kw,
    }


def continuation(li: int, left: str, ri: int, right: str):
    return {"verdict": "same_class", "pair": {"left": quote(li, left), "right": quote(ri, right)}}


class ModelDouble:
    def __init__(self, *responses: tuple[str, dict[str, Any] | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, purpose: str, data: dict[str, Any], model: type[BaseModel]):
        self.calls.append((purpose, data))
        assert self.responses, f"Unexpected model call: {purpose}"
        expected, response = self.responses.pop(0)
        assert purpose == expected
        if isinstance(response, Exception):
            raise response
        result = model.model_validate(response)
        OutputSchema(model.model_json_schema()).validate(response)
        return result

    @property
    def purposes(self):
        return [purpose for purpose, _ in self.calls]


def fragments(*items: dict):
    return "session_fragments", {"fragments": list(items)}


def three_parts():
    windows = (window(0, 60), window(60, 120), window(120, 180))
    raw = [
        row(10, 15, "We open with algebra."),
        row(45, 55, "Hold that proof on line seven while I switch the recording."),
        row(61, 65, "Back to your question about line seven of that proof."),
        row(108, 118, "Before the demo, hold on to that triangle step."),
        row(122, 125, "Continuing that triangle step, now measure its area."),
        row(160, 165, "Class dismissed."),
    ]
    response = [
        fragments(course(1, 2, description_md="Algebra")),
        fragments(course(1, 2, title_md="Geometry", description_md="Triangles")),
        fragments(course(1, 2, description_md="Area", course_end=quote(2, "Class dismissed"))),
        ("course_boundary", continuation(2, "that proof on line seven", 1, "line seven")),
        ("course_boundary", continuation(2, "that triangle step", 1, "that triangle step")),
    ]
    return windows, raw, response


def test_topic_changes_three_recordings_compile_one_course():
    windows, raw, responses = three_parts()
    ask = ModelDouble(*responses)
    result = segment_sessions(ask, windows, raw[::-1])
    assert not ask.responses
    assert ask.purposes == ["session_fragments"] * 3 + ["course_boundary"] * 2
    assert (result.course_activities, result.merged_boundaries, result.uncertain_boundaries) == (
        1,
        2,
        0,
    )
    assert result.unclassified_rows == 0 and result.segmentation.empty_reason == ""
    [activity] = result.segmentation.activities
    assert (activity.start, activity.end) == (at(10), at(165))
    assert activity.evidence_ids == [r.id for r in raw]
    assert activity.description_md == "Algebra\n\nTriangles\n\nArea"
    all_inputs = repr(ask.calls)
    assert "UNSENT" not in all_inputs and not any(r.id in all_inputs for r in raw)
    assert "Geometry" not in repr(ask.calls[3:])  # No titles for boundary decisions.


def test_two_distinct_classes_in_one_window_keep_distinct():
    raw = [row(0, 14, "First class dismissed."), row(15, 60, "Welcome to the next class.")]
    ask = ModelDouble(
        fragments(
            course(1, 1, course_end=quote(1, "First class dismissed")),
            course(2, 2, course_start=quote(2, "Welcome to the next class")),
        )
    )
    result = segment_sessions(ask, (window(0, 60),), raw)
    assert result.course_activities == 2
    assert [a.evidence_ids for a in result.segmentation.activities] == [[r.id] for r in raw]
    assert result.merged_boundaries == result.uncertain_boundaries == 0


def test_no_course_split_only_for_topic_change():
    raw = [row(0, 14, "Now cover the next topic."), row(15, 60, "Work on the exercise.")]
    with pytest.raises(ValueError, match="course_split_requires_explicit_marker"):
        segment_sessions(ModelDouble(fragments(course(1, 1), course(2, 2))), (window(0, 60),), raw)


@pytest.mark.parametrize("marker", ["course_end", "course_start", "other"])
def test_explicit_stops_or_other_never_go_to_the_seam_model(marker):
    raw = [row(40, 60, "First class dismissed."), row(60, 80, "Welcome to the next class.")]
    left, right = course(1, 1), course(1, 1)
    if marker == "other":
        right["kind"] = "other"
    elif marker == "course_end":
        left[marker] = quote(1, "First class dismissed")
    else:
        right[marker] = quote(1, "Welcome to the next class")
    ask = ModelDouble(fragments(left), fragments(right))
    result = segment_sessions(ask, (window(0, 60), window(60, 120)), raw)
    assert ask.purposes == ["session_fragments"] * 2
    assert len(result.segmentation.activities) == 2
    assert result.course_activities == (1 if marker == "other" else 2)
    assert result.merged_boundaries == result.uncertain_boundaries == 0


@pytest.mark.parametrize("middle", [[], [row(70, 75, " \n")]])
def test_blank_or_missing_middle_is_not_a_class_boundary_or_a_merge(middle):
    windows = (window(0, 60), window(60, 120), window(120, 180))
    raw = [row(40, 60, "Pause."), *middle, row(120, 130, "Resume.")]
    ask = ModelDouble(fragments(course(1, 1)), fragments(), fragments(course(1, 1)))
    result = segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 3
    assert result.course_activities == 2
    assert result.segmentation.empty_reason == "session_windows_without_transcript:2"
    assert all("不能据此判断" in a.description_md for a in result.segmentation.activities)
    assert result.merged_boundaries == result.uncertain_boundaries == 0
    assert result.unclassified_rows == 0
    assert ask.calls[1][1]["blank_rows"] == len(middle)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("synthetic transport failure"),
        InputBudgetExceeded(),
        GatewayError(502, "provider_rejected"),
        GatewayError(503, "gateway_outcome_unknown"),
    ],
)
def test_failed_middle_stops_all_later_calls(failure):
    windows, raw, _ = three_parts()
    ask = ModelDouble(fragments(course(1, 2)), ("session_fragments", failure))
    with pytest.raises(type(failure)) as raised:
        segment_sessions(ask, windows, raw)
    assert raised.value is failure
    assert ask.purposes == ["session_fragments"] * 2
    assert len(ask.calls[1][1]["evidence"]) == 2  # No shrinking, retries or fallback.


@pytest.mark.parametrize("later", [60, 240])
def test_same_title_with_ambiguous_evidence_or_wide_gap_stays_separate(later):
    raw = [row(50, 54, "Topic: algebra."), row(later + 1, later + 2, "Topic: algebra.")]
    take = ModelDouble(
        fragments(course(1, 1)),
        fragments(course(1, 1)),
        *(("course_boundary", {"verdict": "uncertain", "pair": None}),) if later == 60 else (),
    )
    result = segment_sessions(take, (window(0, 60), window(later, later + 60)), raw)
    assert not take.responses
    assert (result.course_activities, result.merged_boundaries, result.uncertain_boundaries) == (
        2,
        0,
        1,
    )
    assert all("尚不确定" in a.description_md for a in result.segmentation.activities)


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (fragments(course(1, 3)), "not_partition"),
        (fragments(course(2, 1)), "not_partition"),
        (fragments(course(1, 1), course(1, 1)), "not_partition"),
        (fragments(course(2, 2), course(1, 1)), "not_partition"),
        (fragments(course(1, 2), course(2, 2)), "not_partition"),
        (fragments(course(0, 2)), "greater_than_equal"),
        (fragments(course(True, 2)), "int_type"),
        (fragments(course(1, 2, end="invented")), "extra_forbidden"),
        (fragments(course(1, 2, course_start=quote(0, "Not observed"))), "greater_than_equal"),
        (
            fragments(course(1, 2, title_md=" ", course_start=quote(9, "Bad hint"))),
            "requires_title",
        ),
        (
            fragments(
                course(1, 2, description_md="[[person:invented]]", course_start=quote(9, "Bad"))
            ),
            "must_use_anonymous_titles",
        ),
        (
            fragments(course(1, 2, kind="other", course_start=quote(1, "Proof"))),
            "non_course_marker",
        ),
    ],
)
def test_invalid_indices_and_open_schema_fail_closed(result, match):
    raw = [row(1, 9, "Proof."), row(11, 20, "Next topic.")]
    with pytest.raises((ValueError, ValidationError), match=match):
        segment_sessions(ModelDouble(result), (window(0, 60),), raw)


@pytest.mark.parametrize(
    ("decision", "match"),
    [
        ({"verdict": "same_class", "pair": None}, "requires_pair"),
        (continuation(1, "unseen introduction", 1, "Resume line seven"), "not_in_excerpt"),
        (continuation(9, "Line seven", 1, "Resume line seven"), "out_of_range"),
        (continuation(1, "Line seven", 1, "fabricated"), "not_in_excerpt"),
        (
            {**continuation(1, "Line seven", 1, "Resume line seven"), "merge": True},
            "extra_forbidden",
        ),
    ],
)
def test_boundary_requires_visible_quotes_on_both_sides(decision, match):
    raw = [
        row(50, 55, "unseen introduction " + "x" * 800 + " Line seven"),
        row(60, 65, "Resume line seven " + "x" * 800 + " unseen ending"),
    ]
    ask = ModelDouble(
        fragments(course(1, 1)), fragments(course(1, 1)), ("course_boundary", decision)
    )
    with pytest.raises(ValueError, match=match):
        segment_sessions(ask, (window(0, 60), window(60, 120)), raw)
    excerpts = ask.calls[-1][1]
    assert all(
        len(e["text"]) <= 240 and e["clipped"] for side in ("left", "right") for e in excerpts[side]
    )
    assert "unseen" not in repr(excerpts)


def test_same_class_pair_at_exact_half_open_seam():
    left, right = row(59, 60, "Pause before line seven"), row(60, 61, "Resume at line seven")
    ask = ModelDouble(
        fragments(course(1, 1)),
        fragments(course(1, 1)),
        ("course_boundary", continuation(1, "Pause before line seven", 1, "Resume at line seven")),
    )
    result = segment_sessions(ask, (window(0, 60), window(60, 120)), [right, left])
    [activity] = result.segmentation.activities
    assert (activity.start, activity.end) == (at(59), at(61))
    assert activity.evidence_ids == [left.id, right.id]


def test_true_overlap_across_seam_cannot_use_a_different_eligible_pair():
    windows = (window(0, 60), window(60, 120))
    early, crossing, overlaps = (
        row(50, 53, "Hold line seven"),
        row(58, 63, "Another observation"),
        row(60, 65, "Continue line seven"),
    )
    ask = ModelDouble(fragments(course(1, 2)), fragments(course(1, 1)))
    result = segment_sessions(ask, windows, [crossing, overlaps, crossing, early])
    assert ask.purposes == ["session_fragments"] * 2
    assert result.merged_boundaries == result.course_activities == 0
    assert result.unclassified_rows == 3  # Duplicate raw ID counted only once.
    assert not result.segmentation.activities
    assert result.segmentation.empty_reason == "session_all_rows_unclassified"
    assert ask.calls[0][1]["evidence"][1]["crosses_window_end"] is True


@pytest.mark.parametrize("assign_middle", [False, True])
def test_overlaps_demote_only_conflicting_component_including_omitted_rows(assign_middle):
    raw = [
        row(1, 4, "Independent observation"),
        row(10, 25, "Overlap starts here"),
        row(15, 19, "Nested in the earlier observation"),
        row(24, 28, "Still overlaps the earlier observation"),
        row(28, 32, "A separate observation at the exact end"),
    ]
    middle = [course(3, 3, kind="other")] if assign_middle else []
    ask = ModelDouble(fragments(course(1, 1), course(2, 2), *middle, course(4, 4), course(5, 5)))
    result = segment_sessions(ask, (window(0, 60),), raw[::-1])
    assert result.unclassified_rows == 3
    assert result.segmentation.empty_reason == ""
    assert [a.evidence_ids for a in result.segmentation.activities] == [[raw[0].id], [raw[4].id]]
    assert all("尚未分类" in a.description_md for a in result.segmentation.activities)


def test_overlapping_evidence_within_one_supported_range_remains_classified():
    raw = [row(5, 25, "Long observation"), row(15, 19, "Nested observation")]
    result = segment_sessions(ModelDouble(fragments(course(1, 2))), (window(0, 60),), raw)
    assert result.unclassified_rows == 0
    [activity] = result.segmentation.activities
    assert activity.evidence_ids == [r.id for r in raw]
    assert (activity.start, activity.end) == (at(5), at(25))


def test_prefix_middle_and_tail_gaps_remain_unclassified_and_are_not_stops():
    raw = [row(i, i + 1, f"Observed row {i}") for i in range(5, 30, 5)]
    ask = ModelDouble(fragments(course(2, 2, course_end=quote(3, "Observed row 15")), course(4, 4)))
    result = segment_sessions(ask, (window(0, 60),), raw[::-1])
    assert result.unclassified_rows == 3 and result.segmentation.empty_reason == ""
    assert result.course_activities == 2 and result.merged_boundaries == 0
    assert result.discarded_boundary_hints == 1
    left, right = result.segmentation.activities
    assert left.evidence_ids == [raw[1].id] and right.evidence_ids == [raw[3].id]
    assert (left.start, left.end, right.start, right.end) == (at(10), at(11), at(20), at(21))
    assert "下课边界尚不确定" in left.description_md
    assert all("尚未分类" in a.description_md for a in (left, right))


def test_unclassified_nonblank_window_is_not_missing_and_cannot_be_bridged():
    ask = ModelDouble(fragments(course(1, 1)), fragments(), fragments(course(1, 1)))
    windows = (window(0, 60), window(60, 120), window(120, 180))
    raw = [row(50, 60, "Line seven"), row(70, 80, "Unclear"), row(120, 130, "Line seven")]
    result = segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 3
    assert result.unclassified_rows == 1 and result.segmentation.empty_reason == ""
    assert result.course_activities == 2 and result.merged_boundaries == 0
    assert [a.evidence_ids for a in result.segmentation.activities] == [[raw[0].id], [raw[2].id]]
    assert all("尚未分类" in a.description_md for a in result.segmentation.activities)


def test_all_rows_omitted_is_distinct_from_a_missing_window():
    selected = window(0, 60)
    raw = [row(5, 6, "Unclear"), row(10, 15, "Unclear too"), row(20, 21, " ")]
    result = segment_sessions(ModelDouble(fragments()), (selected,), raw)
    assert result.unclassified_rows == 2
    assert result.segmentation.empty_reason == "session_all_rows_unclassified"
    assert not result.segmentation.activities


@pytest.mark.parametrize("edge", ["left_tail", "right_prefix"])
def test_unclassified_edge_blocks_seam_without_hiding_known_fragments(edge):
    raw = [row(50, 52, "Hold line seven"), row(60, 62, "Resume line seven")]
    unknown = row(56, 58, "Unclear") if edge == "left_tail" else row(60, 60, "Unclear")
    if edge == "right_prefix":
        raw[1] = row(61, 62, "Resume line seven")
    ask = ModelDouble(
        fragments(course(1, 1)), fragments(course(2, 2) if edge == "right_prefix" else course(1, 1))
    )
    result = segment_sessions(ask, (window(0, 60), window(60, 120)), [*raw, unknown])
    assert ask.purposes == ["session_fragments"] * 2
    assert result.unclassified_rows == 1 and result.merged_boundaries == 0
    assert [a.evidence_ids for a in result.segmentation.activities] == [[r.id] for r in raw]
    assert all("尚未分类" in a.description_md for a in result.segmentation.activities)


def test_tail_omitted_in_window_24_is_not_an_error_or_absorbed_into_class():
    windows = tuple(window(i * 60, (i + 1) * 60) for i in range(24))
    raw = [row(i * 60 + 2, i * 60 + 5, "Observed activity") for i in range(24)]
    raw.append(row(1410, 1415, "Unclassified tail in the final window"))
    other = fragments(course(1, 1, kind="other"))
    ask = ModelDouble(*([other] * 23), fragments(course(1, 1)))
    result = segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 24
    assert result.unclassified_rows == 1 and result.segmentation.empty_reason == ""
    assert result.course_activities == 1 and result.merged_boundaries == 0
    assert len(result.segmentation.activities) == 24
    final = result.segmentation.activities[-1]
    assert final.evidence_ids == [raw[-2].id] and final.end == raw[-2].event_end
    assert "尚未分类" in final.description_md


def test_point_evidence_occupies_only_one_tick():
    a, b = row(59.999999, 59.999999, "One observed word"), row(60, 60, "Another word")
    ask = ModelDouble(fragments(course(1, 1)), fragments(course(1, 1, kind="other")))
    result = segment_sessions(ask, (window(0, 60), window(60, 120)), [a, b])
    left, right = result.segmentation.activities
    assert (left.start, left.end) == (at(59.999999), at(60))
    assert (right.start, right.end) == (at(60), at(60.000001))


@pytest.mark.parametrize(
    ("windows", "raw", "match"),
    [
        ((), [], "window_count"),
        (tuple(window(i, i + 1) for i in range(25)), [], "window_count"),
        ((window(0, 901),), [], "window_duration"),
        ((window(0, 60), window(59, 120)), [], "overlap_or_unsorted"),
        ((window(60, 120), window(0, 60)), [], "overlap_or_unsorted"),
        ((window(0, 60),), [row(-1, 1, "Out of scope")], "outside_selected"),
        ((window(0, 60), window(61, 120)), [row(59, 62, "Across a gap")], "outside_selected"),
        ((window(0, 60),), [row(15, 40, None)], "transcript_invalid"),
    ],
)
def test_invalid_input_fails_before_model_call(windows, raw, match):
    ask = ModelDouble()
    with pytest.raises(ValueError, match=match):
        segment_sessions(ask, windows, raw)
    assert not ask.calls


def _seed(raw: list[RawRecord]):
    store = MemoryStorage(fixture_registry())
    store.client("adapter").write(
        [
            AppendRaw(NewRawRecord.model_validate(r.model_dump(exclude={"version", "ingested_at"})))
            for r in raw
        ],
        Context.system(),
    ).check()
    rule_id = new_id()
    client = store.client("extraction")
    client.write(
        [Create("rule", rule_id, Rule(scope="Math", instruction="Take notes").model_dump())],
        Context.system(),
    ).check()
    return client, rule_id


def test_plan_generates_and_creates_once_after_all_course_boundaries():
    windows, raw, responses = three_parts()
    client, rule_id = _seed(raw)
    ask = ModelDouble(
        *responses, ("generation", {"body_md": "Class notes", "evidence_ids": [r.id for r in raw]})
    )
    with client.snapshot() as reader:
        ops, detail = extract_plan(
            reader,
            Command(kind="extract", interval=window(0, 180)),
            new_id(),
            ask,
            activity_min_duration_minutes=1,
            transcript_only=True,
            transcript_summary_rule_id=rule_id,
            session_windows=windows,
        )
    assert ask.purposes == ["session_fragments"] * 3 + ["course_boundary"] * 2 + ["generation"]
    assert ask.calls[-1][1]["evidence"] and [e["id"] for e in ask.calls[-1][1]["evidence"]] == [
        r.id for r in raw
    ]
    assert {
        "activities": 1,
        "course_activities": 1,
        "merged_boundaries": 2,
        "uncertain_boundaries": 0,
        "discarded_boundary_hints": 0,
    }.items() <= detail.items()
    activity, selection, artifact = [op for op in ops if isinstance(op, Create)]
    assert [op.data_type for op in (activity, selection, artifact)] == [
        "activity",
        "rule_selection",
        "artifact",
    ]
    assert selection.payload["activity_id"] == artifact.payload["activity_id"] == activity.id
    assert len([op for op in ops if isinstance(op, RegisterDependencies)]) == 3


@pytest.mark.parametrize("same_class", [False, True])
def test_session_floor_runs_after_confirmed_same_class_merge(same_class):
    windows = (window(0, 600), window(600, 1200))
    raw = [
        row(300, 600, "Paused that proof for a moment"),
        row(600, 900, "Continue that proof at line seven"),
    ]
    verdict = continuation(1, "that proof", 1, "that proof")
    if not same_class:
        verdict["verdict"] = "different_class"
    ask = ModelDouble(
        fragments(course(1, 1)),
        fragments(course(1, 1)),
        ("course_boundary", verdict),
    )
    result = segment_sessions(ask, windows, raw, minimum=timedelta(minutes=10))
    assert ask.purposes == ["session_fragments", "session_fragments", "course_boundary"]
    assert result.course_activities == int(same_class)
    if same_class:
        [activity] = result.segmentation.activities
        assert (activity.start, activity.end) == (at(300), at(900))
        assert activity.evidence_ids == [r.id for r in raw]
        assert not result.below_min_duration
    else:
        assert not result.segmentation.activities and result.unclassified_rows == 2
        assert result.segmentation.empty_reason == "no_supported_activity"
        assert len(result.below_min_duration) == 2


def test_plan_never_creates_an_activity_or_generates_markdown_for_unknown_rows():
    selected = window(0, 900)
    known = row(5, 605, "Observed class discussion.")
    unknown = row(615, 616, "Unclassified tail.")
    client, rule_id = _seed([known, unknown])
    ask = ModelDouble(
        fragments(course(1, 1)),
        ("generation", {"body_md": "Discussion", "evidence_ids": [known.id]}),
    )
    with client.snapshot() as reader:
        ops, _ = extract_plan(
            reader,
            Command(kind="extract", interval=selected),
            new_id(),
            ask,
            transcript_only=True,
            transcript_summary_rule_id=rule_id,
            session_windows=(selected,),
        )
        assert reader.get(unknown.id) is not None
    assert ask.purposes == ["session_fragments", "generation"]
    assert [e["id"] for e in ask.calls[-1][1]["evidence"]] == [known.id]
    assert [op.data_type for op in ops if isinstance(op, Create)] == [
        "activity",
        "rule_selection",
        "artifact",
    ]
    assert not any(isinstance(op, AppendRaw) for op in ops)


def test_plan_fails_without_generation_if_selected_window_is_missing():
    windows, raw, _ = three_parts()
    client, rule_id = _seed(raw[:2] + raw[-2:])
    ask = ModelDouble(fragments(course(1, 2)), fragments(), fragments(course(1, 2)))
    with client.snapshot() as reader, pytest.raises(Blocked, match="windows_without_transcript"):
        extract_plan(
            reader,
            Command(kind="extract", interval=window(0, 180)),
            new_id(),
            ask,
            transcript_only=True,
            transcript_summary_rule_id=rule_id,
            session_windows=windows,
        )
    assert ask.purposes == ["session_fragments"] * 3


def test_boundary_keeps_later_instance_of_same_course_separate():
    windows = (window(0, 60), window(60, 120))
    raw = [
        row(40, 60, "Earlier in today's meeting we proved that result."),
        row(60, 68, "In our previous class meeting we proved that result."),
    ]
    decision = continuation(1, "today's meeting", 1, "previous class meeting")
    decision["verdict"] = "different_class"
    ask = ModelDouble(
        fragments(course(1, 1)), fragments(course(1, 1)), ("course_boundary", decision)
    )
    result = segment_sessions(ask, windows, raw)
    assert (result.course_activities, result.merged_boundaries, result.uncertain_boundaries) == (
        2,
        0,
        0,
    )


def test_farther_rows_are_not_sent_or_eligible_at_boundary():
    left = [row(i, i + 2, f"Left row {i}") for i in (5, 15, 25, 35, 45, 55)]
    right = [row(i, i + 2, f"Right row {i}") for i in (60, 70, 80, 90, 100, 110)]
    ask = ModelDouble(
        fragments(course(1, 6)),
        fragments(course(1, 6)),
        ("course_boundary", continuation(1, "Left row 5", 1, "Right row 60")),
    )
    with pytest.raises(ValueError, match="pair_out_of_range_or_overlapping"):
        segment_sessions(ask, (window(0, 60), window(60, 120)), left + right)
    data = ask.calls[-1][1]
    assert [e["i"] for e in data["left"]] == [4, 5, 6]
    assert [e["i"] for e in data["right"]] == [1, 2, 3]
    assert data["left_omitted_rows"] == data["right_omitted_rows"] == 3


def test_cut_through_row_marks_end_uncertain_and_blocks_seam_even_with_eligible_pair():
    raw = [
        row(50, 55, "Pause at line seven."),
        row(59, 63, "And with that the class ends."),
        row(64, 68, "Back to line seven."),
    ]
    ask = ModelDouble(
        fragments(course(1, 2, course_end=quote(2, "class ends"))),
        fragments(course(1, 1)),
    )
    result = segment_sessions(ask, (window(0, 60), window(60, 120)), raw)
    assert ask.purposes == ["session_fragments"] * 2
    assert result.discarded_boundary_hints == result.uncertain_boundaries == 1
    assert result.merged_boundaries == 0
    left, right = result.segmentation.activities
    assert "下课边界尚不确定" in left.description_md
    assert all("是否属于同一节课尚不确定" in a.description_md for a in (left, right))


def test_schema_valid_window_twenty_with_out_of_edge_end_does_not_stop_later_windows():
    windows = tuple(window(i * 60, (i + 1) * 60) for i in range(24))
    other = fragments(course(1, 1, kind="other"))
    raw = [row(i * 60 + 2, i * 60 + 3, "Other activity.") for i in range(18)]
    raw += [row(1120, 1122, "Pause at line seven.")]
    twenty = [
        row(1140, 1142, "Back to line seven."),
        row(1150, 1152, "A hypothetical dismissal."),
        row(1160, 1162, "A classroom discussion."),
        row(1170, 1172, "Work on an exercise."),
        row(1180, 1182, "Questions about the exercise."),
    ]
    twenty_one = row(1202, 1204, "Review the exercise.")
    raw += [*twenty, twenty_one]
    raw += [row(i * 60 + 2, i * 60 + 3, "Other activity.") for i in range(21, 24)]
    ask = ModelDouble(
        *([other] * 18),
        fragments(course(1, 1)),
        fragments(course(1, 5, course_end=quote(2, "hypothetical dismissal"))),
        fragments(course(1, 1)),
        *([other] * 3),
        ("course_boundary", continuation(1, "line seven", 1, "line seven")),
    )
    result = segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 24 + ["course_boundary"]
    assert not ask.responses
    assert result.discarded_boundary_hints == 1
    assert (result.course_activities, result.merged_boundaries, result.uncertain_boundaries) == (
        2,
        1,
        1,
    )
    left, right = result.segmentation.activities[18:20]
    assert left.evidence_ids == [r.id for r in raw[18:24]]
    assert right.evidence_ids == [twenty_one.id]
    assert "下课边界尚不确定" in left.description_md
    assert "开课边界尚不确定" not in left.description_md
    assert all("是否属于同一节课尚不确定" in a.description_md for a in (left, right))


@pytest.mark.parametrize(
    ("left_hint", "right_hint", "expected_note"),
    [
        ({"course_end": quote(2, "Back to class")}, {}, "下课边界尚不确定"),
        ({}, {"course_start": quote(2, "Invented opening")}, "开课边界尚不确定"),
        ({}, {"course_start": quote(2, "...")}, "开课边界尚不确定"),
    ],
)
def test_same_window_course_split_that_depends_on_bad_marker_is_uncertain(
    left_hint,
    right_hint,
    expected_note,
):
    raw = [row(5, 6, "A class discussion."), row(15, 16, "Back to class ...")]
    ask = ModelDouble(fragments(course(1, 1, **left_hint), course(2, 2, **right_hint)))
    result = segment_sessions(ask, (window(0, 60),), raw)
    assert ask.purposes == ["session_fragments"]
    assert result.discarded_boundary_hints == 1
    assert result.merged_boundaries == result.uncertain_boundaries == 0  # Inter-window seams.
    assert result.course_activities == 2
    left, right = result.segmentation.activities
    assert [a.evidence_ids for a in (left, right)] == [[r.id] for r in raw]
    assert all("是否属于同一节课尚不确定" in a.description_md for a in (left, right))
    assert expected_note in (left if left_hint else right).description_md


def test_reversed_course_markers_discard_both_and_block_both_seams():
    windows = (window(0, 60), window(60, 120), window(120, 180))
    raw = [
        row(55, 59, "Before the break."),
        row(62, 64, "Class dismissed."),
        row(111, 116, "Welcome to class."),
        row(121, 125, "After the break."),
    ]
    ask = ModelDouble(
        fragments(course(1, 1)),
        fragments(course(1, 2, course_start=quote(2, "Welcome"), course_end=quote(1, "dismissed"))),
        fragments(course(1, 1)),
    )
    result = segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 3
    assert result.discarded_boundary_hints == result.uncertain_boundaries == 2
    assert result.merged_boundaries == 0
    first, middle, last = result.segmentation.activities
    assert all("是否属于同一节课尚不确定" in a.description_md for a in (first, middle, last))
    assert "开课边界尚不确定" in middle.description_md
    assert "下课边界尚不确定" in middle.description_md


def test_plan_reports_discarded_hint_without_storing_its_quote():
    selected, raw = window(0, 900), [row(5, 605, "Observed class discussion.")]
    client, rule_id = _seed(raw)
    fabricated = "Fabricated boundary quote"
    ask = ModelDouble(
        fragments(course(1, 1, course_start=quote(1, fabricated))),
        ("generation", {"body_md": "Discussion", "evidence_ids": [raw[0].id]}),
    )
    with client.snapshot() as reader:
        ops, detail = extract_plan(
            reader,
            Command(kind="extract", interval=selected),
            new_id(),
            ask,
            transcript_only=True,
            transcript_summary_rule_id=rule_id,
            session_windows=(selected,),
        )
    assert ask.purposes == ["session_fragments", "generation"]
    assert detail["discarded_boundary_hints"] == 1
    activity = next(op for op in ops if isinstance(op, Create) and op.data_type == "activity")
    assert "开课边界尚不确定" in activity.payload["description_md"]
    assert fabricated not in repr((ask.calls, ops, detail))


def test_boundary_failure_propagates_before_later_boundary_calls():
    windows, raw, responses = three_parts()
    ask = ModelDouble(*responses[:3], ("course_boundary", RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        segment_sessions(ask, windows, raw)
    assert ask.purposes == ["session_fragments"] * 3 + ["course_boundary"]
