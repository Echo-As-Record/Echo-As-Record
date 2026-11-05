"""事件框架的行为测试；测试计划器的 payload 不构成正式业务约定。"""

from collections.abc import Sequence

import pytest
from ear_attribution import AttributionService, Planner
from ear_contracts import (
    Cause,
    DataChanged,
    Event,
    Record,
    ReevaluationRequested,
    SemanticRecord,
    new_id,
)
from ear_storage_client import (
    AppendRaw,
    ConflictError,
    Context,
    Create,
    MemoryStorage,
    Query,
    Reader,
    Supersede,
    Update,
    WriteOp,
)
from ear_storage_client.testing import make_raw, ts


def ignore(reader: Reader, record: Record, event: Event) -> None:
    return None


def service(storage: MemoryStorage, planner: Planner) -> AttributionService:
    return AttributionService(
        storage.client("attribution"), new_record=planner, feedback=planner, reevaluation=planner
    )


def append(storage: MemoryStorage, data_type: str = "voiceprint") -> DataChanged:
    record = make_raw(data_type, start="2026-11-05T00:00:00Z")
    module = "api" if data_type == "feedback" else "adapter"
    result = storage.client(module).write([AppendRaw(record)], Context.system()).check()
    event = result.events[0]
    assert isinstance(event, DataChanged)
    return event


def request(target_id: str) -> ReevaluationRequested:
    return ReevaluationRequested(
        event_id=new_id(),
        occurred_at=ts("2026-11-05T00:00:00Z"),
        target_type="attribution",
        target_id=target_id,
        cause=Cause(data_type="person", data_id=new_id(), old_version=1, new_version=2),
        change_description="test change",
        root_feedback_id=new_id(),
        depth=2,
    )


@pytest.mark.parametrize("data_type", ["voiceprint", "gps_point", "feedback"])
def test_duplicate_delivery_commits_once(data_type: str) -> None:
    storage = MemoryStorage(duplicate_delivery=True)
    calls = []

    def planner(reader: Reader, record: Record, event: Event) -> Sequence[WriteOp]:
        calls.append(record.id)
        return [Create("person", new_id(), {"test_marker": record.id})]

    service(storage, planner).subscribe()
    event = append(storage, data_type)
    storage.run_until_idle()
    assert calls == [event.data_id]
    assert storage.client("attribution").count(Query("person")) == 1
    assert storage.client("attribution").is_processed("attribution", event.event_id)
    assert not storage.dead_letters
    created = storage.client("attribution").query(Query("person")).items[0]
    assert isinstance(created, SemanticRecord)
    assert created.root_feedback_id == (event.data_id if data_type == "feedback" else "system")
    assert created.depth == 0


def test_failed_batch_does_not_acknowledge_or_leave_partial_writes() -> None:
    storage = MemoryStorage()
    event = append(storage)
    person_id = new_id()

    def planner(reader: Reader, record: Record, event: Event) -> Sequence[WriteOp]:
        return [Create("person", person_id, {}), Create("person", person_id, {})]

    with pytest.raises(ConflictError):
        service(storage, planner).handle(event)
    client = storage.client("attribution")
    assert client.get(person_id) is None
    assert not client.is_processed("attribution", event.event_id)


def test_conflict_reloads_current_state() -> None:
    storage = MemoryStorage()
    client = storage.client("attribution")
    person_id = new_id()
    client.write([Create("person", person_id, {"test_counter": 0})], Context.system()).check()
    event = append(storage)
    seen = []

    def planner(reader: Reader, record: Record, event: Event) -> Sequence[WriteOp]:
        current = reader.get(person_id)
        assert isinstance(current, SemanticRecord)
        seen.append(current.version)
        if len(seen) == 1:
            # 模拟计划计算期间，另一个处理者完成了一次写入。
            client.write(
                [Update("person", person_id, 1, {"test_counter": 10})], Context.system()
            ).check()
        return [Update("person", person_id, current.version, {"test_counter": current.version})]

    service(storage, planner).handle(event)
    assert seen == [1, 2]
    current = client.get(person_id)
    assert isinstance(current, SemanticRecord) and current.version == 3
    assert current.payload == {"test_counter": 2}
    assert client.is_processed("attribution", event.event_id)


def test_reassessment_reads_current_version_and_preserves_context() -> None:
    storage = MemoryStorage()
    client = storage.client("attribution")
    subject = append(storage)
    target = new_id()
    # 仅为满足①已有约束的测试数据，不是③确认过的 payload。
    payload = {"subject_id": subject.data_id, "test_counter": 0}
    client.write([Create("attribution", target, payload)], Context.system()).check()
    client.write(
        [Update("attribution", target, 1, {**payload, "test_counter": 1})], Context.system()
    ).check()
    event = request(target)
    seen = []

    def planner(reader: Reader, record: Record, event: Event) -> Sequence[WriteOp]:
        seen.append(record.version)
        return [Update("attribution", record.id, record.version, {**payload, "test_counter": 2})]

    service(storage, planner).handle(event)
    current = client.get(target)
    assert isinstance(current, SemanticRecord)
    assert seen == [2]
    assert (current.root_feedback_id, current.depth) == (event.root_feedback_id, 2)


def test_superseded_target_does_not_reach_planner() -> None:
    storage = MemoryStorage()
    client = storage.client("attribution")
    subject = append(storage)
    target = new_id()
    client.write(
        [Create("attribution", target, {"subject_id": subject.data_id})], Context.system()
    ).check()
    client.write([Supersede("attribution", target, 1)], Context.system()).check()
    event = request(target)

    def fail(reader: Reader, record: Record, event: Event) -> None:
        pytest.fail("已替代目标不能重新判断")

    service(storage, fail).handle(event)
    assert client.is_processed("attribution", event.event_id)


def test_irrelevant_or_declined_events_are_not_consumed() -> None:
    storage = MemoryStorage()
    runtime = service(storage, ignore)
    for data_type in ["phone_info", "feedback", "voiceprint"]:
        event = append(storage, data_type)
        runtime.handle(event)
        assert not storage.client("attribution").is_processed("attribution", event.event_id)
    runtime.handle(request(new_id()).model_copy(update={"target_type": "artifact"}))


def test_handled_unchanged_event_only_marks_processed() -> None:
    storage = MemoryStorage()
    event = append(storage)

    def unchanged(reader: Reader, record: Record, event: Event) -> Sequence[WriteOp]:
        return []

    service(storage, unchanged).handle(event)
    assert storage.client("attribution").is_processed("attribution", event.event_id)
    assert storage.events == (event,)


def test_missing_target_is_not_silently_acknowledged() -> None:
    storage = MemoryStorage()
    event = request(new_id())
    with pytest.raises(LookupError):
        service(storage, ignore).handle(event)
    assert not storage.client("attribution").is_processed("attribution", event.event_id)


def test_requires_attribution_identity() -> None:
    with pytest.raises(ValueError, match="attribution"):
        AttributionService(
            MemoryStorage().client("api"),
            new_record=ignore,
            feedback=ignore,
            reevaluation=ignore,
        )
