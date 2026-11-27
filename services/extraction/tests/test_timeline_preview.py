from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ear_extraction.domain import Interval
from ear_extraction.timeline_preview import (
    HostedWindowReview,
    Timeline,
    TimelineReviewPlan,
    Transcript,
    WindowReview,
    assemble_timeline,
    plan_timeline,
    unsafe_review_rows,
)
from ear_llm_gateway.output_json import OutputSchema
from pydantic import ValidationError

BASE = datetime(2020, 1, 1, tzinfo=UTC)


def at(s: float) -> datetime:
    return BASE + timedelta(seconds=s)


def window(a: float, b: float) -> Interval:
    return Interval(start=at(a), end=at(b))


def row(a: float, b: float, text: str) -> Transcript:
    return Transcript(start=at(a), end=at(b), text=text)


def review(
    view: dict[str, Any],
    *ranges: tuple[int, int, str],
    continuity: tuple[int, ...] = (),
    cuts: tuple[int, ...] = (),
) -> WindowReview:
    evidence = {item["i"]: item["text"] for item in view["rows"]}
    anchor = lambda i: {"i": i, "quote": evidence[i][:24]}  # noqa: E731
    return WindowReview.model_validate(
        {
            "digest": view["digest"],
            "spans": [
                {
                    "first": a,
                    "last": b,
                    "engagement": label,
                    "first_evidence": anchor(a),
                    "last_evidence": anchor(b),
                    "engagement_evidence": None if label == "unknown" else anchor(a),
                    "transition_before": {"left": anchor(a - 1), "right": anchor(a)}
                    if a - 1 in cuts
                    else None,
                    "continuity": [
                        {"left": anchor(i), "right": anchor(i + 1)}
                        for i in continuity
                        if a <= i < b
                    ],
                }
                for a, b, label in ranges
            ],
        }
    )


def test_game_bridged_with_shared_original_rows_at_recording_and_compute_seams():
    rows = tuple(row(i * 2, i * 2 + 1, f"操作{i}：继续排查合成工厂的传送装置") for i in range(150))
    t = Timeline(
        scope=window(0, 300),
        coverage=(window(0, 156), window(156, 300)),
        transcripts=rows,
        withheld=(),
    )
    left, right = TimelineReviewPlan(t).windows
    # Digests from the pre-fixed-time planner: existing offline reviews still bind.
    assert [left["digest"], right["digest"]] == [
        "sha256:d0dfc351ccb2ca2ada1a11719e210dcd9bee5c61cc402404b0122f87732f3467",
        "sha256:0e5da51fa17fe1a3b6abe99f30a6cf7b5e459a7b89a776d8cdd2fea3db19e4ff",
    ]
    assert left["core"] == [1, 96] and right["core"] == [97, 150]
    assert left["review_edges"] == right["review_edges"] == [78, 96]
    assert left["rows"][72:] == right["rows"][:48]
    assert set(left["rows"][0]) == {"i", "start", "end", "text", "clipped"}

    # Independently reviewed ranges cover parts of the shared overlap. After agreeing
    # on continuity, the two views describe one session with no deleted/duplicated rows.
    replies = [
        review(left, (1, 110, "participation"), continuity=(78, 96)),
        review(right, (77, 150, "participation"), continuity=(78, 96)),
    ]
    out = assemble_timeline(t, replies[::-1])
    assert out["joined_review_edges_after_row"] == [78, 96]
    assert out["unclassified_rows"] == 0 and not out["unresolved_edges"]
    assert [(x["first"], x["last"], x["engagement"]) for x in out["candidates"]] == [
        (1, 150, "participation")
    ]
    assert "操作" not in str(out)
    assert out["preview_only"] is True

    # A span alone or matching role/subject is not a citable bridge.
    no_proof = assemble_timeline(t, [review(left, (1, 110, "participation")), replies[1]])
    assert no_proof["joined_review_edges_after_row"] == [78, 96]  # evidence from right view
    unproven = assemble_timeline(
        t, [review(left, (1, 110, "participation")), review(right, (77, 150, "participation"))]
    )
    assert not unproven["joined_review_edges_after_row"]
    assert [edge["after_row"] for edge in unproven["unresolved_edges"]] == [78, 96]
    assert not unproven["transition_proposals_after_row"]


