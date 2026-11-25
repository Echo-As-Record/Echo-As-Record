"""Synthetic integration tests of current attribution and plugin boundaries."""

from datetime import timedelta

import pytest
from ear_contracts import DEFAULT_REGISTRY, SemanticRecord, new_id
from ear_habit.rebuild import rebuild_day_ops
from ear_habit.recognition_context import recognition_context
from ear_habit.service import PersonMemoryService
from ear_habit.social import activity_observation
from ear_habit.store import extend_registry, find_bucket, rollup_ops
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Update, write_with_retry
from ear_storage_client.testing import make_raw, ts


def fixtures():
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY))
    ctx = Context.system()
    person_a, person_b, voice, segment, unknown, activity, audio = (new_id() for _ in range(7))
    attr = new_id()
    owner = storage.client("attribution")
    owner.write(
        [Create("person", person_a, {"name": "A"}), Create("person", person_b, {"name": "B"})], ctx
    ).check()
    raw = [
        make_raw("voiceprint", {}, start="2026-11-23T10:00:00Z", id=voice),
        make_raw(
            "transcript_segment",
            {
                "text": "我觉得很好",
                "voiceprint_id": voice,
                "local_speaker_id": "s1",
                "audio_ref_id": audio,
            },
            start="2026-11-23T10:01:00Z",
            end="2026-11-23T10:01:05Z",
            id=segment,
        ),
        make_raw(
            "transcript_segment",
            {
                "text": "你好",
                "voiceprint_id": None,
                "local_speaker_id": "s2",
                "audio_ref_id": audio,
            },
            start="2026-11-23T10:02:00Z",
            end="2026-11-23T10:02:05Z",
            id=unknown,
        ),
    ]
    storage.client("adapter").write([AppendRaw(x) for x in raw], ctx).check()
    owner.write(
        [
            Create(
                "attribution",
                attr,
                {"subject_id": voice, "target_id": person_a, "target_type": "person"},
            )
        ],
        ctx,
    ).check()
    storage.client("extraction").write(
        [
            Create(
                "activity",
                activity,
                {"title_md": "test"},
                event_start=ts("2026-11-23T10:00:00Z"),
                event_end=ts("2026-11-23T11:00:00Z"),
            )
        ],
        ctx,
    ).check()
    return storage, person_a, person_b, voice, segment, unknown, activity, attr


def test_rebuild_after_correction_and_week_rollup():
    storage, person_a, person_b, voice, segment, _, _, attr = fixtures()
    client, owner = storage.client("habit"), storage.client("attribution")
    write_with_retry(client, lambda r: rebuild_day_ops(r, "2026-11-23"), Context.system()).check()
    first = find_bucket(client, f"{person_a}:day:2026-11-23")
    assert first is not None and first.payload["evidence_ids"] == [segment]
    assert first.payload["stats"]["segments"] == 1
    old = owner.get(attr)
    assert isinstance(old, SemanticRecord)
    owner.write(
        [
            Update(
                "attribution",
                attr,
                old.version,
                {"subject_id": voice, "target_id": person_b, "target_type": "person"},
            )
        ],
        Context.system(),
    ).check()
    write_with_retry(client, lambda r: rebuild_day_ops(r, "2026-11-23"), Context.system()).check()
    a = find_bucket(client, f"{person_a}:day:2026-11-23")
    b = find_bucket(client, f"{person_b}:day:2026-11-23")
    assert a is not None and a.payload["stats"]["segments"] == 0
    assert b is not None and b.payload["stats"]["segments"] == 1
    assert b.payload["evidence_ids"] == [segment]
    write_with_retry(
        client, lambda r: rollup_ops(r, person_b, "week", "2026-11-23"), Context.system()
    ).check()
    week = find_bucket(client, f"{person_b}:week:2026-11-23")
    assert week is not None and week.payload["stats"]["segments"] == 1


def test_activity_reports_unresolved_without_inventing_relationships():
    storage, person_a, _, _, segment, unknown, activity, _ = fixtures()
    with storage.client("habit").snapshot() as reader:
        view = activity_observation(reader, activity)
    assert [(p.person_id, p.segment_ids) for p in view.participants] == [(person_a, (segment,))]
    assert view.unresolved_segment_ids == (unknown,)


def test_recognition_context_uses_only_prior_same_audio_and_no_person_profile():
    storage, _, _, _, segment, unknown, _, _ = fixtures()
    with storage.client("habit").snapshot() as reader:
        view = recognition_context(reader, unknown, lookback=timedelta(minutes=3))
        earlier = recognition_context(reader, segment, lookback=timedelta(minutes=3))
    assert [x.segment_id for x in view.preceding] == [segment]
    assert earlier.preceding == ()
    assert not hasattr(view, "person_id")
    with pytest.raises(ValueError, match="bounds"):
        recognition_context(storage.client("habit"), unknown, lookback=timedelta(0))


