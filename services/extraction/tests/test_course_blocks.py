"""Whole-block course segmentation with synthetic transcripts and no live model."""

import json
from datetime import timedelta
from typing import Any

import pytest
from ear_contracts import NewRawRecord, RawRecord, format_utc, new_id
from ear_extraction.course_blocks import COURSE_BLOCK_SYSTEM, _BlockResult, segment_course_blocks
from ear_extraction.demo import fixture_registry
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.planning import SessionCoverageBlocked, extract_plan
from ear_llm_gateway.models import CallRequest
from ear_llm_gateway.openai_compatible import OpenAICompatibleProvider
from ear_llm_gateway.output_json import OutputSchema
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage
from ear_storage_client.testing import make_raw, ts
from pydantic import BaseModel

BASE = ts("2026-11-05T09:00:00Z")


def at(seconds: int):
    return BASE + timedelta(seconds=seconds)


def window(start: int, end: int) -> Interval:
    return Interval(start=at(start), end=at(end))


def row(start: int, end: int, text: str) -> RawRecord:
    source = make_raw(
        "transcript_segment",
        {"text": text, "audio_ref_id": "HIDDEN_AUDIO", "speaker_id": "HIDDEN_SPEAKER"},
        start=at(start),
        end=at(end),
        device="HIDDEN_DEVICE",
    )
    return RawRecord(**source.model_dump(), ingested_at=BASE)


def cite(i: int, quote: str) -> dict:
    return {"i": i, "quote": quote}


def span(first: int, last: int, left: str, right: str, **kw: Any) -> dict:
    return {
        "kind": "course",
        "first": first,
        "last": last,
        "title_md": "Lecture",
        "description_md": "Observed material",
        "first_evidence": cite(first, left),
        "last_evidence": cite(last, right),
        **kw,
    }


class ModelDouble:
    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, purpose: str, data: dict[str, Any], model: type[BaseModel]):
        self.calls.append((purpose, data))
        if purpose == "course_blocks":
            assert model is _BlockResult
        reply = self.replies.pop(0)
        OutputSchema(model.model_json_schema()).validate(reply)
        return model.model_validate(reply)


def test_six_windows_one_lecture_through_an_unclear_turn():
    windows = tuple(window(i, i + 900) for i in range(0, 5400, 900))
    raw = [
        row(10, 12, "Opening on vector spaces"),
        row(600, 602, "Example with a plane"),
        row(910, 912, "Let's compute a basis"),
        row(1820, 1822, "[unintelligible]"),
        row(2710, 2712, "Back to the basis calculation"),
        row(3601, 3604, "Student asks a question about that basis"),
        row(4510, 4513, "The answer gives the closing result"),
        row(5390, 5392, "Unclear outside the session"),
    ]
    ask = ModelDouble({"spans": [span(1, 7, "vector spaces", "closing result")]})
    result = segment_course_blocks(ask, windows, [*reversed(raw), raw[4]])
    [activity] = result.segmentation.activities
    assert (activity.start, activity.end) == (raw[0].event_start, raw[6].event_end)
    assert activity.evidence_ids == [r.id for r in raw[:7]]
    assert result.merged_boundaries == 5
    assert result.course_activities == 1 and result.unclassified_rows == 1
    assert not result.segmentation.empty_reason
    [(purpose, data)] = ask.calls
    assert purpose == "course_blocks" and set(data) == {"evidence", "row_count", "coverage"}
    assert len(data["coverage"]) == 6 and data["row_count"] == 8
    assert [e["i"] for e in data["evidence"]] == list(range(1, 9))
    assert not any(x in repr(data) for x in ("HIDDEN_", *(r.id for r in raw)))


def two_classes():
    windows = (window(0, 900), window(900, 1800))
    raw = [
        row(20, 25, "The proof begins with a definition"),
        row(985, 990, "That concludes today's proof lecture"),
        row(1004, 1009, "Welcome to the history of art lecture"),
        row(1700, 1705, "We finish with a fresco"),
    ]
    first = span(1, 2, "with a definition", "concludes today's proof lecture")
    second = span(3, 4, "history of art", "a fresco")
    second["split_before"] = {
        "left": cite(2, "concludes today's proof lecture"),
        "right": cite(3, "Welcome to the history of art lecture"),
    }
    return windows, raw, first, second


def test_two_distinct_classes_inside_touching_windows():
    windows, raw, first, second = two_classes()
    ask = ModelDouble({"spans": [first, second]})
    result = segment_course_blocks(ask, windows, raw)
    left, right = result.segmentation.activities
    assert left.end == raw[1].event_end and right.start == raw[2].event_start
    assert left.evidence_ids == [r.id for r in raw[:2]]
    assert right.evidence_ids == [r.id for r in raw[2:]]
    assert result.course_activities == 2 and result.merged_boundaries == 1
    assert result.uncertain_boundaries == result.unclassified_rows == 0
    assert len(ask.calls) == 1