def test_transcript_continues_but_source_is_a_separate_judgment():
    t = Timeline(
        scope=window(0, 20),
        coverage=(window(0, 20),),
        withheld=(),
        transcripts=(
            row(0, 1, "剧情里的角色即将演唱"),
            row(2, 3, "下一句仍是同一场剧情对白"),
            row(4, 5, "现在进入本期的芯片介绍"),
            row(6, 7, "视频中的演示展示缓存结构"),
            row(8, 9, "我正在听这句话，先暂停记下来"),
            row(10, 11, "我在做实验，旁边的视频只是背景"),
        ),
    )
    [v] = plan_timeline(t)["windows"]
    result = assemble_timeline(
        t,
        [
            review(
                v,
                (1, 2, "unknown"),
                (3, 4, "unknown"),
                (5, 5, "watching_listening"),
                (6, 6, "background_playback"),
                cuts=(2,),
            )
        ],
    )
    assert [(s["first"], s["last"], s["engagement"]) for s in result["candidates"]] == [
        (1, 2, "unknown"),
        (3, 4, "unknown"),
        (5, 5, "watching_listening"),
        (6, 6, "background_playback"),
    ]
    assert result["transition_proposals_after_row"] == [2]
    assert [u["after_row"] for u in result["unresolved_edges"]] == [4, 5]


def test_blank_clips_missing_coverage_and_withheld_are_not_a_break_or_a_join():
    t = Timeline(
        scope=window(0, 70),
        coverage=(window(0, 10), window(10, 20), window(20, 50), window(60, 70)),
        withheld=(window(30, 40),),
        transcripts=(
            row(1, 2, "做同一项操作"),
            row(12, 13, "  "),
            row(22, 23, "做同一项操作"),
            row(42, 43, "做同一项操作"),
            row(62, 63, "做同一项操作"),
        ),
    )
    windows = plan_timeline(t)["windows"]
    assert [v["core"] for v in windows] == [[1, 1], [2, 2], [3, 3], [4, 4]]
    out = assemble_timeline(t, [review(v, (*v["core"], "unknown")) for v in windows])
    assert [x["reason"] for x in out["coverage_issues"]] == [
        "no_usable_transcript",
        "withheld",
        "not_available",
    ]
    assert not out["unresolved_edges"] and not out["transition_proposals_after_row"]
    assert out["observed_blocks"] == [[1, 1], [2, 2], [3, 3], [4, 4]]
    assert out["blank_rows"] == 1
    bad = t.model_copy(
        update={
            "transcripts": (*t.transcripts[:3], row(29, 31, "NEVER EXPOSE"), *t.transcripts[3:])
        }
    )
    with pytest.raises(ValueError, match="outside_available_coverage"):
        plan_timeline(bad)


def test_conflicting_role_or_boundary_does_not_become_a_confirmed_merge():
    t = Timeline(
        scope=window(0, 150),
        coverage=(window(0, 150),),
        withheld=(),
        transcripts=tuple(row(i, i + 1, f"活动证据{i}") for i in range(150)),
    )
    left, right = plan_timeline(t)["windows"]
    a = review(left, (1, 115, "participation"), continuity=(96,))
    b = review(right, (90, 96, "watching_listening"), (97, 150, "participation"), cuts=(96,))
    out = assemble_timeline(t, [a, b])
    assert out["role_conflict_rows"] == list(range(90, 97))
    assert not out["joined_review_edges_after_row"] and not out["transition_proposals_after_row"]
    assert any(
        u["after_row"] == 96 and u["reason"] == "conflicting_reviews"
        for u in out["unresolved_edges"]
    )
    assert all(x["engagement"] == "unknown" for x in out["candidates"] if 90 <= x["first"] <= 96)


