from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, VersionRef, new_id
from ear_storage import Limits, PostgresStorage, ProjectionLimit, SchemaMismatch, bootstrap, migrate
from ear_storage.schema import WRITER_GATE, connection, writer
from ear_storage_client import (
    AppendRaw,
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
    TimeRange,
    Update,
)
from ear_storage_client.testing import make_raw, ts

pytestmark = pytest.mark.postgres
SYS = Context.system()


def setup(dsn: str) -> PostgresStorage:
    migrate(dsn)
    registry = DEFAULT_REGISTRY.extend_core(
        DataTypeSpec("derived", "semantic", "extraction", requires_dependencies=True)
    )
    bootstrap(dsn, registry)
    return PostgresStorage(dsn)


def ref(id: str, version: int = 1) -> VersionRef:
    return VersionRef(data_type="activity", id=id, version=version)


def test_explicit_migration_registry_atomic_reentry(pg_dsn: str) -> None:
    with pytest.raises(SchemaMismatch):
        PostgresStorage(pg_dsn)
    migrate(pg_dsn)
    with pytest.raises(SchemaMismatch):
        PostgresStorage(pg_dsn)
    store = setup(pg_dsn)
    seq = store.commit_seq
    migrate(pg_dsn)
    bootstrap(pg_dsn, store.registry)
    assert store.commit_seq == seq
    assert store.registry.core_types == DEFAULT_REGISTRY.core_types | {"derived"}
    bad = DEFAULT_REGISTRY.extend(DataTypeSpec("bad", "semantic", "unsafe;drop"))
    with pytest.raises(ValueError):
        bootstrap(pg_dsn, bad)
    changed = replace(store.registry.get("activity"), description="changed")
    registry = replace(
        store.registry, data_types={**store.registry.data_types, "activity": changed}
    )
    with pytest.raises(SchemaMismatch):
        bootstrap(pg_dsn, registry)
    assert PostgresStorage(pg_dsn).registry == store.registry
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("UPDATE storage.meta SET schema_revision=999")
    with pytest.raises(SchemaMismatch):
        migrate(pg_dsn)
    with pytest.raises(SchemaMismatch):
        PostgresStorage(pg_dsn)