def test_rebuild_rejects_snapshot_after_new_source_arrives():
    storage, _, _, _, _, _, _, _ = fixtures()
    client = storage.client("habit")
    with client.snapshot() as reader:
        ops = rebuild_day_ops(reader, "2026-11-23")
    new_segment = make_raw(
        "transcript_segment", {"text": "later", "voiceprint_id": None}, start="2026-11-23T10:03:00Z"
    )
    storage.client("adapter").write([AppendRaw(new_segment)], Context.system()).check()
    result = client.write(ops, Context.system())
    assert result.conflict is not None and result.conflict.reason == "collection_changed"


def test_service_uses_plugin_identity():
    storage, person_a, _, _, _, _, activity, _ = fixtures()
    with pytest.raises(ValueError, match="habit"):
        PersonMemoryService(storage.client("attribution"))
    service = PersonMemoryService(storage.client("habit"))
    service.rebuild_day("2026-11-23", Context.system())
    assert service.activity(activity).participants[0].person_id == person_a


def test_social_window_reports_shared_activity_with_evidence():
    storage, a, b, _, _, _, activity, _ = fixtures()
    voice, segment = new_id(), new_id()
    raw = [
        make_raw("voiceprint", {}, start="2026-11-23T10:05:00Z", id=voice),
        make_raw(
            "transcript_segment",
            {"text": "test", "voiceprint_id": voice},
            start="2026-11-23T10:06:00Z",
            id=segment,
        ),
    ]
    storage.client("adapter").write([AppendRaw(x) for x in raw], Context.system()).check()
    storage.client("attribution").write(
        [
            Create(
                "attribution",
                new_id(),
                {"subject_id": voice, "target_id": b, "target_type": "person"},
            )
        ],
        Context.system(),
    ).check()
    service = PersonMemoryService(storage.client("habit"))
    pairs = service.social_window(ts("2026-11-23T00:00:00Z"), ts("2026-11-24T00:00:00Z"))
    assert len(pairs) == 1 and set(pairs[0].person_ids) == {a, b}
    assert pairs[0].activities[0].activity_id == activity
    assert any(
        segment in participant.segment_ids for participant in pairs[0].activities[0].participants
    )


def test_adjacent_turns_and_conditional_profile_remain_evidence_backed():
    storage, a, b, _, a_segment, _, activity, _ = fixtures()
    voice, b_segment = new_id(), new_id()
    raw = [
        make_raw("voiceprint", {}, start="2026-11-23T10:00:00Z", id=voice),
        make_raw(
            "transcript_segment",
            {"text": "我觉得可以", "voiceprint_id": voice},
            start="2026-11-23T10:01:10Z",
            end="2026-11-23T10:01:12Z",
            id=b_segment,
        ),
    ]
    storage.client("adapter").write([AppendRaw(x) for x in raw], Context.system()).check()
    storage.client("attribution").write(
        [
            Create(
                "attribution",
                new_id(),
                {"subject_id": voice, "target_id": b, "target_type": "person"},
            )
        ],
        Context.system(),
    ).check()
    service = PersonMemoryService(storage.client("habit"))
    view = service.activity(activity)
    assert len(view.adjacent_turns) == 1
    turn = view.adjacent_turns[0]
    assert (turn.first_segment_id, turn.second_segment_id, turn.gap_ms) == (
        a_segment,
        b_segment,
        5000,
    )
    profile = service.conditional_profile(
        a, b, ts("2026-11-23T00:00:00Z"), ts("2026-11-24T00:00:00Z")
    )
    assert profile.evidence_ids == (a_segment,)
    assert profile.stats["segments"] == 1
    assert profile.interpretation == "co_present_not_directed_speech"


def test_expression_candidates_require_multiple_observed_days_and_cite_buckets():
    storage, a, _, voice, _, _, _, _ = fixtures()
    second = make_raw(
        "transcript_segment",
        {"text": "我觉得不错", "voiceprint_id": voice},
        start="2026-11-24T10:00:00Z",
    )
    storage.client("adapter").write([AppendRaw(second)], Context.system()).check()
    service = PersonMemoryService(storage.client("habit"))
    service.rebuild_day("2026-11-23", Context.system())
    service.rebuild_day("2026-11-24", Context.system())
    args = (a, ts("2026-11-23T00:00:00Z"), ts("2026-11-25T00:00:00Z"))
    candidates = service.expression_candidates(*args, min_days=2, min_occurrences=2)
    match = next(c for c in candidates if c.expression == "我觉得")
    assert (match.occurrences, match.days_present, match.observed_days) == (2, 2, 2)
    assert len(match.bucket_versions) == 2
    assert service.expression_candidates(*args, min_days=3, min_occurrences=2) == ()