def test_tails_and_unassigned_rows_are_never_inferred_or_filled():
    t = Timeline(
        scope=window(0, 20),
        coverage=(window(0, 20),),
        withheld=(),
        transcripts=(
            row(0, 4, "片段的介绍" + "甲" * 200 + "未呈现的末尾"),
            row(2, 3, "来源暂时不明"),
            row(3, 5, "下一段的可见内容"),
        ),
    )
    [v] = plan_timeline(t)["windows"]
    assert v["rows"][0]["clipped"]
    a = review(v, (1, 1, "unknown"), (3, 3, "unknown"))
    out = assemble_timeline(t, [a])
    assert out["unclassified_rows"] == 1
    assert out["time_overlap_candidate_indices"] == [2]
    with pytest.raises(ValueError, match="reply_set_mismatch"):
        assemble_timeline(t, [a, a])
    stale = t.model_copy(update={"transcripts": (row(0, 4, "文本变动"), *t.transcripts[1:])})
    with pytest.raises(ValueError, match="reply_set_mismatch"):
        assemble_timeline(stale, [a])
    payload = a.model_dump()
    payload["spans"][0]["first_evidence"]["quote"] = "未呈现的末尾"
    with pytest.raises(ValueError, match="citation_not_shown"):
        assemble_timeline(t, [WindowReview.model_validate(payload)])
    payload = a.model_dump()
    payload["spans"][0]["engagement"] = "participation"
    with pytest.raises(ValueError, match="engagement_requires_citation"):
        assemble_timeline(t, [WindowReview.model_validate(payload)])


def test_all_unclassified_and_missing_are_distinct():
    t = Timeline(
        scope=window(0, 2),
        coverage=(window(0, 2),),
        withheld=(),
        transcripts=(row(0, 1, "只录到几个零碎词"),),
    )
    [v] = plan_timeline(t)["windows"]
    assert assemble_timeline(t, [review(v)])["unclassified_rows"] == 1
    empty = t.model_copy(update={"transcripts": ()})
    assert assemble_timeline(empty, [])["coverage_issues"][0]["reason"] == "no_usable_transcript"


def test_long_internal_no_text_interval_kept_separate_without_claiming_a_change():
    t = Timeline(
        scope=window(0, 900),
        coverage=(window(0, 900),),
        withheld=(),
        transcripts=(
            row(10, 20, "正在调整测试装置"),
            row(12, 50, "重叠的第二轨转写"),
            row(390, 400, "继续调整测试装置"),
        ),
    )
    a, b = plan_timeline(t)["windows"]
    out = assemble_timeline(t, [review(a, (1, 2, "unknown")), review(b, (3, 3, "unknown"))])
    assert out["observed_blocks"] == [[1, 2], [3, 3]]
    assert out["coverage_issues"] == [
        {
            "start": "2020-01-01T00:00:50Z",
            "end": "2020-01-01T00:06:30Z",
            "reason": "no_usable_transcript",
        },
        {
            "start": "2020-01-01T00:06:40Z",
            "end": "2020-01-01T00:15:00Z",
            "reason": "no_usable_transcript",
        },
    ]
    assert not out["transition_proposals_after_row"]


def test_preview_budget_handles_full_day_and_never_includes_withheld_or_metadata():
    t = Timeline(
        scope=window(0, 86400),
        coverage=tuple(window(i, i + 600) for i in range(0, 86400, 600)),
        withheld=(window(21_600, 28_800),),
        transcripts=tuple(
            row(i, i + 0.4, "合成测试转写") for i in range(0, 86400, 6) if not 21_600 <= i < 28_800
        ),
    )
    plan = plan_timeline(t)
    result = assemble_timeline(t, [review(w) for w in plan["windows"]])
    assert sum(w["core"][1] - w["core"][0] + 1 for w in plan["windows"]) == len(t.transcripts)
    assert result["unclassified_rows"] == len(t.transcripts) == 13200
    assert len(plan["windows"]) <= 256
    assert all(len(w["rows"]) <= 144 for w in plan["windows"])
    assert (
        len(result["coverage_issues"]) == 1 and result["coverage_issues"][0]["reason"] == "withheld"
    )
    assert not result["candidates"]


def test_cross_clip_row_preserved_once_and_transition_inside_overlap_unresolved():
    t = Timeline(
        scope=window(0, 4),
        coverage=(window(0, 2), window(2, 4)),
        withheld=(),
        transcripts=(row(1, 3, "第一段仍然在进行"), row(2, 4, "另一来源开始说话")),
    )
    [v] = plan_timeline(t)["windows"]
    assert [x["i"] for x in v["rows"]] == [1, 2] and v["review_edges"] == [1]
    out = assemble_timeline(t, [review(v, (1, 1, "participation"), (2, 2, "unknown"), cuts=(1,))])
    assert out["unresolved_edges"][0]["reason"] == "overlapping_timestamps"
    assert not out["transition_proposals_after_row"]
    assert out["time_overlap_candidate_indices"] == [2]


