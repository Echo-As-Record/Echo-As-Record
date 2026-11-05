"""Commit-only collection validation, independent of object dependency edges."""

from __future__ import annotations

import json

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, VersionRef, new_id
from ear_storage_client import (
    AppendRaw,
    AssertTypeRevision,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    MemoryStorage,
    PublishReevaluation,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.engine import State
from ear_storage_client.testing import make_raw
from ear_storage_client.wire import decode_request, encode_request

SYS = Context.system()


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None])
def test_strict_expected_revision(value):
    with pytest.raises(InvalidRequest):
        AssertTypeRevision("rule", value)
    body = json.loads(encode_request([AssertTypeRevision("rule", 0)], SYS))
    body["ops"][0]["body"]["expected_revision"] = value
    with pytest.raises(InvalidRequest):
        decode_request(json.dumps(body))


@pytest.mark.parametrize("dtype", ["", "NotSnake", "bad.type", None, True, 42])
def test_strict_type(dtype):
    with pytest.raises(InvalidRequest):
        AssertTypeRevision(dtype, 0)


def test_wire_roundtrip_and_copy_isolation():
    op = AssertTypeRevision("rule", 0)
    assert decode_request(encode_request([op], SYS))[0] == (op,)
    state = State(type_revisions={"rule": 5})
    copied = state.copy()
    copied.type_revisions["rule"] += 1
    assert state.type_revisions == {"rule": 5}


@pytest.mark.parametrize("first", [True, False])
@pytest.mark.parametrize("expected", [0, 1])
def test_batch_start_and_final_guard(first, expected):
    store = MemoryStorage()
    client = store.client("extraction")
    guard = AssertTypeRevision("rule", expected)
    record = Create("rule", new_id(), {})
    ops = [guard, record] if first else [record, guard]
    result = client.write(ops, SYS)
    assert result.conflict and result.conflict.reason == "collection_changed"
    assert client.type_revision("rule") == 0
    assert client.count(Query("rule")) == 0
    assert store.events == ()


def test_counters_actual_changes_and_non_record_noops():
    store = MemoryStorage()
    client = store.client("extraction")
    raw = make_raw("gps_point", {}, start="2026-10-30T00:00:00Z")
    adapter = store.client("adapter")
    adapter.write([AppendRaw(raw), AppendRaw(raw)], SYS).check()
    assert adapter.type_revision("gps_point") == 1
    rid, aid, sid, event = (new_id() for _ in range(4))
    client.write(
        [
            Create("rule", rid, {"name": "old", "refs": []}),
            Create("activity", aid, {}),
            PutSystemRecord("extraction_run", sid, {}),
        ],
        SYS,
    ).check()
    with client.snapshot() as snap:
        assert snap.type_revision("rule") == 1
        client.write([Update("rule", rid, 1, {"name": "new", "refs": []})], SYS).check()
        assert snap.type_revision("rule") == 1
        old = snap.get(rid)
        assert old is not None and old.payload["name"] == "old"
    before = store.events
    client.write(
        [
            AssertTypeRevision("rule", 2),
            Update("rule", rid, 2, {"name": "new", "refs": []}),
            RegisterDependencies(
                "activity",
                aid,
                [DependencyInput(VersionRef(data_type="rule", id=rid, version=2), ("refs",))],
            ),
            PutSystemRecord("extraction_run", sid, {}, 1),
            MarkProcessed("extraction.unit", event),
        ],
        SYS,
    ).check()
    assert store.events == before
    assert client.type_revision("rule") == 2
    assert client.type_revision("activity") == 1
    assert client.type_revision("extraction_run") == 1
    request = PublishReevaluation(
        "activity",
        aid,
        Cause(data_type="rule", data_id=rid, old_version=1, new_version=2),
        "synthetic",
        event_id=new_id(),
    )
    dep = store.client("dependency")
    dep.write([request], SYS.deeper()).check()
    dep.write([request], SYS.deeper()).check()
    assert client.type_revision("activity") == 1
    client.write([PutSystemRecord("extraction_run", sid, {"changed": True}, 1)], SYS).check()
    assert client.type_revision("extraction_run") == 2
    client.write([Supersede("rule", rid, 2), Supersede("rule", rid, 3)], SYS).check()
    assert client.type_revision("rule") == 3
    assert client.count(Query("rule")) == 0
    assert len(client.history(rid)) == 3
    # Failed batches must not leak speculative increments or system changes.
    result = client.write(
        [
            Create("activity", new_id(), {}),
            PutSystemRecord("extraction_run", sid, {"bad": True}, 2),
            MarkProcessed("extraction.unit", event),
        ],
        SYS,
    )
    assert result.conflict and result.conflict.reason == "already_processed"
    assert client.type_revision("activity") == 1
    assert client.type_revision("extraction_run") == 2


def test_phantom_guard_is_not_a_dependency_or_trigger():
    store = MemoryStorage()
    client = store.client("extraction")
    with client.snapshot() as snap:
        revision = snap.type_revision("rule")
        assert snap.query(Query("rule")).items == ()
        client.write([Create("rule", new_id(), {})], SYS).check()
        aid = new_id()
        result = client.write(
            [
                Create("activity", aid, {}),
                RegisterDependencies("activity", aid, []),
                AssertTypeRevision("rule", revision),
            ],
            SYS,
        )
        assert result.conflict and result.conflict.reason == "collection_changed"
        assert client.get(aid) is None and client.dependency_set(aid) is None
    count = len(store.events)
    client.write([AssertTypeRevision("rule", 1)], SYS).check()
    assert len(store.events) == count
    assert all(e.event_type == "data_changed" for e in store.events)
    assert all(
        not s.requires_dependencies and not s.supports_reevaluation
        for s in DEFAULT_REGISTRY.data_types.values()
    )
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec("synthetic_item", "semantic", "synthetic"))
    assert MemoryStorage(registry).reader().type_revision("synthetic_item") == 0
    with pytest.raises(InvalidRequest):
        client.type_revision("unknown_type")
    with pytest.raises(InvalidRequest):
        client.write([AssertTypeRevision("unknown_type", 0)], SYS)
