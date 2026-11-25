"""Memory retention, similar episodes, staleness and temporal leakage."""

from datetime import timedelta

from ear_contracts import DEFAULT_REGISTRY, new_id
from ear_habit.service import PersonMemoryService
from ear_habit.store import extend_registry
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Update
from ear_storage_client.testing import make_raw, ts


def setup_memory():
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY))
    client = storage.client("habit")
    service = PersonMemoryService(client)
    activities = []
    for day, color in (("20", "红色"), ("21", "蓝色")):
        start = ts(f"2026-11-{day}T10:00:00Z")
        segment = make_raw(
            "transcript_segment",
            {"text": f"会议决定采用{color}方案", "voiceprint_id": None},
            start=start,
            end=start + timedelta(minutes=1),
        )
        storage.client("adapter").write([AppendRaw(segment)], Context.system()).check()
        activity = new_id()
        storage.client("extraction").write(
            [
                Create(
                    "activity",
                    activity,
                    {"title_md": "项目会议", "description_md": "讨论方案"},
                    event_start=start,
                    event_end=start + timedelta(hours=1),
                )
            ],
            Context.system(),
        ).check()
        service.rebuild_episode(activity, Context.system())
        activities.append((activity, segment.id))
    return storage, service, activities


def test_original_evidence_retained_and_similar_episodes_not_confused():
    storage, service, activities = setup_memory()
    raw = service.search_episodes("红色", mode="raw")
    hybrid = service.search_episodes("红色", mode="hybrid")
    summary = service.search_episodes("红色", mode="summary")
    assert len(raw) == len(hybrid) == 1
    assert hybrid[0].activity_id == activities[0][0]
    assert hybrid[0].evidence[0].segment_id == activities[0][1]
    assert "红色" in hybrid[0].evidence[0].text
    assert summary == ()  # the lossy activity description has no color
    assert len(service.search_episodes("会议", mode="summary")) == 2
    assert service.search_episodes("紫色", mode="raw") == ()
    original = storage.client("habit").get(activities[0][1])
    assert original is not None and original.payload["text"].endswith("红色方案")


def test_online_as_of_does_not_use_later_built_episode():
    storage, service, activities = setup_memory()
    from ear_habit.episodes import find_episode

    first = find_episode(storage.client("habit"), activities[0][0])
    second = find_episode(storage.client("habit"), activities[1][0])
    assert first is not None and second is not None and first.created_at < second.created_at
    assert service.search_episodes("红色", mode="raw", as_of=first.created_at)
    assert service.search_episodes("蓝色", mode="raw", as_of=first.created_at) == ()


def test_changed_activity_marks_episode_stale_until_rebuilt():
    storage, service, activities = setup_memory()
    activity_id = activities[0][0]
    owner = storage.client("extraction")
    activity = owner.get(activity_id)
    assert activity is not None
    owner.write(
        [
            Update(
                "activity",
                activity_id,
                activity.version,
                {"title_md": "修正后的会议", "description_md": "讨论方案"},
                event_start=activity.event_start,
                event_end=activity.event_end,
            )
        ],
        Context.system(),
    ).check()
    assert service.search_episodes("红色", mode="raw") == ()
    assert service.search_episodes("红色", mode="raw", include_stale=True)[0].freshness == "stale"
    service.rebuild_episode(activity_id, Context.system())
    assert service.search_episodes("红色", mode="raw")[0].freshness == "unverified"