def test_contextual_continuity_reviews_an_explicit_edge_with_uninformative_edge_rows():
    t = Timeline(
        scope=window(0, 120),
        coverage=(window(0, 60), window(60, 120)),
        transcripts=(
            row(45, 46, "检查给工业泵供电的继电器"),
            row(59, 59.5, "嗯"),
            row(60, 61, "啊"),
            row(75, 76, "继电器接通后工业泵有电了"),
        ),
        withheld=(),
    )
    [v] = plan_timeline(t)["windows"]
    r = review(v, (1, 4, "unknown")).model_dump()
    claim = {
        "after_row": 2,
        "left": {"i": 1, "quote": "给工业泵供电"},
        "right": {"i": 4, "quote": "工业泵有电了"},
    }
    r["spans"][0]["continuity"] = [claim]
    out = assemble_timeline(t, [WindowReview.model_validate(r)])
    assert out["joined_review_edges_after_row"] == [2]
    assert len(out["candidates"]) == 1
    claim["left"] = {"i": 3, "quote": "啊"}
    with pytest.raises(ValueError, match="timeline_invalid_citation"):
        assemble_timeline(t, [WindowReview.model_validate(r)])


def test_contextual_continuity_cannot_quote_across_the_review_window_or_hide_a_long_pause():
    t = Timeline(
        scope=window(0, 401),
        coverage=(window(0, 200), window(200, 401)),
        transcripts=(row(0, 1, "早先测试"), row(199, 200, "这一项"), row(201, 202, "后来测试")),
        withheld=(),
    )
    [v] = plan_timeline(t)["windows"]
    r = review(v, (1, 3, "unknown")).model_dump()
    r["spans"][0]["continuity"] = [
        {
            "after_row": 2,
            "left": {"i": 1, "quote": "早先测试"},
            "right": {"i": 3, "quote": "后来测试"},
        }
    ]
    with pytest.raises(ValueError, match="timeline_continuity_quote_too_far"):
        assemble_timeline(t, [WindowReview.model_validate(r)])


def test_partial_reviews_separate_missing_work_from_explicitly_unclassified():
    t = Timeline(
        scope=window(0, 150),
        coverage=(window(0, 150),),
        withheld=(),
        transcripts=tuple(row(i, i + 1, f"活动证据{i}") for i in range(150)),
    )
    left, right = plan_timeline(t)["windows"]
    r = review(left, (90, 102, "unknown"), continuity=(96,))
    out = assemble_timeline(t, [r], allow_partial=True)
    assert out["completed_review_windows"] == 1 and out["unreviewed_core_ranges"] == [[97, 150]]
    assert out["unreviewed_core_rows"] == 54 and out["unreviewed_unlabeled_rows"] == 48
    assert out["unclassified_rows"] == 89
    with pytest.raises(ValueError, match="timeline_reply_set_mismatch"):
        assemble_timeline(t, [r])
    incomplete = r.model_copy(update={"digest": "sha256:" + "3" * 64})
    with pytest.raises(ValueError, match="timeline_reply_set_mismatch"):
        assemble_timeline(t, [incomplete, review(right)], allow_partial=True)


