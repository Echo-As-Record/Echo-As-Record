from __future__ import annotations

import json
import selectors
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from typing import Any

import psycopg
import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, VersionRef, new_id
from ear_storage import Limits, PostgresStorage, bootstrap, migrate
from ear_storage.codec import encode_request
from ear_storage.repository import Projection, persist
from ear_storage.schema import connection, writer
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    MarkProcessed,
    MemoryStorage,
    PermissionDenied,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.testing import make_raw

SYS = Context.system()


def setup(dsn: str) -> PostgresStorage:
    migrate(dsn)
    bootstrap(dsn)
    return PostgresStorage(dsn)


def ref(id: str, version: int = 1) -> VersionRef:
    return VersionRef(data_type="activity", id=id, version=version)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_shared_reader_paths(request: pytest.FixtureRequest, backend: str) -> None:
    store = MemoryStorage() if backend == "memory" else setup(request.getfixturevalue("pg_dsn"))
    c = store.client("extraction")
    raw = make_raw("voiceprint", {"x": 1.0}, start="2026-10-30T00:00:00Z")
    store.client("adapter").write([AppendRaw(raw)], SYS).check()
    a, b, down, system, event = (new_id() for _ in range(5))
    c.write(
        [
            Create("activity", a, {}),
            Create("activity", b, {}),
            Create("activity", down, {}),
            PutSystemRecord("extraction_run", system, {"ok": True}),
            RegisterDependencies("activity", down, [ref(a), ref(b)]),
            MarkProcessed("extraction", event),
        ],
        SYS,
    ).check()
    assert c.get_version(raw.id, 1) == c.get(raw.id)
    assert c.get_version(raw.id, 2) is None and c.history(raw.id) == []
    assert c.get(system) is None and c.get_version(system, 1) is None
    assert c.get_many([a, system, new_id()]).keys() == {a}
    assert c.query(Query("extraction_run")).items[0] == c.get_system_record(system)
    assert c.count(Query("voiceprint", source_user="default")) == 1
    assert c.is_processed("extraction", event) and not c.is_processed("other", event)
    assert len(c.dependencies_of(down)) == 2
    assert c.dependency_set(a) is None
    assert c.dependents_of(a)[0].dependent.id == down
    c.write([Update("activity", a, 1, {"x": 1}), Update("activity", b, 1, {"x": 2})], SYS).check()
    assert [d.depends_on.id for d in c.stale_dependencies([b, a, b])] == [b, a]
    c.write(
        [Update("activity", down, 1, {"new": True}), RegisterDependencies("activity", down, [])],
        SYS,
    ).check()
    assert c.dependency_set(down).empty  # type: ignore[union-attr]
    assert c.dependencies_of(down) == [] and len(c.dependencies_of(down, 1)) == 2
    assert c.dependents_of(a) == []
    assert len(c.dependents_of(a, only_current_dependents=False)) == 1
    assert len(c.stale_dependencies([a, b], only_current_dependents=False)) == 2
    c.write([Supersede("activity", down, 2)], SYS).check()
    assert [r.version for r in c.history(down)] == [1, 2, 3]
    assert c.get_version(down, 2).payload == {"new": True}  # type: ignore[union-attr]
    assert all(
        not spec.requires_dependencies and not spec.supports_reevaluation
        for spec in DEFAULT_REGISTRY.data_types.values()
    )
    with pytest.raises(PermissionDenied):
        store.client("adapter").write([Create("activity", new_id(), {})], SYS)


@pytest.mark.postgres
def test_atomic_sql_failure_after_business_and_outbox(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ear_storage.backend as backend

    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b, system, event, request = (new_id() for _ in range(5))
    ops = [
        Create("activity", a, {}),
        Create("activity", b, {}),
        RegisterDependencies("activity", b, [ref(a)]),
        PutSystemRecord("extraction_run", system, {}),
        MarkProcessed("extraction", event),
    ]
    real_persist = persist

    def fail_after_persist(conn: psycopg.Connection, *args: Any) -> None:
        real_persist(conn, *args)
        conn.execute("SELECT 1/0")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "persist", fail_after_persist)
        with pytest.raises(psycopg.errors.DivisionByZero):
            c.write(ops, SYS, request_id=request)
    assert store.commit_seq == 0 and store.outbox() == ()
    assert c.get_many([a, b]) == {} and c.get_system_record(system) is None
    assert c.dependency_set(b) is None and not c.is_processed("extraction", event)
    assert c.request_required_types(request) is None
    result = c.write(ops, SYS, request_id=request).check()
    assert (
        PostgresStorage(pg_dsn).client("extraction").write(ops, SYS, request_id=request) == result
    )
    assert store.commit_seq == 1 and len(store.outbox()[0][1]) == 2


