"""内存实现的行为测试。前 8 组对应工作分解 ① 的验收标准和接口说明里的承诺。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataChanged,
    DataTypeSpec,
    Event,
    RawRecord,
    ReevaluationRequested,
    SemanticRecord,
    VersionRef,
    new_id,
    to_json_dict,
)
from ear_storage_client import (
    AppendRaw,
    ConflictError,
    Context,
    Create,
    InvalidRequest,
    MarkProcessed,
    MemoryStorage,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Query,
    Reader,
    RegisterDependencies,
    StorageClient,
    Supersede,
    TimeRange,
    Update,
    iter_query,
    write_with_retry,
)
from ear_storage_client.testing import make_raw, ts
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SYS = Context.system()


def schema(name: str) -> Draft202012Validator:
    (path,) = CONTRACTS.rglob(f"{name}.schema.json")
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


DATA_CHANGED = schema("data-changed")
REEVALUATION = schema("reevaluation-requested")
SEMANTIC = schema("semantic-record")
RAW = schema("raw-record")


def assert_contract(event: Event) -> None:
    validator = DATA_CHANGED if isinstance(event, DataChanged) else REEVALUATION
    errors = [e.message for e in validator.iter_errors(to_json_dict(event))]
    assert errors == [], errors


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def adapter(storage: MemoryStorage) -> StorageClient:
    return storage.client("adapter")


@pytest.fixture
def attribution(storage: MemoryStorage) -> StorageClient:
    return storage.client("attribution")


@pytest.fixture
def extraction(storage: MemoryStorage) -> StorageClient:
    return storage.client("extraction")


@pytest.fixture
def dependency(storage: MemoryStorage) -> StorageClient:
    return storage.client("dependency")


def voiceprint(adapter: StorageClient, start: str = "2026-10-29T06:05:12Z", **kw: Any) -> str:
    rec = make_raw("voiceprint", {"local_speaker": "C"}, start=start, **kw)
    return adapter.write([AppendRaw(rec)], SYS).check()[0].id or ""


def person(attribution: StorageClient, references: list[str], name: str | None = None) -> str:
    pid = new_id()
    attribution.write(
        [Create("person", pid, {"name": name, "references": references})], SYS
    ).check()
    return pid


def attribute(attribution: StorageClient, subject: str, target: str) -> str:
    aid = new_id()
    payload = {"subject_type": "voiceprint", "subject_id": subject, "target_type": "person"}
    attribution.write([Create("attribution", aid, {**payload, "target_id": target})], SYS).check()
    return aid


# 1. 同一条原始数据写入多次，只存一条 -------------------------------------------


def test_raw_dedup(storage: MemoryStorage, adapter: StorageClient) -> None:
    results = [
        adapter.write([AppendRaw(make_raw("gps_point", {"lat": 1}, start=t, dedup_key="k"))], SYS)
        for t in ("2026-10-29T06:00:00Z",) * 3
    ]
    assert [r[0].outcome for r in results] == ["written", "deduplicated", "deduplicated"]
    assert len({r[0].id for r in results}) == 1
    assert adapter.count(Query("gps_point")) == 1
    assert len(storage.events) == 1
    assert results[1].events == ()


def test_raw_new_adapter_version_is_new_data(adapter: StorageClient) -> None:
    for version in ("gps/1", "gps/2"):
        rec = make_raw("gps_point", {}, start="2026-10-29T06:00:00Z", dedup_key="k")
        adapter.write([AppendRaw(rec.model_copy(update={"adapter_version": version}))], SYS)
    assert adapter.count(Query("gps_point")) == 2


def test_raw_record_matches_contract(adapter: StorageClient) -> None:
    vid = voiceprint(adapter)
    record = adapter.get(vid)
    assert isinstance(record, RawRecord)
    assert list(RAW.iter_errors(record.model_dump(mode="json"))) == []
    assert record.version == 1


# 2. 写入成功就一定发事件，失败就一定不发 -----------------------------------------


def test_batch_is_atomic_on_conflict(
    storage: MemoryStorage, adapter: StorageClient, attribution: StorageClient
) -> None:
    vid = voiceprint(adapter)
    pid = person(attribution, [vid])
    before = len(storage.events)
    result = attribution.write(
        [
            Create("person", new_id(), {"name": "新", "references": []}),
            Update("person", pid, 5, {"name": "乙", "references": [vid]}),
        ],
        SYS,
    )
    assert result.conflict is not None
    assert result.conflict.reason == "version_mismatch"
    assert result.conflict.index == 1
    assert result.conflict.details["current_version"] == 1
    assert len(storage.events) == before
    assert attribution.count(Query("person")) == 1
    with pytest.raises(ConflictError):
        result.check()


def test_batch_is_atomic_on_error(storage: MemoryStorage, attribution: StorageClient) -> None:
    with pytest.raises(InvalidRequest):
        attribution.write(
            [
                Create("person", new_id(), {"name": "甲", "references": []}),
                Create("no_such_type", new_id(), {}),
            ],
            SYS,
        )
    assert storage.events == ()
    assert attribution.count(Query("person")) == 0


def test_events_match_contract(
    storage: MemoryStorage, adapter: StorageClient, attribution: StorageClient
) -> None:
    vid = voiceprint(adapter)
    pid = person(attribution, [vid])
    attribution.write([Update("person", pid, 1, {"name": "乙", "references": [vid]})], SYS)
    attribution.write([Supersede("person", pid, 2)], SYS)
    assert [e.operation for e in storage.events if isinstance(e, DataChanged)] == [
        "created",
        "created",
        "updated",
        "superseded",
    ]
    for event in storage.events:
        assert_contract(event)


# 3. 内容没变就不产生新版本、不发事件 --------------------------------------------


def test_unchanged_content(storage: MemoryStorage, extraction: StorageClient) -> None:
    aid = new_id()
    body = "# 组会\n\n- [[person:x]] 周五前交报告"
    extraction.write(
        [Create("artifact", aid, {"body": body, "rule_id": "r", "rule_version": 1})], SYS
    ).check()
    reordered = {"rule_version": 1, "rule_id": "r", "body": body}
    result = extraction.write([Update("artifact", aid, 1, reordered)], SYS).check()
    assert result[0].outcome == "unchanged"
    assert result[0].version == 1
    assert result.events == ()
    assert len(storage.events) == 1

    changed = {**reordered, "body": body + "\n"}
    result = extraction.write([Update("artifact", aid, 1, changed)], SYS).check()
    assert (result[0].outcome, result[0].version, result[0].previous_version) == ("written", 2, 1)


def test_skip_history_does_not_count_as_change(extraction: StorageClient) -> None:
    aid = new_id()
    extraction.write([Create("activity", aid, {"title": "组会"})], SYS)
    result = extraction.write(
        [Update("activity", aid, 1, {"title": "组会"}, skip_history=True)], SYS
    )
    assert result[0].outcome == "unchanged"


# 4. 能读出任意对象的任意历史版本 -------------------------------------------------


def test_history_and_supersede(storage: MemoryStorage, attribution: StorageClient) -> None:
    pid = person(attribution, [], name="v1")
    for v in range(2, 6):
        attribution.write([Update("person", pid, v - 1, {"name": f"v{v}", "references": []})], SYS)
    result = attribution.write([Supersede("person", pid, 5)], SYS).check()
    assert (result[0].version, result[0].previous_version) == (6, 5)
    event = result.events[0]
    assert isinstance(event, DataChanged)
    assert (event.operation, event.new_version, event.old_version) == ("superseded", 6, 5)

    history = attribution.history(pid)
    assert [h.payload["name"] for h in history] == ["v1", "v2", "v3", "v4", "v5", "v5"]
    assert [h.status for h in history][-2:] == ["active", "superseded"]
    for record in history:
        assert list(SEMANTIC.iter_errors(record.model_dump(mode="json"))) == []
    v3 = attribution.get_version(pid, 3)
    assert isinstance(v3, SemanticRecord) and v3.payload["name"] == "v3"
    current = attribution.get(pid)
    assert isinstance(current, SemanticRecord) and current.superseded
    assert attribution.get_version(pid, 7) is None

    again = attribution.write([Supersede("person", pid, 6)], SYS).check()
    assert again[0].outcome == "unchanged"
    blocked = attribution.write([Update("person", pid, 6, {"name": "x", "references": []})], SYS)
    assert blocked.conflict is not None and blocked.conflict.reason == "object_superseded"
    assert attribution.count(Query("person")) == 0
    assert attribution.count(Query("person", include_superseded=True)) == 1


def test_returned_records_are_copies(attribution: StorageClient) -> None:
    pid = person(attribution, [], name="甲")
    record = attribution.get(pid)
    assert record is not None
    record.payload["name"] = "改了"
    again = attribution.get(pid)
    assert again is not None and again.payload["name"] == "甲"


# 5. 依赖查询不遗漏 ----------------------------------------------------------------


def test_stale_dependencies(
    adapter: StorageClient, attribution: StorageClient, extraction: StorageClient
) -> None:
    v1, v2 = voiceprint(adapter), voiceprint(adapter, start="2026-10-29T06:06:00Z")
    a, b = person(attribution, [v1], "甲"), person(attribution, [v2], "乙")
    att = attribute(attribution, v1, a)
    act, art = new_id(), new_id()
    extraction.write(
        [
            Create("activity", act, {"title": "组会"}),
            Create("artifact", art, {"body": "甲发言"}),
            RegisterDependencies(
                "artifact",
                art,
                [
                    VersionRef(data_type="activity", id=act, version=1),
                    VersionRef(data_type="attribution", id=att, version=1),
                ],
            ),
        ],
        SYS,
    ).check()
    assert extraction.stale_dependencies([att, act]) == []
    assert len(extraction.dependents_of(att)) == 1

    payload = {"subject_type": "voiceprint", "subject_id": v1, "target_type": "person"}
    attribution.write([Update("attribution", att, 1, {**payload, "target_id": b})], SYS).check()
    (stale,) = extraction.stale_dependencies([att, act])
    assert (stale.dependent.id, stale.dependent.version) == (art, 1)
    assert (stale.depends_on.version, stale.current_version) == (1, 2)
    assert not stale.target_superseded

    # 重新判断后结果不变：用新的被依赖版本重新登记，就不再过时
    extraction.write(
        [
            Update("artifact", art, 1, {"body": "甲发言"}),
            RegisterDependencies(
                "artifact",
                art,
                [
                    VersionRef(data_type="activity", id=act, version=1),
                    VersionRef(data_type="attribution", id=att, version=2),
                ],
            ),
        ],
        SYS,
    ).check()
    assert extraction.stale_dependencies([att]) == []

    # 被依赖方整体替代
    extraction.write([Supersede("activity", act, 1)], SYS).check()
    (stale,) = extraction.stale_dependencies([act])
    assert stale.target_superseded and stale.current_version == 2


def test_only_current_dependents(extraction: StorageClient) -> None:
    rule, art = new_id(), new_id()
    extraction.write(
        [
            Create("rule", rule, {"scope": "组会", "instruction": "待办"}),
            Create("artifact", art, {"body": "v1"}),
            RegisterDependencies(
                "artifact", art, [VersionRef(data_type="rule", id=rule, version=1)]
            ),
        ],
        SYS,
    ).check()
    extraction.write(
        [
            Update("artifact", art, 1, {"body": "v2"}),
            RegisterDependencies(
                "artifact", art, [VersionRef(data_type="rule", id=rule, version=1)]
            ),
        ],
        SYS,
    ).check()
    extraction.write([Update("rule", rule, 1, {"scope": "组会", "instruction": "总结"})], SYS)
    current = extraction.stale_dependencies([rule])
    assert [(s.dependent.id, s.dependent.version) for s in current] == [(art, 2)]
    everything = extraction.stale_dependencies([rule], only_current_dependents=False)
    assert sorted(s.dependent.version for s in everything) == [1, 2]
    assert len(extraction.dependencies_of(art, 1)) == 1


def test_register_stale_dependency_is_rejected(
    adapter: StorageClient, attribution: StorageClient, extraction: StorageClient
) -> None:
    vid = voiceprint(adapter)
    a, b = person(attribution, [vid]), person(attribution, [])
    att = attribute(attribution, vid, a)
    payload = {"subject_type": "voiceprint", "subject_id": vid, "target_type": "person"}
    attribution.write([Update("attribution", att, 1, {**payload, "target_id": b})], SYS).check()
    art = new_id()
    result = extraction.write(
        [
            Create("artifact", art, {"body": "x"}),
            RegisterDependencies(
                "artifact", art, [VersionRef(data_type="attribution", id=att, version=1)]
            ),
        ],
        SYS,
    )
    assert result.conflict is not None
    assert result.conflict.reason == "stale_dependency"
    assert result.conflict.details["current_version"] == 2
    assert extraction.get(art) is None


def test_only_writer_registers_dependencies(
    attribution: StorageClient, extraction: StorageClient
) -> None:
    art = new_id()
    extraction.write([Create("artifact", art, {"body": "x"})], SYS)
    with pytest.raises(PermissionDenied):
        attribution.write([RegisterDependencies("artifact", art, [])], SYS)


# 6. 纠正归属：多条数据一起写 ------------------------------------------------------


def test_correct_attribution_batch(
    storage: MemoryStorage, adapter: StorageClient, attribution: StorageClient
) -> None:
    vid, other = voiceprint(adapter), voiceprint(adapter, start="2026-10-29T06:07:00Z")
    a = person(attribution, [vid, other], "甲")
    b = person(attribution, [], "乙")
    att = attribute(attribution, vid, a)
    feedback = new_id()
    payload = {"subject_type": "voiceprint", "subject_id": vid, "target_type": "person"}

    # 只把 V 加进乙、不从甲移出：违反「一条数据最多只作为一个对象的参考」
    bad = attribution.write(
        [Update("person", b, 1, {"name": "乙", "references": [vid]}, skip_history=True)],
        Context.feedback(feedback),
    )
    assert bad.conflict is not None and bad.conflict.reason == "member_taken"

    result = attribution.write(
        [
            Update("person", b, 1, {"name": "乙", "references": [vid]}, skip_history=True),
            Update("person", a, 1, {"name": "甲", "references": [other]}, skip_history=True),
            Update("attribution", att, 1, {**payload, "target_id": b}),
        ],
        Context.feedback(feedback),
    ).check()
    events = [e for e in result.events if isinstance(e, DataChanged)]
    assert len(events) == 3
    assert {e.batch_id for e in events} == {result.batch_id}
    assert [e.skip_history for e in events] == [True, True, False]
    assert {e.root_feedback_id for e in events} == {feedback}
    assert {e.depth for e in events} == {0}


def test_attribution_subject_is_unique(adapter: StorageClient, attribution: StorageClient) -> None:
    vid = voiceprint(adapter)
    a = person(attribution, [])
    attribute(attribution, vid, a)
    payload = {"subject_type": "voiceprint", "subject_id": vid, "target_type": "person"}
    result = attribution.write([Create("attribution", new_id(), {**payload, "target_id": a})], SYS)
    assert result.conflict is not None and result.conflict.reason == "unique_field_taken"


def test_semantic_dedup_key(attribution: StorageClient) -> None:
    key = f"unnamed_person:{new_id()}"
    first = attribution.write(
        [Create("person", new_id(), {"name": None, "references": []}, dedup_key=key)], SYS
    )
    second = attribution.write(
        [Create("person", new_id(), {"name": None, "references": []}, dedup_key=key)], SYS
    )
    assert first.ok
    assert second.conflict is not None
    assert second.conflict.reason == "dedup_key_exists"
    assert second.conflict.details["existing_id"] == first[0].id


# 7. 并发写的冲突控制 ---------------------------------------------------------------


def test_concurrent_writers(attribution: StorageClient) -> None:
    pid = person(attribution, [], "甲")
    with attribution.snapshot() as reader:
        seen = reader.get(pid)
    assert seen is not None
    first = attribution.write([Update("person", pid, seen.version, {"name": "乙"})], SYS)
    second = attribution.write([Update("person", pid, seen.version, {"name": "丙"})], SYS)
    assert first.ok
    assert second.conflict is not None and second.conflict.reason == "version_mismatch"
    assert second.conflict.retryable


def test_write_with_retry_recomputes(attribution: StorageClient) -> None:
    pid = person(attribution, [], "甲")
    calls: list[int] = []

    def plan(reader: Reader) -> list[Update]:
        current = reader.get(pid)
        assert current is not None
        calls.append(current.version)
        if len(calls) == 1:  # 模拟别人在我计算期间改了它
            attribution.write([Update("person", pid, current.version, {"name": "乙"})], SYS)
        return [Update("person", pid, current.version, {"name": current.payload["name"] + "!"})]

    result = write_with_retry(attribution, plan, SYS)
    assert result.ok and calls == [1, 2]
    final = attribution.get(pid)
    assert final is not None and final.payload["name"] == "乙!"


def test_snapshot_is_stable(attribution: StorageClient) -> None:
    pid = person(attribution, [], "甲")
    with attribution.snapshot() as reader:
        attribution.write([Update("person", pid, 1, {"name": "乙"})], SYS)
        old = reader.get(pid)
        assert old is not None and old.version == 1
    new = attribution.get(pid)
    assert new is not None and new.version == 2


# 8. 重复投递不出错 -----------------------------------------------------------------


def test_duplicate_delivery_with_mark_processed() -> None:
    storage = MemoryStorage(duplicate_delivery=True)
    adapter, attribution = storage.client("adapter"), storage.client("attribution")
    outcomes: list[str] = []

    def on_event(event: Event) -> None:
        if not (isinstance(event, DataChanged) and event.data_type == "voiceprint"):
            return
        result = attribution.write(
            [
                Create("person", new_id(), {"name": None, "references": [event.data_id]}),
                MarkProcessed("attribution", event.event_id),
            ],
            Context.of(event),
        )
        outcomes.append("ok" if result.ok else result.conflict.reason if result.conflict else "")

    attribution.subscribe("attribution", on_event, event_types=["data_changed"])
    voiceprint(adapter)
    storage.run_until_idle()
    assert outcomes == ["ok", "already_processed"]
    assert attribution.count(Query("person")) == 1


def test_handler_failure_retries_then_dead_letters() -> None:
    storage = MemoryStorage(max_attempts=3)
    attempts: list[str] = []

    def flaky(event: Event) -> None:
        attempts.append(event.event_id)
        raise RuntimeError("boom")

    storage.client("api").subscribe("api", flaky)
    voiceprint(storage.client("adapter"))
    storage.run_until_idle()
    assert len(attempts) == 3
    assert len(storage.dead_letters) == 1


def test_subscribe_from_beginning(storage: MemoryStorage, adapter: StorageClient) -> None:
    voiceprint(adapter)
    seen: list[Event] = []
    storage.client("dependency").subscribe("dependency.audit", seen.append, from_beginning=True)
    storage.run_until_idle()
    assert len(seen) == 1


# 写入权限与「请求重新判断」 -------------------------------------------------------


def test_write_permission(adapter: StorageClient, attribution: StorageClient) -> None:
    with pytest.raises(PermissionDenied):
        attribution.write([AppendRaw(make_raw("voiceprint", start="2026-10-29T06:00:00Z"))], SYS)
    with pytest.raises(PermissionDenied):
        adapter.write([Create("person", new_id(), {"name": "甲", "references": []})], SYS)
    with pytest.raises(PermissionDenied):
        adapter.subscribe("attribution", lambda e: None)


def test_reevaluation_requested(
    storage: MemoryStorage,
    adapter: StorageClient,
    attribution: StorageClient,
    extraction: StorageClient,
    dependency: StorageClient,
) -> None:
    vid = voiceprint(adapter)
    a = person(attribution, [vid])
    att = attribute(attribution, vid, a)
    art = new_id()
    extraction.write([Create("artifact", art, {"body": "x"})], SYS)
    received: list[Event] = []
    extraction.subscribe("extraction", received.append, event_types=["reevaluation_requested"])

    feedback = new_id()
    ctx = Context.feedback(feedback)
    cause = Cause(data_type="attribution", data_id=att, old_version=1, new_version=2)
    publish = PublishReevaluation("artifact", art, cause, "局部说话人 C：甲 → 乙")
    with pytest.raises(PermissionDenied):
        extraction.write([publish], ctx.deeper())
    with pytest.raises(InvalidRequest):
        dependency.write([publish], ctx)  # depth 0

    record = PutSystemRecord("propagation_record", new_id(), {"step": 1})
    result = dependency.write([record, publish], ctx.deeper()).check()
    storage.run_until_idle()
    (event,) = received
    assert isinstance(event, ReevaluationRequested)
    assert (event.depth, event.root_feedback_id, event.target_id) == (1, feedback, art)
    assert_contract(event)
    assert result[1].event_id == event.event_id

    # 同一 event_id 不重复发布
    again = PublishReevaluation("artifact", art, cause, "x", event_id=event.event_id)
    assert dependency.write([again], ctx.deeper())[0].outcome == "deduplicated"


# 查询 -------------------------------------------------------------------------------


def test_unreferenced_and_time_queries(adapter: StorageClient, attribution: StorageClient) -> None:
    v1 = voiceprint(adapter, start="2026-10-29T06:00:00Z", end="2026-10-29T06:00:30Z")
    v2 = voiceprint(adapter, start="2026-10-29T07:00:00Z", end="2026-10-29T07:00:30Z")
    pending = Query("voiceprint", unreferenced_by=("attribution", "subject_id"))
    assert adapter.count(pending) == 2
    a = person(attribution, [v1])
    att = attribute(attribution, v1, a)
    assert [r.id for r in adapter.query(pending).items] == [v2]

    record = attribution.get(att)
    assert record is not None and record.event_start == ts("2026-10-29T06:00:00Z")
    window = TimeRange(ts("2026-10-29T06:00:00Z"), ts("2026-10-29T06:30:00Z"))
    assert [r.id for r in attribution.query(Query("attribution", time=window)).items] == [att]
    assert attribution.count(Query("attribution", where={"target_id": a})) == 1
    assert attribution.count(Query("person", contains={"references": v1})) == 1

    attribution.write([Supersede("attribution", att, 1)], SYS)
    assert adapter.count(pending) == 2  # 归属关系被替代后，声纹重新算作未归属


def test_point_time_semantics(adapter: StorageClient) -> None:
    for t in ("2026-10-29T06:00:00Z", "2026-10-29T07:00:00Z"):
        adapter.write([AppendRaw(make_raw("gps_point", {}, start=t))], SYS)
    window = TimeRange(ts("2026-10-29T06:00:00Z"), ts("2026-10-29T07:00:00Z"))
    assert adapter.count(Query("gps_point", time=window)) == 1


def test_pagination(adapter: StorageClient) -> None:
    for minute in range(25):
        adapter.write(
            [AppendRaw(make_raw("gps_point", {}, start=f"2026-10-29T06:{minute:02d}:00Z"))], SYS
        )
    query = Query("gps_point", limit=10)
    first = adapter.query(query)
    assert len(first.items) == 10 and first.next_cursor is not None
    everything = list(iter_query(adapter, query))
    assert len(everything) == 25
    starts = [r.event_start for r in everything if r.event_start is not None]
    assert len(starts) == 25 and starts == sorted(starts)


def test_system_records(extraction: StorageClient) -> None:
    rid = new_id()
    start, end = ts("2026-10-28T13:00:00Z"), ts("2026-10-29T13:00:00Z")
    extraction.write(
        [PutSystemRecord("extraction_run", rid, {"status": "running"}, None, start, end)], SYS
    ).check()
    done = {"status": "succeeded"}
    result = extraction.write([PutSystemRecord("extraction_run", rid, done, 1)], SYS).check()
    assert (result[0].version, result.events) == (2, ())
    stale = extraction.write([PutSystemRecord("extraction_run", rid, done, 1)], SYS)
    assert stale.conflict is not None and stale.conflict.reason == "revision_mismatch"
    record = extraction.get_system_record(rid)
    assert record is not None and record.event_end == end
    runs = extraction.query(Query("extraction_run", where={"status": "succeeded"}))
    assert [r.id for r in runs.items] == [rid]


# 扩展 -------------------------------------------------------------------------------


def test_extension_registers_new_type() -> None:
    registry = DEFAULT_REGISTRY.extend(
        DataTypeSpec("social_summary", "semantic", "social", description="社交扩展")
    )
    storage = MemoryStorage(registry)
    social = storage.client("social")
    result = social.write([Create("social_summary", new_id(), {"body": "x"})], SYS).check()
    assert result.events[0].data_type == "social_summary"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        registry.extend(DataTypeSpec("person", "semantic", "social"))


def test_bad_payload_is_rejected(attribution: StorageClient) -> None:
    with pytest.raises(InvalidRequest):
        attribution.write([Create("person", new_id(), {"when": ts("2026-10-29T06:00:00Z")})], SYS)
    with pytest.raises(InvalidRequest):
        attribution.write(
            [Create("attribution", new_id(), {"subject_id": new_id(), "target_id": new_id()})],
            SYS,
        )