def test_hosted_review_v2_has_a_closed_wire_and_offline_schema_still_checks_anchors():
    t = Timeline(
        scope=window(0, 60),
        coverage=(window(0, 60),),
        transcripts=(row(1, 2, "我正在排查连接"),),
        withheld=(),
    )
    plan = TimelineReviewPlan(t)
    hosted = plan.hosted_window(0)
    assert "digest" not in hosted
    wire = {
        "spans": [
            {
                "first": 1,
                "last": 1,
                "engagement": "participation",
                "engagement_evidence": {"i": 1, "quote": "排查连接"},
            }
        ]
    }
    schema = OutputSchema(HostedWindowReview.model_json_schema())
    schema.validate(wire)
    parsed = HostedWindowReview.model_validate(wire)
    admitted = plan.admit_hosted_window(0, parsed)
    offline = review(plan.windows[0], (1, 1, "participation"))
    assert plan.assemble_hosted([admitted]) == assemble_timeline(t, [offline])
    for extra in ({"digest": plan.windows[0]["digest"]}, {"call_id": "untrusted"}):
        with pytest.raises(ValueError, match="output_schema_mismatch"):
            schema.validate(wire | extra)
    for extra in (
        {"first_evidence": {"i": 1, "quote": "排查连接"}},
        {"first": True},
        {"engagement_evidence": {"i": 1, "quote": "号" * 65}},
    ):
        with pytest.raises(ValueError, match="output_schema_mismatch"):
            schema.validate({"spans": [wire["spans"][0] | extra]})
    # Offline still requires the digest and anchors; they are not synthesized for v2.
    with pytest.raises(ValidationError):
        WindowReview.model_validate(wire | {"digest": plan.windows[0]["digest"]})
    with pytest.raises(ValueError):
        plan.assemble_hosted([])


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("time", "timeline_continuity_quote_too_far"),
        ("side", "timeline_invalid_citation"),
        ("transition", "timeline_transition_requires_two_adjacent_spans"),
    ],
)
def test_hosted_review_v2_discards_invalid_optional_proofs_but_offline_rejects(problem, error):
    t = Timeline(
        scope=window(0, 400),
        coverage=(window(0, 300), window(300, 400)),
        transcripts=(row(1, 2, "举例"), row(240, 241, "操作中"), row(301, 302, "继续操作")),
        withheld=(),
    )
    plan = TimelineReviewPlan(t)
    assert plan.hosted_window(0)["review_edges"] == [2]
    left = {"i": 3, "quote": "继续"} if problem == "side" else {"i": 1, "quote": "举例"}
    right = {"i": 3, "quote": "继续"}
    if problem == "transition":
        spans = [
            {"first": 1, "last": 1, "engagement": "unknown"},
            {
                "first": 3,
                "last": 3,
                "engagement": "unknown",
                "transition_before": {"left": left, "right": right},
            },
        ]
    else:
        spans = [
            {
                "first": 1,
                "last": 3,
                "engagement": "participation",
                "engagement_evidence": {"i": 2, "quote": "操作"},
                "continuity": [{"after_row": 2, "left": left, "right": right}],
            }
        ]
    wire = HostedWindowReview.model_validate({"spans": spans})
    admitted = plan.admit_hosted_window(0, wire)
    out = plan.assemble_hosted([admitted])
    assert not out["transition_proposals_after_row"] and not out["joined_review_edges_after_row"]
    if problem == "transition":
        assert admitted.discarded_transitions == 1
        assert admitted.reply.spans[1].transition_before is None
        assert not unsafe_review_rows(out)  # no contrary shared span at the dropped cut
    else:
        assert admitted.discarded_bridges == 1
        assert admitted.reply.spans[0].continuity == ()
        assert out["unresolved_edges"] == [
            {"after_row": 2, "before_row": 3, "reason": "invalid_hosted_proof"}
        ]
        assert unsafe_review_rows(out) == {2, 3}
    offline = review(plan.windows[0], *((s.first, s.last, s.engagement) for s in wire.spans))
    offline = WindowReview.model_validate(
        {
            "digest": offline.digest,
            "spans": [
                strict.model_dump() | original
                for strict, original in zip(offline.spans, spans, strict=True)
            ],
        }
    )
    with pytest.raises(ValueError, match=error):
        assemble_timeline(t, [offline])