@pytest.mark.postgres
def test_projection_reads_current_and_only_requested_history(pg_dsn: str) -> None:
    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b = new_id(), new_id()
    c.write([Create("activity", a, {"x": "same", "n": 0}), Create("activity", b, {})], SYS).check()
    c.write([Update("activity", a, n, {"x": "same", "n": n}) for n in range(1, 151)], SYS).check()
    op = RegisterDependencies("activity", b, [DependencyInput(ref(a), ("x",))])
    with connection(pg_dsn) as conn, writer(conn):
        state = Projection(conn, Limits(max_graph_nodes=2, max_projection_bytes=10_000)).load(
            [op], store.registry
        )
        assert sum(map(len, state.versions.values())) == 2
        assert set(state.prior) == {(a, 1)}
    c.write([op], SYS).check()
    assert len(c.history(a)) == 151 and c.stale_dependencies([a]) == []


@pytest.mark.postgres
def test_long_chain_duplicate_nodes_and_historical_edges(pg_dsn: str) -> None:
    c = setup(pg_dsn).client("extraction")
    ids = [new_id() for _ in range(1100)]
    c.write([Create("activity", id, {}) for id in ids], SYS).check()
    c.write(
        [RegisterDependencies("activity", a, [ref(b), ref(b)]) for a, b in pairwise(ids)], SYS
    ).check()
    result = c.write([RegisterDependencies("activity", ids[-1], [ref(ids[0])])], SYS)
    assert result.conflict is not None and result.conflict.reason == "dependency_cycle"
    assert len(result.conflict.details["path"]) == 1101
    c.write([Update("activity", ids[0], 1, {"new": True})], SYS).check()
    c.write([RegisterDependencies("activity", ids[-1], [ref(ids[0], 2)])], SYS).check()
    assert c.dependencies_of(ids[0]) == [] and len(c.dependencies_of(ids[0], 1)) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["migrate", "bootstrap", "write"])
def test_every_write_path_waits_on_same_database_gate(pg_dsn: str, operation: str) -> None:
    store = setup(pg_dsn)
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec("plugin_test", "semantic", "plugin"))
    action = {
        "migrate": lambda: migrate(pg_dsn),
        "bootstrap": lambda: bootstrap(pg_dsn, registry),
        "write": lambda: store.client("extraction").write([], SYS),
    }[operation]
    with ThreadPoolExecutor(1) as pool:
        with connection(pg_dsn) as conn, writer(conn):
            future = pool.submit(action)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                blocked = conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                    "AND NOT granted AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database())"
                ).fetchone()
                if blocked == (1,):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("writer never waited on the shared PostgreSQL gate")
            assert not future.done()
        future.result(timeout=10)