@pytest.mark.parametrize(
    "hint", [None, {}, {"left": {"i": 2}}, {"left": {"quote": "unseen"}}, {"right": cite(99, "x")}]
)
def test_incomplete_split_hint_retains_boundaries_as_uncertain(hint):
    windows, raw, first, second = two_classes()
    second["split_before"] = hint
    result = segment_course_blocks(ModelDouble({"spans": [first, second]}), windows, raw)
    assert result.uncertain_boundaries == 1 and result.discarded_boundary_hints == (
        hint is not None
    )
    assert all("尚不确定" in a.description_md for a in result.segmentation.activities)
    assert result.unclassified_rows == 0


def test_wide_capture_gap_not_bridged_and_unknown_never_guessed():
    windows = (window(0, 900), window(1501, 2401), window(3002, 3902))
    raw = [
        row(10, 15, "Known lecture A"),
        row(1700, 1710, "Known lecture B"),
        row(3200, 3202, "[?]"),
    ]
    ask = ModelDouble(
        {"spans": [span(1, 1, "lecture", "lecture A")]},
        {"spans": [span(1, 1, "lecture", "lecture B")]},
        {"spans": []},
    )
    result = segment_course_blocks(ask, windows, raw)
    assert [d["evidence"][0]["i"] for _, d in ask.calls] == [1, 1, 1]
    assert [a.evidence_ids for a in result.segmentation.activities] == [[r.id] for r in raw[:2]]
    assert result.merged_boundaries == 0 and result.unclassified_rows == 1


def test_gap_of_exactly_ten_minutes_and_silent_asr_are_not_capture_breaks():
    windows = (window(0, 900), window(1500, 2400))
    raw = [row(10, 12, "Course begins"), row(2350, 2353, "Course ends")]
    ask = ModelDouble({"spans": [span(1, 2, "Course begins", "Course ends")]})
    result = segment_course_blocks(ask, windows, raw)
    assert len(ask.calls) == 1 and result.course_activities == 1


def test_missing_and_blank_windows_are_explicit_not_unclassified_rows():
    windows = (window(0, 60), window(60, 120), window(120, 180), window(180, 240))
    raw = [row(10, 15, "A lecturer"), row(65, 70, " \n "), row(190, 195, "Still teaching")]
    ask = ModelDouble({"spans": [span(1, 2, "A lecturer", "Still teaching")]})
    result = segment_course_blocks(ask, windows, raw)
    assert result.segmentation.empty_reason == "session_windows_without_transcript:2,3"
    assert result.unclassified_rows == 0 and result.merged_boundaries == 0
    [activity] = result.segmentation.activities
    assert "缺少可用转写" in activity.description_md
    coverage = ask.calls[0][1]["coverage"]
    assert [(r["first"], r["last"], r["blank_rows"]) for r in coverage] == [
        (1, 1, 0),
        (None, None, 1),
        (None, None, 0),
        (2, 2, 0),
    ]


def test_no_usable_rows_sends_nothing():
    ask = ModelDouble()
    result = segment_course_blocks(ask, (window(0, 60),), [row(1, 3, " ")])
    assert not ask.calls and not result.segmentation.activities
    assert result.segmentation.empty_reason == "session_windows_without_transcript:1"


def test_cross_window_record_uses_original_end_and_is_sent_once():
    a, b = row(50, 71, "First context"), row(75, 78, "Unclear")
    ask = ModelDouble({"spans": [span(1, 1, "First", "context")]})
    result = segment_course_blocks(ask, (window(0, 60), window(60, 120)), [a, a, b])
    [activity] = result.segmentation.activities
    assert activity.evidence_ids == [a.id] and activity.end == a.event_end
    assert ask.calls[0][1]["evidence"][0]["e"] == format_utc(a.event_end)


def test_required_anchor_matches_exact_visible_substring_including_tail():
    raw = [row(10, 12, "Opening " + "a" * 100 + "UNSHOWN" + "b" * 100 + " closing cue")]
    windows = (window(0, 60),)
    result = segment_course_blocks(
        ModelDouble({"spans": [span(1, 1, "Opening", "closing cue")]}), windows, raw
    )
    assert result.course_activities == 1
    for invalid in ("UNSHOWN", "Closing cue", "ab"):
        with pytest.raises(ValueError, match="anchor_not_in_excerpt"):
            segment_course_blocks(
                ModelDouble({"spans": [span(1, 1, "Opening", invalid)]}), windows, raw
            )


def test_required_anchor_cannot_cite_unsampled_row_or_wrong_index():
    windows = (window(0, 900),)
    raw = [row(i * 2, i * 2 + 1, "Clearly a synthetic lesson") for i in range(40)]
    for item in (
        span(2, 40, "lesson", "lesson"),
        span(1, 40, "lesson", "lesson") | {"last_evidence": cite(1, "lesson")},
    ):
        with pytest.raises(ValueError, match="anchor_not_in_excerpt"):
            segment_course_blocks(ModelDouble({"spans": [item]}), windows, raw)


@pytest.mark.parametrize("reason", ["indices_not_ordered", "times_overlap"])
def test_invalid_span_overlap_is_rejected(reason):
    raw = [row(10, 55, "Lecture A"), row(50, 58, "Lecture B")]
    first = span(1, 2, "A", "B") if reason == "indices_not_ordered" else span(1, 1, "A", "A")
    ask = ModelDouble({"spans": [first, span(2, 2, "B", "B")]})
    with pytest.raises(ValueError, match=reason):
        segment_course_blocks(ask, (window(0, 60),), raw)