@pytest.mark.parametrize(
    "problem",
    [
        "cut_at_view_start",
        "cut_quote_pair",
        "bridge",
        "duplicate_bridge",
        "wrong_bridge_edge",
        "bridge_at_view_start",
        "role",
    ],
)
def test_hosted_v2_degradation_cannot_be_voted_away_at_shared_edges(problem):
    t = Timeline(
        scope=window(0, 110),
        coverage=(window(0, 72), window(72, 110)),
        transcripts=tuple(row(i, i + 1, f"evidence {i + 1}") for i in range(110)),
        withheld=(),
    )
    plan = TimelineReviewPlan(t)
    assert plan.hosted_window(1)["rows"][0]["i"] == 73

    def anchor(i: int) -> dict[str, Any]:
        return {"i": i, "quote": f"evidence {i}"}

    def span(first: int, last: int, **proofs: Any) -> dict[str, Any]:
        return {
            "first": first,
            "last": last,
            "engagement": "participation",
            "engagement_evidence": anchor(first),
            **proofs,
        }

    def admit(i: int, *spans: dict[str, Any]):
        return plan.admit_hosted_window(i, HostedWindowReview.model_validate({"spans": spans}))

    proof = {"after_row": 96, "left": anchor(96), "right": anchor(97)}
    entrance = {"after_row": 72, "left": anchor(72), "right": anchor(73)}
    left = admit(0, span(72, 99, continuity=[entrance, proof]))
    right = admit(1, span(73, 99, continuity=[proof]))
    assert plan.assemble_hosted([left, right])["joined_review_edges_after_row"] == [72, 96]
    if problem == "cut_at_view_start":
        right = admit(1, span(73, 99, transition_before={"left": anchor(72), "right": anchor(73)}))
        edge = 72
    elif problem == "cut_quote_pair":
        right = admit(1, span(99, 99, transition_before={"left": anchor(96), "right": anchor(97)}))
        edge = 96
    elif problem == "role":
        right = admit(1, span(73, 99, engagement_evidence=anchor(100)))
        assert right.downgraded_engagements == 1
        assert right.reply.spans[0].engagement == "unknown"
        assert right.reply.spans[0].engagement_evidence is None
        candidates = plan.assemble_hosted([left, right])["candidates"]
        assert all(c["engagement"] == "unknown" for c in candidates if c["last"] >= 73)
        return
    else:
        bridges = {
            "duplicate_bridge": [proof, proof],
            "wrong_bridge_edge": [{"after_row": 111, "left": anchor(95), "right": anchor(98)}],
            "bridge_at_view_start": [entrance],
        }.get(problem, [proof | {"right": {"i": 97, "quote": "invented"}}])
        right = admit(1, span(73, 99, continuity=bridges))
        assert right.discarded_bridges == len(bridges)
        assert right.reply.spans[0].continuity == ()
        edge = 72 if problem == "bridge_at_view_start" else 96
    out = plan.assemble_hosted([left, right])
    assert edge not in out["joined_review_edges_after_row"]
    assert edge not in out["transition_proposals_after_row"]
    assert {edge, edge + 1} <= unsafe_review_rows(out)
    assert all(not c["first"] <= edge < c["last"] for c in out["candidates"])


def test_fixed_time_dense_views_preserve_full_padding_and_require_seam_proofs():
    t = Timeline(
        scope=window(125, 1700),
        coverage=(window(125, 540), window(540, 1200), window(1200, 1700)),
        transcripts=tuple(row(i, i + 1, f"同一操作{i}") for i in range(126, 1700, 2)),
        withheld=(),
    )
    plan = TimelineReviewPlan(t, fixed_time=True)
    assert TimelineReviewPlan(t).windows == plan_timeline(t)["windows"]
    assert TimelineReviewPlan(t, fixed_time=False).windows == TimelineReviewPlan(t).windows
    assert [v["core_time"] for v in plan.windows] == [
        window(a, b).model_dump(mode="json") for a, b in ((125, 600), (600, 1200), (1200, 1700))
    ]
    assert [v["core"] for v in plan.windows] == [[1, 237], [238, 537], [538, 787]]
    assert [v["review_edges"] for v in plan.windows] == [[207, 237], [207, 237, 537], [537]]
    for index, (v, a, b) in enumerate(
        zip(plan.windows, (125, 480, 1080), (720, 1320, 1700), strict=True)
    ):
        indices = [i for i, r in enumerate(t.transcripts, 1) if at(a) <= r.start < at(b)]
        assert [r["i"] for r in v["rows"]] == indices
        assert plan.hosted_window(index) == {k: x for k, x in v.items() if k != "digest"}
    left, middle, right = plan.windows
    assert len(middle["rows"]) == 420  # 300 core, plus 60 on each side.
    assert left["rows"][-120:] == middle["rows"][:120]
    assert middle["rows"][-120:] == right["rows"][:120]

    def all_rows(v):
        return (v["rows"][0]["i"], v["rows"][-1]["i"], "unknown")

    unproved = plan.assemble([review(v, all_rows(v)) for v in plan.windows])
    assert [e["after_row"] for e in unproved["unresolved_edges"]] == [207, 237, 537]
    replies = [review(v, all_rows(v), continuity=tuple(v["review_edges"])) for v in plan.windows]
    result = plan.assemble(replies)
    assert result["joined_review_edges_after_row"] == [207, 237, 537]
    assert not result["unresolved_edges"]
    assert [(c["first"], c["last"]) for c in result["candidates"]] == [(1, 787)]

    # View context is not quote distance: this visible citation is within two
    # minutes of the compute edge, but more than 24 rows to its left.
    reply = review(middle, all_rows(middle)).model_dump()
    reply["spans"][0]["continuity"] = [
        {
            "after_row": 237,
            "left": {"i": 213, "quote": "同一操作"},
            "right": {"i": 238, "quote": "同一操作"},
        }
    ]
    with pytest.raises(ValueError, match="timeline_invalid_citation"):
        plan.validate_window(1, WindowReview.model_validate(reply))


