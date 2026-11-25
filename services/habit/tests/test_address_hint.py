"""Synthetic, auditable dialogue-address candidate; no identity is assigned."""

from datetime import timedelta

from ear_contracts import DEFAULT_REGISTRY, new_id
from ear_habit.service import PersonMemoryService
from ear_habit.store import extend_registry
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage
from ear_storage_client.testing import make_raw, ts


def test_address_hint_uses_prior_turn_and_caller_supplied_alias_only():
    now = [ts("2026-11-20T11:00:00Z")]
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY), clock=lambda: now[0])
    person, audio = new_id(), new_id()
    storage.client("attribution").write(
        [Create("person", person, {"name": "小王"})], Context.system()
    ).check()
    adapter = storage.client("adapter")
    prior = make_raw(
        "transcript_segment",
        {"text": "小王，你怎么看？", "audio_ref_id": audio, "local_speaker_id": "speaker-1"},
        start="2026-11-20T10:00:00Z",
        end="2026-11-20T10:00:04Z",
    )
    target = make_raw(
        "transcript_segment",
        {"text": "我赞成", "audio_ref_id": audio, "local_speaker_id": "speaker-2"},
        start="2026-11-20T10:00:06Z",
        end="2026-11-20T10:00:08Z",
    )
    adapter.write([AppendRaw(prior), AppendRaw(target)], Context.system()).check()
    service = PersonMemoryService(storage.client("habit"))
    hints = service.address_hints(target.id, aliases={"小王": person}, as_of=now[0])
    assert len(hints) == 1 and hints[0].person_id == person
    assert hints[0].preceding_segment_id == prior.id
    assert service.address_hints(target.id, aliases={}, as_of=now[0]) == ()
    assert (
        service.address_hints(
            target.id, aliases={"小王": person}, as_of=now[0], max_gap=timedelta(seconds=1)
        )
        == ()
    )
    # A later-ingested intervening turn cannot leak into the earlier evaluation.
    now[0] += timedelta(hours=1)
    late = make_raw(
        "transcript_segment",
        {"text": "打断一下", "audio_ref_id": audio, "local_speaker_id": "speaker-3"},
        start="2026-11-20T10:00:04Z",
        end="2026-11-20T10:00:05Z",
    )
    adapter.write([AppendRaw(late)], Context.system()).check()
    assert (
        service.address_hints(
            target.id, aliases={"小王": person}, as_of=now[0] - timedelta(hours=1)
        )
        == hints
    )
    assert service.address_hints(target.id, aliases={"小王": person}, as_of=now[0]) == ()