def test_earlier_overlapping_row_cannot_extend_beyond_the_supported_last_anchor():
    raw = [row(10, 57, "An extended row"), row(50, 55, "An embedded row")]
    ask = ModelDouble({"spans": [span(1, 2, "extended row", "embedded row")]})
    with pytest.raises(ValueError, match="span_end_not_supported"):
        segment_course_blocks(ask, (window(0, 60),), raw)


@pytest.mark.parametrize("character", ["𠀀", "\x01"])
def test_full_allowlist_excerpt_payload_fits_actual_compact_provider_wire(character):
    windows = tuple(window(i, i + 900) for i in range(0, 24 * 900, 900))
    raw = [
        row(i + j, i + j + 1, "Opening " + character * 250 + " closing")
        for i in range(0, 24 * 900, 900)
        for j in range(40)
    ]
    ask = ModelDouble({"spans": []})
    result = segment_course_blocks(ask, windows, raw)
    assert result.unclassified_rows == len(raw)
    [(_, data)] = ask.calls
    assert len(data["evidence"]) == 8 * 24 and data["row_count"] == 40 * 24
    assert len(json.dumps(data, ensure_ascii=False)) < 100_000
    request = CallRequest(
        call_id=new_id(),
        module="extraction",
        model="synthetic",
        purpose="course_blocks",
        root_feedback_id=new_id(),
        depth=0,
        system=COURSE_BLOCK_SYSTEM,
        data=data,
        output_schema=_BlockResult.model_json_schema(),
    )
    _, body = OpenAICompatibleProvider._prepare(request, "synthetic", 60)
    assert len(body) < 160_000


def test_planner_generates_one_note_per_final_span_with_all_assigned_raw_rows():
    windows, raw, first, second = two_classes()
    hidden = [row(i, i + 1, "Implicitly assigned proof details") for i in range(40, 65)]
    unknown = row(1790, 1792, "Indistinct later talk")
    rule = new_id()
    # Adding 25 rows shifts the coordinates of the four previously named rows.
    first |= {"last": 27, "last_evidence": cite(27, "concludes today's proof lecture")}
    second |= {
        "first": 28,
        "last": 29,
        "first_evidence": cite(28, "history of art"),
        "last_evidence": cite(29, "a fresco"),
        "split_before": {
            "left": cite(27, "concludes today's proof lecture"),
            "right": cite(28, "Welcome"),
        },
    }
    client = seed([*raw, *hidden, unknown], rule)
    ask = ModelDouble(
        {"spans": [first, second]},
        {"body_md": "# Math notes", "evidence_ids": [hidden[10].id]},
        {"body_md": "# Art notes", "evidence_ids": [raw[3].id]},
    )
    with client.snapshot() as reader:
        ops, detail = extract_plan(
            reader,
            Command(kind="extract", interval=window(0, 1800)),
            new_id(),
            ask,
            transcript_only=True,
            transcript_summary_rule_id=rule,
            session_windows=windows,
            course_blocks=True,
        )
    assert [p for p, _ in ask.calls] == ["course_blocks", "generation", "generation"]
    assert detail["unclassified_rows"] == 1 and detail["activities"] == 2
    assert unknown.id not in repr(ask.calls[1:])
    assert [r["id"] for r in ask.calls[1][1]["evidence"]] == [
        r.id for r in [raw[0], *hidden, raw[1]]
    ]
    assert [r["id"] for r in ask.calls[2][1]["evidence"]] == [r.id for r in raw[2:]]
    assert [op.data_type for op in ops if isinstance(op, Create)] == [
        "activity",
        "rule_selection",
        "artifact",
        "activity",
        "rule_selection",
        "artifact",
    ]


def test_planner_blocks_if_selected_window_missing_without_generating_a_note():
    windows = (window(0, 60), window(60, 120))
    rule = new_id()
    client = seed([row(10, 12, "Start course"), row(65, 70, " \n")], rule)
    ask = ModelDouble({"spans": [span(1, 1, "Start", "course")]})
    with (
        client.snapshot() as reader,
        pytest.raises(SessionCoverageBlocked, match="without_transcript"),
    ):
        extract_plan(
            reader,
            Command(kind="extract", interval=window(0, 120)),
            new_id(),
            ask,
            transcript_only=True,
            transcript_summary_rule_id=rule,
            session_windows=windows,
            course_blocks=True,
        )
    assert [p for p, _ in ask.calls] == ["course_blocks"]


def seed(raw: list[RawRecord], rule: str):
    store = MemoryStorage(fixture_registry())
    store.client("adapter").write(
        [
            AppendRaw(NewRawRecord.model_validate(r.model_dump(exclude={"version", "ingested_at"})))
            for r in raw
        ],
        Context.system(),
    ).check()
    client = store.client("extraction")
    client.write(
        [Create("rule", rule, Rule(scope="Course", instruction="Take notes").model_dump())],
        Context.system(),
    ).check()
    return client