def test_fixed_time_half_open_context_and_core_follow_starts_even_for_straddlers():
    t = Timeline(
        scope=window(-1, 1201),
        coverage=(window(-1, 1201),),
        transcripts=(
            row(-0.000001, 0.5, "before clock boundary"),
            row(0, 1, "at clock boundary"),
            row(479.999999, 481, "starts before left context"),
            row(480, 481, "starts exactly at left context"),
            row(599.999999, 602, "last start in preceding core"),
            row(600, 601, "first start in next core"),
            row(600, 602, "another row with the same start"),
            row(719.999999, 722, "last start in right context"),
            row(720, 721, "exclusive right context end"),
            row(1200, 1201, "first start in final core"),
        ),
        withheld=(),
    )
    previous, left, right, final = TimelineReviewPlan(t, fixed_time=True).windows
    assert [v["core"] for v in (previous, left, right, final)] == [[1, 1], [2, 5], [6, 9], [10, 10]]
    assert [r["i"] for r in left["rows"]] == list(range(1, 9))
    assert [r["i"] for r in right["rows"]] == list(range(4, 11))
    assert right["rows"][1]["end"] == "2020-01-01T00:10:02Z"  # Never split the row.


def test_fixed_time_skips_context_only_buckets_without_inventing_coverage_gaps():
    t = Timeline(
        scope=window(5, 2400),
        coverage=(window(5, 2400),),
        transcripts=(
            row(595, 596, "first"),
            row(1201, 1203, "later"),
            row(1790, 1794, "near seam"),
            row(1801, 1805, "past seam"),
        ),
        withheld=(),
    )
    plan = TimelineReviewPlan(t, fixed_time=True)
    assert [v["core"] for v in plan.windows] == [[1, 1], [2, 3], [4, 4]]
    assert [[r["i"] for r in v["rows"]] for v in plan.windows] == [[1], [2, 3, 4], [3, 4]]
    replies = [review(v, (*v["core"], "unknown")) for v in plan.windows]
    result = plan.assemble(replies)
    assert result["coverage_issues"] == []
    assert result["observed_blocks"] == [[1, 4]]
    assert [(s["first"], s["last"]) for s in result["candidates"]] == [(1, 1), (2, 3), (4, 4)]
    assert not result["joined_review_edges_after_row"]
    assert not result["transition_proposals_after_row"]
    assert plan_timeline(t)["coverage_issues"]  # Offline retains its pause-based policy.


