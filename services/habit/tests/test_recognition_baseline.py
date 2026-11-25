"""No future labels, same recording, automatic profiles or forced identity."""

from datetime import timedelta

from ear_contracts import DEFAULT_REGISTRY, new_id
from ear_habit.recognition_baseline import confirmed_candidates
from ear_habit.store import extend_registry
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Update
from ear_storage_client.testing import make_raw, ts


def test_only_confirmed_prior_independent_references_are_candidates():
    now = [ts("2026-11-20T12:00:00Z")]
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY), clock=lambda: now[0])
    a, b, va, vb, target_voice, target_audio = (new_id() for _ in range(6))
    owner, adapter = storage.client("attribution"), storage.client("adapter")
    owner.write(
        [Create("person", a, {"name": "A"}), Create("person", b, {"name": "B"})], Context.system()
    ).check()
    for voice in (va, vb, target_voice):
        adapter.write(
            [AppendRaw(make_raw("voiceprint", {}, start="2026-11-20T10:00:00Z", id=voice))],
            Context.system(),
        ).check()
    attr_a, attr_b = new_id(), new_id()
    owner.write(
        [
            Create(
                "attribution",
                attr_a,
                {"subject_id": va, "target_type": "person", "target_id": a, "user_protected": True},
            ),
            Create(
                "attribution",
                attr_b,
                {
                    "subject_id": vb,
                    "target_type": "person",
                    "target_id": b,
                    "user_protected": False,
                },
            ),
        ],
        Context.system(),
    ).check()
    reference_a = make_raw(
        "transcript_segment",
        {"text": "电池 续航 方案", "voiceprint_id": va, "audio_ref_id": new_id()},
        start="2026-11-20T10:01:00Z",
    )
    reference_b = make_raw(
        "transcript_segment",
        {"text": "电池 续航 方案", "voiceprint_id": vb, "audio_ref_id": new_id()},
        start="2026-11-20T10:02:00Z",
    )
    same_recording = make_raw(
        "transcript_segment",
        {"text": "电池 续航 方案", "voiceprint_id": va, "audio_ref_id": target_audio},
        start="2026-11-20T10:03:00Z",
    )
    adapter.write(
        [AppendRaw(reference_a), AppendRaw(reference_b), AppendRaw(same_recording)],
        Context.system(),
    ).check()
    now[0] = ts("2026-11-21T12:00:00Z")
    target = make_raw(
        "transcript_segment",
        {"text": "电池 续航 方案", "voiceprint_id": target_voice, "audio_ref_id": target_audio},
        start="2026-11-21T10:00:00Z",
    )
    adapter.write([AppendRaw(target)], Context.system()).check()
    as_of = now[0]
    with storage.client("habit").snapshot() as reader:
        candidates = confirmed_candidates(
            reader, target.id, as_of=as_of, lookback=timedelta(days=5)
        )
    assert [candidate.person_id for candidate in candidates] == [a]
    assert [ref.segment_id for ref in candidates[0].references] == [reference_a.id]
    now[0] += timedelta(days=1)
    current = owner.get(attr_b)
    assert current is not None
    owner.write(
        [
            Update(
                "attribution", attr_b, current.version, {**current.payload, "user_protected": True}
            )
        ],
        Context.system(),
    ).check()
    with storage.client("habit").snapshot() as reader:
        prior = confirmed_candidates(reader, target.id, as_of=as_of, lookback=timedelta(days=5))
        later = confirmed_candidates(reader, target.id, as_of=now[0], lookback=timedelta(days=5))
    assert [candidate.person_id for candidate in prior] == [a]
    assert {candidate.person_id for candidate in later} == {a, b}