@pytest.mark.postgres
@pytest.mark.parametrize("race", ["raw", "cycle", "request", "semantic_dedup"])
def test_independent_process_races(pg_dsn: str, race: str) -> None:
    store = setup(pg_dsn)
    a, b, request_id = new_id(), new_id(), new_id()
    store.client("extraction").write(
        [Create("activity", a, {}), Create("activity", b, {})], SYS
    ).check()
    module = "adapter" if race == "raw" else "extraction"
    raw = make_raw("voiceprint", {}, start="2026-10-30T00:00:00Z")
    batches = {
        "raw": [[AppendRaw(raw.model_copy(update={"id": new_id()}))] for _ in range(2)],
        "cycle": [
            [RegisterDependencies("activity", a, [ref(b)])],
            [RegisterDependencies("activity", b, [ref(a)])],
        ],
        "request": [[Update("activity", a, 1, {"changed": True})]] * 2,
        "semantic_dedup": [[Create("activity", new_id(), {}, dedup_key="same")] for _ in range(2)],
    }[race]
    script = """
import json, sys
from ear_storage import PostgresStorage
from ear_storage.codec import decode_request, encode_result
config=json.loads(sys.stdin.readline())
client=PostgresStorage(config['dsn']).client(config['module'])
ops,context=decode_request(config['request'])
print('ready',flush=True)
assert sys.stdin.readline().strip()=='go'
result=client.write(ops,context,request_id=config['id'])
print(encode_result(result),flush=True)
"""
    workers = []
    try:
        for ops in batches:
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            workers.append(p)
            assert p.stdin is not None and p.stdout is not None
            p.stdin.write(
                json.dumps(
                    {
                        "dsn": pg_dsn,
                        "module": module,
                        "request": encode_request(ops, SYS),
                        "id": request_id if race == "request" else new_id(),
                    }
                )
                + "\n"
            )
            p.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=10), "worker startup timed out"
            assert p.stdout.readline().strip() == "ready"
        for p in workers:
            assert p.stdin is not None
            p.stdin.write("go\n")
            p.stdin.flush()
        results = []
        for p in workers:
            output, _ = p.communicate(timeout=30)
            assert p.returncode == 0
            results.append(json.loads(output)["result"])
        if race == "request":
            assert results[0] == results[1]
            assert len(store.history(a)) == 2 and store.commit_seq == 2
        elif race == "raw":
            assert sorted(r["results"][0]["outcome"] for r in results) == [
                "deduplicated",
                "written",
            ]
            assert results[0]["results"][0]["id"] == results[1]["results"][0]["id"]
        else:
            assert sum(r["conflict"] is None for r in results) == 1
            conflict = next(r["conflict"] for r in results if r["conflict"] is not None)
            assert conflict["reason"] == (
                "dependency_cycle" if race == "cycle" else "dedup_key_exists"
            )
    finally:
        for p in workers:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)
            for stream in (p.stdin, p.stdout):
                if stream is not None:
                    stream.close()


@pytest.mark.postgres
def test_lost_response_after_commit_replays_durable_result(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    import ear_storage.backend as backend

    store = setup(pg_dsn)
    c = store.client("extraction")
    id, request_id = new_id(), new_id()
    ops = [Create("activity", id, {"n": 1.0})]

    @contextmanager
    def lose_response(dsn: str):
        with connection(dsn) as conn:
            yield conn
        # Real transaction committed and connection closed, before caller sees return.
        raise ConnectionError("injected response loss after commit")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "connection", lose_response)
        with pytest.raises(ConnectionError, match="response loss"):
            c.write(ops, SYS, request_id=request_id)
    restarted = PostgresStorage(pg_dsn)
    replay = restarted.client("extraction").write(ops, SYS, request_id=request_id).check()
    assert replay[0].outcome == "written" and replay[0].id == id
    assert len(restarted.history(id)) == 1 and restarted.commit_seq == 1
    assert restarted.outbox()[0][1] == replay.events


@pytest.mark.postgres
def test_waiting_writer_reads_fresh_state_after_gate(pg_dsn: str) -> None:
    import ear_storage.repository as repository
    from ear_contracts import utcnow
    from ear_storage_client.engine import Transaction

    store = setup(pg_dsn)
    c = store.client("extraction")
    a, b = new_id(), new_id()
    c.write([Create("activity", a, {}), Create("activity", b, {})], SYS).check()
    with ThreadPoolExecutor(1) as pool:
        with connection(pg_dsn) as conn, writer(conn):
            ops = [Update("activity", a, 1, {"new": True})]
            before = Projection(conn, Limits()).load(ops, store.registry)
            txn = Transaction(before.copy(), store.registry, "extraction", SYS, utcnow(), new_id())
            txn.run(ops)
            row = conn.execute(
                "UPDATE storage.meta SET commit_seq=commit_seq+1 "
                "WHERE singleton RETURNING commit_seq"
            ).fetchone()
            assert row is not None
            repository.persist(conn, before, txn, row[0])
            future = pool.submit(c.write, [RegisterDependencies("activity", b, [ref(a)])], SYS)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                    "AND NOT granted AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database())"
                ).fetchone() == (1,):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("second connection did not wait for the writer gate")
        result = future.result(timeout=10)
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    assert c.dependency_set(b) is None