def test_fixed_time_never_crosses_unavailable_coverage_even_in_same_bucket():
    t = Timeline(
        scope=window(410, 740),
        coverage=(window(420, 560), window(565, 625), window(625, 630), window(630, 720)),
        transcripts=tuple(
            row(a, b, "same words")
            for a, b in (
                (559, 560),
                (565, 566),
                (599, 601),
                (600, 601),
                (624, 625),
                (630, 631),
                (644, 645),
                (650, 651),
            )
        ),
        withheld=(window(645, 650),),
    )
    plan = TimelineReviewPlan(t, fixed_time=True)
    assert [v["core"] for v in plan.windows] == [[1, 1], [2, 3], [4, 5], [6, 7], [8, 8]]
    assert [[r["i"] for r in v["rows"]] for v in plan.windows] == [
        [1],
        [2, 3, 4, 5],
        [2, 3, 4, 5],
        [6, 7],
        [8],
    ]
    assert [v["review_edges"] for v in plan.windows] == [[], [3], [3], [], []]
    result = plan.assemble([review(v, (*v["core"], "unknown")) for v in plan.windows])
    assert result["observed_blocks"] == [[1, 1], [2, 5], [6, 7], [8, 8]]
    assert result["coverage_issues"] == [
        {**window(a, b).model_dump(mode="json"), "reason": reason}
        for a, b, reason in (
            (410, 420, "not_available"),
            (560, 565, "not_available"),
            (625, 630, "no_usable_transcript"),
            (645, 650, "withheld"),
            (720, 740, "not_available"),
        )
    ]
    assert [e["after_row"] for e in result["unresolved_edges"]] == [3]
    assert not result["transition_proposals_after_row"]
    bad = t.model_copy(update={"transcripts": (row(559, 566, "invalid"), *t.transcripts[1:])})
    with pytest.raises(ValueError, match="timeline_transcript_outside_available_coverage"):
        TimelineReviewPlan(bad, fixed_time=True)
    empty = t.model_copy(update={"transcripts": ()})
    assert not TimelineReviewPlan(empty, fixed_time=True).windows


def test_fixed_time_dense_hosted_view_can_admit_more_than_legacy_144_spans():
    t = Timeline(
        scope=window(0, 600),
        coverage=(window(0, 600),),
        transcripts=tuple(row(i, i + 1, "evidence") for i in range(512)),
        withheld=(),
    )
    plan = TimelineReviewPlan(t, fixed_time=True)
    assert plan.windows[0]["core"] == [1, 512]
    data = {"spans": [{"first": i, "last": i, "engagement": "unknown"} for i in range(1, 513)]}
    OutputSchema(HostedWindowReview.model_json_schema()).validate(data)
    reply = plan.admit_hosted_window(0, HostedWindowReview.model_validate(data))
    result = plan.assemble_hosted([reply])
    assert result["unclassified_rows"] == 0
    assert [c["first"] for c in result["candidates"]] == list(range(1, 513))


@pytest.mark.parametrize(
    ("length", "text", "error"),
    [(513, "short", "too_many_rows"), (180, "测" * 192, "too_large")],
)
def test_fixed_time_oversized_later_view_rejects_plan_instead_of_trimming(length, text, error):
    t = Timeline(
        scope=window(0, 1200),
        coverage=(window(0, 1200),),
        transcripts=(
            row(1, 2, "earlier core fits"),
            *(row(730 + i / 2, 730 + (i + 1) / 2, text) for i in range(length)),
        ),
        withheld=(),
    )
    with pytest.raises(ValueError, match=f"timeline_fixed_view_{error}"):
        TimelineReviewPlan(t, fixed_time=True)
    assert (
        sum(b - a + 1 for a, b in (v["core"] for v in TimelineReviewPlan(t).windows)) == length + 1
    )


def test_fixed_time_keeps_total_view_cap_across_coverage_components():
    t = Timeline(
        scope=window(0, 514),
        coverage=tuple(window(i, i + 1) for i in range(0, 514, 2)),
        transcripts=tuple(row(i, i + 1, "evidence") for i in range(0, 514, 2)),
        withheld=(),
    )
    with pytest.raises(ValueError, match="timeline_too_many_review_windows"):
        TimelineReviewPlan(t, fixed_time=True)


def test_fixed_time_row_cap_counts_context_as_well_as_core():
    t = Timeline(
        scope=window(0, 1200),
        coverage=(window(0, 1200),),
        transcripts=tuple(row(i, i + 0.5, "short") for i in (*range(480, 600), *range(730, 1123))),
        withheld=(),
    )
    # Neither core is oversized, but the second view's left context adds 120 rows.
    with pytest.raises(ValueError, match="timeline_fixed_view_too_many_rows"):
        TimelineReviewPlan(t, fixed_time=True)
