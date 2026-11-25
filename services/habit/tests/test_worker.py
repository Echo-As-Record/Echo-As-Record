"""Synthetic event delivery, duplicate handling, and ⑤ completion protocol."""

from datetime import UTC, datetime, timedelta

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, SemanticRecord, new_id
from ear_habit.episodes import find_episode
from ear_habit.store import BUCKET_TYPE, extend_registry, find_bucket
from ear_habit.worker import PersonMemoryWorker
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    MemoryStorage,
    PublishReevaluation,
    Update,
)
from ear_storage_client.testing import make_raw, ts


def setup_worker():
    since = datetime.now(UTC) - timedelta(seconds=1)
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY), duplicate_delivery=True)
    owner, adapter = storage.client("attribution"), storage.client("adapter")
    a, b, voice, attr, activity = (new_id() for _ in range(5))
    owner.write(
        [Create("person", a, {"name": "A"}), Create("person", b, {"name": "B"})], Context.system()
    ).check()
    adapter.write(
        [AppendRaw(make_raw("voiceprint", {}, start="2026-11-20T10:00:00Z", id=voice))],
        Context.system(),
    ).check()
    owner.write(
        [
            Create(
                "attribution", attr, {"subject_id": voice, "target_id": a, "target_type": "person"}
            )
        ],
        Context.system(),
    ).check()
    worker = PersonMemoryWorker(storage.client("habit"), active_since=since)
    worker.subscribe()
    storage.client("extraction").write(
        [
            Create(
                "activity",
                activity,
                {"title_md": "会议"},
                event_start=ts("2026-11-20T10:00:00Z"),
                event_end=ts("2026-11-20T11:00:00Z"),
            )
        ],
        Context.system(),
    ).check()
    segment = make_raw(
        "transcript_segment",
        {"text": "我觉得红色方案", "voiceprint_id": voice},
        start="2026-11-20T10:01:00Z",
        end="2026-11-20T10:01:05Z",
    )
    event = adapter.write([AppendRaw(segment)], Context.system()).check().events[0]
    storage.run_until_idle()
    assert not storage.dead_letters
    return storage, worker, a, b, voice, attr, activity, segment, event


def test_automatic_event_and_correction_update_daily_weekly_and_episode():
    storage, worker, a, b, voice, attr, activity, segment, event = setup_worker()
    client = storage.client("habit")
    first = find_bucket(client, f"{a}:day:2026-11-20")
    assert first is not None and first.payload["stats"]["segments"] == 1
    week = find_bucket(client, f"{a}:week:2026-11-16")
    assert week is not None and week.payload["stats"]["segments"] == 1
    episode = find_episode(client, activity)
    assert episode is not None and episode.payload["evidence_ids"] == [segment.id]
    before_version = first.version
    worker.handle(event)  # redelivery must not create a version
    redelivered = find_bucket(client, f"{a}:day:2026-11-20")
    assert redelivered is not None and redelivered.version == before_version
    current = storage.client("attribution").get(attr)
    assert isinstance(current, SemanticRecord)
    storage.client("attribution").write(
        [
            Update(
                "attribution",
                attr,
                current.version,
                {"subject_id": voice, "target_id": b, "target_type": "person"},
            )
        ],
        Context.system(),
    ).check()
    storage.run_until_idle()
    assert not storage.dead_letters
    for person, scale, period, expected in (
        (a, "day", "2026-11-20", 0),
        (b, "day", "2026-11-20", 1),
        (a, "week", "2026-11-16", 0),
        (b, "week", "2026-11-16", 1),
    ):
        result = find_bucket(client, f"{person}:{scale}:{period}")
        assert result is not None and result.payload["stats"]["segments"] == expected
    updated_episode = find_episode(client, activity)
    assert updated_episode is not None and updated_episode.payload["person_ids"] == [b]


def test_reevaluation_writes_unchanged_completion_atomically():
    storage, _, a, _, _, attr, _, _, _ = setup_worker()
    client = storage.client("habit")
    bucket = find_bucket(client, f"{a}:day:2026-11-20")
    assert bucket is not None
    request = (
        storage.client("dependency")
        .write(
            [
                PublishReevaluation(
                    BUCKET_TYPE,
                    bucket.id,
                    Cause(data_type="attribution", data_id=attr, old_version=None, new_version=1),
                    "check",
                )
            ],
            Context.system().deeper(),
        )
        .check()
        .events[0]
    )
    storage.run_until_idle()
    assert not storage.dead_letters
    result = client.get_reevaluation_result(request.event_id)
    assert result is not None and result.outcome == "unchanged"
    assert result.before_version == result.after_version == bucket.version
    assert client.is_processed("habit", request.event_id)


def test_incompatible_activation_scope_cannot_overwrite_existing_memory():
    storage, worker, a, _, _, _, _, _, _ = setup_worker()
    client = storage.client("habit")
    from ear_habit.service import PersonMemoryService

    wrong_scope = PersonMemoryService(client, since=worker.since + timedelta(seconds=1))
    with pytest.raises(ValueError, match="different ingestion scope"):
        wrong_scope.rebuild_day("2026-11-20", Context.system())
    bucket = find_bucket(client, f"{a}:day:2026-11-20")
    assert bucket is not None and bucket.payload["stats"]["segments"] == 1