def test_restart_persistence_replay_and_histories(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    client = store.client("extraction")
    raw = make_raw("voiceprint", {"n": 1.0}, start="2026-10-30T00:00:00Z")
    store.client("adapter").write([AppendRaw(raw)], SYS).check()
    up, down, sysid, event, request = (new_id() for _ in range(5))
    ops = [
        Create("activity", up, {"n": 1}),
        Create("activity", down, {}),
        RegisterDependencies("activity", down, [DependencyInput(ref(up), ("n",))]),
        PutSystemRecord("extraction_run", sysid, {"done": True}),
        MarkProcessed("extraction", event),
    ]
    result = client.write(ops, SYS, request_id=request).check()
    client.write(
        [
            Update("activity", up, 1, {"n": 1.0}),
            PutSystemRecord("extraction_run", sysid, {"done": False}, 1),
            PutSystemRecord("extraction_run", sysid, {"done": "again"}, 2),
        ],
        SYS,
    ).check()
    before = store.outbox()
    seq = store.commit_seq
    # New objects/connections simulate backend restart; no in-process state survives.
    store = PostgresStorage(pg_dsn)
    client = store.client("extraction")
    assert store.commit_seq == seq and store.outbox() == before
    assert client.get(raw.id).payload == {"n": 1.0}  # type: ignore[union-attr]
    assert [r.payload for r in client.history(up)] == [{"n": 1}, {"n": 1.0}]
    assert [r.revision for r in client.system_history(sysid)] == [1, 2, 3]
    assert client.is_processed("extraction", event)
    assert client.dependency_set(down).dependencies[0].fields == ("n",)  # type: ignore[union-attr]
    assert client.stale_dependencies([up])[0].current_version == 2
    assert client.write(ops, SYS, request_id=request) == result
    assert store.commit_seq == seq
    assert client.request_required_types(request) == frozenset({"activity", "extraction_run"})
    with pytest.raises(InvalidRequest, match="reused"):
        client.write([], SYS, request_id=request)
    assert client.record_types([up, sysid]) == {up: "activity", sysid: "extraction_run"}
    with pytest.raises(InvalidRequest):
        client.write([], SYS, request_id="not-uuid")
    with pytest.raises(NotImplementedError):
        client.snapshot()
    with pytest.raises(NotImplementedError):
        client.subscribe("x", lambda e: None)


def test_atomic_conflicts_final_freshness_and_replay(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b, sysid, event, request = (new_id() for _ in range(5))
    c.write([Create("activity", a, {"name": "A"}), Create("activity", b, {})], SYS).check()
    before = store.outbox()
    ops = [
        RegisterDependencies("activity", b, [ref(a)]),
        PutSystemRecord("extraction_run", sysid, {}),
        MarkProcessed("extraction", event),
        Update("activity", a, 1, {"name": "B"}),
    ]
    result = c.write(ops, SYS, request_id=request)
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    assert c.get_system_record(sysid) is None and not c.is_processed("extraction", event)
    assert c.dependency_set(b) is None and store.outbox() == before
    c.write([Update("activity", a, 1, {"name": "X"})], SYS).check()
    assert c.write(ops, SYS, request_id=request).conflict.reason == "stale_dependency"  # type: ignore[union-attr]
    required = new_id()
    seq = store.commit_seq
    with pytest.raises(InvalidRequest, match="依赖"):
        c.write(
            [
                PutSystemRecord("extraction_run", sysid, {}),
                MarkProcessed("extraction", event),
                Create("derived", required, {}),
            ],
            SYS,
        )
    assert store.commit_seq == seq and c.get(required) is None
    c.write(
        [Create("derived", required, {}), RegisterDependencies("derived", required, [])], SYS
    ).check()
    empty = c.dependency_set(required)
    res = c.write(
        [Update("derived", required, 1, {}), RegisterDependencies("derived", required, [])], SYS
    )
    assert [r.outcome for r in res.results] == ["unchanged", "unchanged"]
    assert res.events == () and c.dependency_set(required) == empty
    c.write(
        [
            Update("derived", required, 1, {}),
            RegisterDependencies("derived", required, [ref(a, 2)]),
        ],
        SYS,
    ).check()
    c.write([Supersede("derived", required, 1)], SYS).check()
    assert c.dependency_set(required) is None and c.dependency_set(required, 1) is not None


@pytest.mark.parametrize(
    ("before", "after", "equal"),
    [
        ({}, {}, True),
        ({}, {"x": None}, False),
        ({"x": None}, {}, False),
        ({"x": 1}, {"x": 1.0}, False),
        ({"x": True}, {"x": 1}, False),
        ({"x": {"a": 1, "b": [2]}}, {"x": {"b": [2], "a": 1}}, True),
        ({"x": [1, 2]}, {"x": [2, 1]}, False),
        ({"x": {"n": 1}}, {"x": {"n": 1.0}}, False),
    ],
)
@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_numeric_and_field_history(
    request: pytest.FixtureRequest,
    backend: str,
    before: dict[str, Any],
    after: dict[str, Any],
    equal: bool,
) -> None:
    store = MemoryStorage() if backend == "memory" else setup(request.getfixturevalue("pg_dsn"))
    c = store.client("extraction")
    a, b = new_id(), new_id()
    c.write([Create("activity", a, {**before, "other": 0}), Create("activity", b, {})], SYS).check()
    c.write([RegisterDependencies("activity", b, [DependencyInput(ref(a), ("x",))])], SYS).check()
    c.write([Update("activity", a, 1, {**after, "other": 1})], SYS).check()
    assert bool(c.stale_dependencies([a])) is not equal
    old = c.write([RegisterDependencies("activity", b, [DependencyInput(ref(a), ("x",))])], SYS)
    assert old.ok is equal
    strict = c.write([RegisterDependencies("activity", b, [ref(a)])], SYS)
    assert strict.conflict is not None and strict.conflict.reason == "stale_dependency"
    assert c.count(Query("activity", where=after)) >= 1
    c.write([Update("activity", a, 2, {**before, "other": 2})], SYS).check()
    assert c.stale_dependencies([a]) == []
    c.write([Supersede("activity", a, 3)], SYS).check()
    assert c.stale_dependencies([a])[0].target_superseded


def test_sql_query_filters_paging_and_numeric_distinct(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    c = store.client("extraction")
    ids = [new_id() for _ in range(4)]
    c.write(
        [
            Create(
                "activity",
                id,
                {"n": value, "arr": [value], "obj": {"b": 2, "a": 1}},
                event_start=ts("2026-10-30T00:00:00Z"),
                event_end=ts("2026-10-30T00:00:00Z"),
            )
            for id, value in zip(ids, [1, 1.0, True, None], strict=True)
        ],
        SYS,
    ).check()
    for id, value in zip(ids, [1, 1.0, True, None], strict=True):
        assert [r.id for r in c.query(Query("activity", where={"n": value})).items] == [id]
        assert c.count(Query("activity", contains={"arr": value})) == 1
    assert c.count(Query("activity", where={"obj": {"a": 1, "b": 2}})) == 4
    assert c.count(Query("activity", where={"missing": None})) == 0
    page = c.query(Query("activity", order="id", limit=2))
    assert [r.id for r in page.items] == ids[:2]
    assert [
        r.id for r in c.query(Query("activity", order="id", limit=2, cursor=page.next_cursor)).items
    ] == ids[2:]
    assert c.get_many([*ids, new_id()]).keys() == set(ids)
    span = TimeRange(ts("2026-10-30T00:00:00Z"), ts("2026-10-30T00:00:01Z"))
    assert c.count(Query("activity", time=span)) == 4
    assert c.count(Query("activity", time=TimeRange(span.start, span.start))) == 0
    c.write([Create("artifact", new_id(), {"activity_id": ids[0]})], SYS).check()
    assert c.count(Query("activity", unreferenced_by=("artifact", "activity_id"))) == 3
    c.write([Supersede("activity", ids[0], 1)], SYS).check()
    assert c.count(Query("activity")) == 3
    assert c.count(Query("activity", include_superseded=True)) == 4
    assert [r.status for r in c.history(ids[0])] == ["active", "superseded"]


def test_dedup_unique_members_global_ids_and_publish(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    a = store.client("attribution")
    c = store.client("extraction")
    raw = make_raw("voiceprint", {}, start="2026-10-30T00:00:00Z")
    adapter = store.client("adapter")
    adapter.write([AppendRaw(raw)], SYS).check()
    other = raw.model_copy(update={"id": new_id()})
    assert adapter.write([AppendRaw(other)], SYS)[0].id == raw.id
    p, q = new_id(), new_id()
    a.write([Create("person", p, {"references": [raw.id]}, dedup_key="p")], SYS).check()
    assert not a.write([Create("person", q, {}, dedup_key="p")], SYS).ok
    assert not a.write([Create("person", q, {"references": [raw.id]})], SYS).ok
    assert not c.write([Create("activity", raw.id, {})], SYS).ok
    a.write(
        [Update("person", p, 1, {"references": []}), Create("person", q, {"references": [raw.id]})],
        SYS,
    ).check()
    attr = new_id()
    a.write([Create("attribution", attr, {"subject_id": raw.id})], SYS).check()
    assert a.get(attr).event_start == raw.event_start  # type: ignore[union-attr]
    r = a.write([Create("attribution", new_id(), {"subject_id": raw.id})], SYS)
    assert r.conflict is not None and r.conflict.reason == "unique_field_taken"
    target = new_id()
    c.write([Create("activity", target, {})], SYS).check()
    pub = PublishReevaluation(
        "activity",
        target,
        Cause(data_type="activity", data_id=target, old_version=None, new_version=1),
        "updated",
        new_id(),
    )
    dep = store.client("dependency")
    assert dep.write([pub], SYS.deeper())[0].outcome == "written"
    assert (
        PostgresStorage(pg_dsn).client("dependency").write([pub], SYS.deeper())[0].outcome
        == "deduplicated"
    )


def test_projection_fail_closed_no_partial_graph(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b = new_id(), new_id()
    c.write([Create("activity", a, {}), Create("activity", b, {})], SYS).check()
    limited = PostgresStorage(pg_dsn, limits=Limits(max_graph_nodes=1))
    seq = store.commit_seq
    with pytest.raises(ProjectionLimit):
        limited.client("extraction").write([RegisterDependencies("activity", a, [ref(b)])], SYS)
    assert store.commit_seq == seq and c.dependency_set(a) is None
    with pytest.raises(ProjectionLimit):
        PostgresStorage(pg_dsn, limits=Limits(max_projection_bytes=1)).client("extraction").write(
            [], SYS
        )
    assert c.get(a) is not None


def test_two_connections_version_cycle_and_raw_races(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b = new_id(), new_id()
    c.write([Create("activity", a, {}), Create("activity", b, {})], SYS).check()
    barrier = threading.Barrier(2)

    def race_update(n: int):
        other = PostgresStorage(pg_dsn).client("extraction")
        barrier.wait(timeout=10)
        return other.write([Update("activity", a, 1, {"n": n})], SYS)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(race_update, [1, 2]))
    assert sum(r.ok for r in results) == 1
    assert next(r.conflict.reason for r in results if r.conflict) == "version_mismatch"
    barrier = threading.Barrier(2)

    def edge(pair: tuple[str, str]):
        other = PostgresStorage(pg_dsn).client("extraction")
        barrier.wait(timeout=10)
        return other.write(
            [RegisterDependencies("activity", pair[0], [ref(pair[1], 2 if pair[1] == a else 1)])],
            SYS,
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(edge, [(a, b), (b, a)]))
    assert sum(r.ok for r in results) == 1
    assert next(r.conflict.reason for r in results if r.conflict) == "dependency_cycle"
    raw = make_raw("voiceprint", {}, start="2026-10-30T00:00:00Z")
    barrier = threading.Barrier(2)

    def append(_: int):
        other = PostgresStorage(pg_dsn).client("adapter")
        barrier.wait(timeout=10)
        return other.write([AppendRaw(raw.model_copy(update={"id": new_id()}))], SYS)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(append, [1, 2]))
    assert sorted(r[0].outcome for r in results) == ["deduplicated", "written"]
    assert results[0][0].id == results[1][0].id


def test_real_multiprocess_gate_and_no_idle_transactions(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    id = new_id()
    store.client("extraction").write([Create("activity", id, {})], SYS).check()
    script = """
import json, sys
from ear_storage import PostgresStorage
from ear_storage_client import Create, Update, Context
config = json.loads(sys.stdin.readline())
client = PostgresStorage(config['dsn']).client('extraction')
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'go'
result = client.write([Update('activity', config['id'], 1, {'n': config['n']})], Context.system())
print('ok' if result.ok else result.conflict.reason, flush=True)
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for n, process in enumerate(workers):
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({"dsn": pg_dsn, "id": id, "n": n}) + "\n")
            process.stdin.flush()
            assert process.stdout.readline().strip() == "ready"
        for process in workers:
            assert process.stdin is not None
            process.stdin.write("go\n")
            process.stdin.flush()
        results = []
        for process in workers:
            stdout, _ = process.communicate(timeout=30)
            assert process.returncode == 0
            results.append(stdout.strip())
        assert sorted(results) == ["ok", "version_mismatch"]
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
    with connection(pg_dsn) as conn:
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with writer(conn):
            assert conn.execute("SHOW transaction_isolation").fetchone() == ("read committed",)
            assert conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND granted"
            ).fetchone() == (1,)
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND state LIKE 'idle in transaction%'"
        ).fetchone() == (0,)
        assert WRITER_GATE > 0
