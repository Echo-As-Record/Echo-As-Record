"""Real PostgreSQL lease lifecycle, complete historical reads and current authorization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from ear_contracts import DEFAULT_REGISTRY, is_id, new_id, utcnow
from ear_storage import (
    AuthOperator,
    PostgresStorage,
    ProjectionLimit,
    SchemaMismatch,
    SnapshotLimits,
    SnapshotUnavailable,
    StorageService,
    bootstrap,
    migrate,
    schema,
)
from ear_storage.codec import encode_result, fingerprint
from ear_storage.repository import persist
from ear_storage.schema import DDL, DDL_V2, connection, writer
from ear_storage.snapshots import sweep_expired
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Supersede,
    Update,
    WriteResult,
    write_with_retry,
)
from ear_storage_client.engine import State, Transaction, canonical_json
from ear_storage_client.protocol import iter_query
from ear_storage_client.testing import make_raw

from .test_auth import env as env
from .test_auth import reference, wait_blocked

pytestmark = pytest.mark.postgres
SYS = Context.system()


def counts(dsn):
    with connection(dsn) as conn:
        result = []
        for table in ("snapshot_leases", "snapshot_items"):
            row = conn.execute("SELECT count(*) FROM storage." + table).fetchone()
            assert row is not None
            result.append(row[0])
        return tuple(result)


def expire(dsn, sid):
    with connection(dsn) as conn, writer(conn):
        conn.execute(
            "UPDATE storage.snapshot_leases SET expires_at=clock_timestamp()-interval '1s' "
            "WHERE snapshot_id=%s",
            (sid,),
        )


@pytest.fixture
def seeded(env):
    dsn, store, service, operator = env
    source, target, system, empty, event = (new_id() for _ in range(5))
    raw = make_raw("voiceprint", {"unicode": "中文", "n": 1.0}, start="2026-10-30T00:00:00Z")
    store.client("adapter").write([AppendRaw(raw)], SYS).check()
    store.client("attribution").write(
        [
            Create("person", source, {"name": "old"}),
            Update("person", source, 1, {"name": "new"}),
        ],
        SYS,
    ).check()
    store.client("extraction").write(
        [
            Create("activity", target, {"tags": ["yes"], "n": 1}),
            RegisterDependencies(
                "activity", target, [DependencyInput(reference("person", source, 2), ("name",))]
            ),
            Update("activity", target, 1, {"tags": ["yes"], "n": 2}),
            RegisterDependencies("activity", target, [reference("person", source, 2)]),
            Create("activity", empty, {"n": 3}),
            RegisterDependencies("activity", empty, []),
            PutSystemRecord("extraction_run", system, {"n": 1}),
            PutSystemRecord("extraction_run", system, {"n": 2}, 1),
            MarkProcessed("extraction.worker", event),
        ],
        SYS,
    ).check()
    store.client("attribution").write([Update("person", source, 2, {"name": "stale"})], SYS).check()
    issued = operator.issue_principal(
        "extraction", read_types=["activity", "person", "voiceprint", "extraction_run", "artifact"]
    )
    return dsn, store, service, operator, issued, source, target, system, empty, event, raw


def test_all_reader_results_frozen_restart_and_no_transaction_while_paused(seeded):
    dsn, store, service, _operator, issued, source, target, system, empty, event, raw = seeded
    client = service.client(issued.token)
    later_event = new_id()
    operations = [
        ("get", (target,), {}),
        ("get_version", (target, 1), {}),
        ("get_version", (source, 2), {}),
        ("get_version", (raw.id, 1), {}),
        ("get_version", (raw.id, 2), {}),
        ("history", (target,), {}),
        ("history", (raw.id,), {}),
        ("get_many", ([target, source, raw.id, system],), {}),
        ("get_system_record", (system,), {}),
        ("system_history", (system,), {}),
        ("dependency_set", (empty,), {}),
        ("dependency_set", (target, 1), {}),
        ("dependencies_of", (target,), {}),
        ("dependents_of", (source,), {}),
        ("dependents_of", (source,), {"only_current_dependents": False}),
        ("stale_dependencies", ([source],), {"only_current_dependents": False}),
        ("is_processed", ("extraction.worker", event), {}),
        ("is_processed", ("extraction.worker", later_event), {}),
    ]
    for q in [
        Query("activity", limit=1),
        Query("activity", where={"n": 2}),
        Query("activity", contains={"tags": "yes"}),
        Query("extraction_run"),
        Query("voiceprint"),
        Query("activity", unreferenced_by=("artifact", "parent")),
    ]:
        operations.extend([(name, (q,), {}) for name in ("query", "count")])
    expected = [getattr(client, name)(*args, **kw) for name, args, kw in operations]
    with client.snapshot() as snap:
        sid = snap.snapshot_id
        assert is_id(sid)
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL"
            ).fetchone() == (0,)
        # This writer must finish while the context is open (not merely after __exit__).
        with ThreadPoolExecutor(1) as pool:
            pool.submit(
                store.client("extraction").write,
                [
                    RegisterDependencies("activity", target, []),
                    Supersede("activity", target, 2),
                    PutSystemRecord("extraction_run", system, {"n": 3}, 2),
                    MarkProcessed("extraction.worker", later_event),
                    Create("activity", new_id(), {}),
                ],
                SYS,
            ).result(timeout=5).check()
        restarted = StorageService(dsn).read_snapshot(issued.token, sid)
        for view in (snap, restarted):
            assert [getattr(view, name)(*args, **kw) for name, args, kw in operations] == expected
            assert {r.id for r in iter_query(view, Query("activity", limit=1))} == {target, empty}
            assert len(view.history(source)) == 3
            first, second = view.get(target), view.get(target)
            assert first is not None and second is not None
            first.payload["tampered"] = True
            assert "tampered" not in second.payload
        assert client.get(target).superseded
    assert counts(dsn) == (0, 0)
    with pytest.raises(SnapshotUnavailable, match=r"^snapshot unavailable$"):
        snap.get(target)


@pytest.mark.parametrize(
    "method",
    [
        "get",
        "get_version",
        "history",
        "get_many",
        "query",
        "count",
        "dependency_set",
        "dependencies_of",
        "dependents_of",
        "stale_dependencies",
        "get_system_record",
        "system_history",
        "is_processed",
    ],
)
def test_each_reader_method_revalidates_revoked_principal(seeded, method):
    dsn, _store, service, operator, issued, source, target, system, _empty, event, _raw = seeded
    client = service.client(issued.token)
    lease = client.create_snapshot()
    view = client.read_snapshot(lease.snapshot_id)
    operator.revoke(issued.principal_id)
    args = {
        "get_version": (target, 1),
        "get_many": ([target],),
        "query": (Query("activity"),),
        "count": (Query("activity"),),
        "stale_dependencies": ([source],),
        "is_processed": ("extraction.worker", event),
    }.get(method, (system if "system" in method else target,))
    with pytest.raises(SnapshotUnavailable, match=r"^snapshot unavailable$"):
        getattr(view, method)(*args)
    for sid in (lease.snapshot_id, new_id()):
        with pytest.raises(SnapshotUnavailable, match=r"^snapshot unavailable$"):
            client.release_snapshot(sid)
    expire(dsn, lease.snapshot_id)
    assert sweep_expired(dsn) == 1
    assert counts(dsn) == (0, 0)


def test_scope_intersection_hidden_groups_both_end_permissions(seeded):
    dsn, _store, service, operator, issued, source, target, _system, _empty, _event, _raw = seeded
    operator.set_grants(issued.principal_id, read_types=["activity"])
    client = service.client(issued.token)
    with client.snapshot() as snap:
        assert snap.get(source) is None
        for fn in (snap.dependency_set, snap.dependencies_of):
            with pytest.raises(PermissionDenied, match=r"^access denied$"):
                fn(target)
        with connection(dsn) as conn:
            texts = [
                r[0]
                for r in conn.execute(
                    "SELECT record_text FROM storage.snapshot_items WHERE snapshot_id=%s",
                    (snap.snapshot_id,),
                )
            ]
            assert source not in "".join(texts) and "stale" not in "".join(texts)
        operator.set_grants(issued.principal_id, read_types=["activity", "person", "artifact"])
        assert snap.get(source) is None
        assert snap.dependents_of(source) == []
        for q in [Query("person"), Query("activity", unreferenced_by=("artifact", "parent"))]:
            for fn in (snap.query, snap.count):
                with pytest.raises(PermissionDenied):
                    fn(q)
        with pytest.raises(PermissionDenied):
            snap.dependencies_of(target)
    with client.snapshot() as snap:
        assert len(snap.dependencies_of(target)) == 1
        operator.set_grants(issued.principal_id, read_types=["activity"])
        with pytest.raises(PermissionDenied):
            snap.get(source)
        with pytest.raises(PermissionDenied):
            snap.dependency_set(target)
        operator.set_grants(issued.principal_id, read_types=["person"])
        assert snap.dependents_of(source) == []
        assert snap.stale_dependencies([source], only_current_dependents=False) == []
        with pytest.raises(PermissionDenied):
            snap.count(Query("activity"))


def test_foreign_same_module_expiry_descriptor_release_and_bounded_sweep(env):
    dsn, _store, service, operator = env
    a, b = (operator.issue_principal("extraction", read_types=["activity"]) for _ in range(2))
    lease = service.create_snapshot(a.token, ttl_seconds=60)
    descriptor = lease.to_json()
    assert set(descriptor) == {
        "snapshot_id",
        "expires_at",
        "commit_seq",
        "item_count",
        "byte_count",
    }
    assert descriptor["expires_at"].endswith("Z")
    assert descriptor["item_count"] == 1  # Empty-but-authorized type scope is captured.
    for sid in (lease.snapshot_id, new_id(), "malformed"):
        for fn in (service.read_snapshot, service.release_snapshot):
            with pytest.raises(SnapshotUnavailable, match=r"^snapshot unavailable$"):
                fn(b.token, sid)
    view = service.read_snapshot(a.token, lease.snapshot_id)
    expire(dsn, lease.snapshot_id)
    with pytest.raises(SnapshotUnavailable):
        view.count(Query("activity"))
    for _ in range(2):
        expire(dsn, service.create_snapshot(a.token).snapshot_id)
    live = service.create_snapshot(a.token)
    assert sweep_expired(dsn, batch_size=2) == 2
    assert counts(dsn)[0] == 2
    assert sweep_expired(dsn, batch_size=2) == 1
    assert service.read_snapshot(a.token, live.snapshot_id).count(Query("activity")) == 0
    service.release_snapshot(a.token, live.snapshot_id)
    assert counts(dsn) == (0, 0)
    for n in (0, -1, 1001, True):
        with pytest.raises(InvalidRequest):
            sweep_expired(dsn, batch_size=n)


@pytest.mark.parametrize("limit", ["rows", "bytes", "active", "ttl"])
def test_limits_fail_closed_no_leases_items_or_business_changes(seeded, limit):
    dsn, store, _service, _operator, issued, _source, _target, _system, _empty, _event, _raw = (
        seeded
    )
    limits = SnapshotLimits()
    if limit == "rows":
        limits = replace(limits, max_rows=2)
    elif limit == "bytes":
        limits = replace(limits, max_bytes=5)
    else:
        limits = replace(limits, max_active_leases=1)
    client = StorageService(dsn, snapshot_limits=limits).client(issued.token)
    if limit == "active":
        client.create_snapshot()
    before = counts(dsn), store.commit_seq, store.outbox()
    for _ in range(2):
        with pytest.raises(InvalidRequest if limit == "ttl" else ProjectionLimit):
            client.create_snapshot(ttl_seconds=3601 if limit == "ttl" else None)
        assert (counts(dsn), store.commit_seq, store.outbox()) == before


def test_history_limit_not_only_current_rows_and_canonical_items(env):
    dsn, store, service, operator = env
    id = new_id()
    store.client("extraction").write([Create("activity", id, {"n": 1.0, "s": "中文"})], SYS).check()
    for i in range(1, 12):
        store.client("extraction").write([Update("activity", id, i, {"n": i + 1})], SYS).check()
    issued = operator.issue_principal("extraction", read_types=["activity"])
    with pytest.raises(ProjectionLimit):
        StorageService(dsn, snapshot_limits=SnapshotLimits(max_rows=12)).create_snapshot(
            issued.token
        )
    assert counts(dsn) == (0, 0)
    lease = service.create_snapshot(issued.token)
    with connection(dsn) as conn:
        import json

        texts = conn.execute(
            "SELECT record_text FROM storage.snapshot_items WHERE snapshot_id=%s",
            (lease.snapshot_id,),
        ).fetchall()
        assert all(text == canonical_json(json.loads(text)) for (text,) in texts)
    assert lease.item_count == 13
    assert len(service.read_snapshot(issued.token, lease.snapshot_id).history(id)) == 12


def test_creation_waits_for_gate_then_reads_new_state_and_auth(env):
    dsn, _store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["activity"])
    client = service.client(issued.token)
    id = new_id()
    with ThreadPoolExecutor(2) as pool:
        with connection(dsn) as conn, writer(conn):
            writing = pool.submit(client.write, [Create("activity", id, {})], SYS)
            wait_blocked(conn, 1)
            creating = pool.submit(client.create_snapshot)
            wait_blocked(conn, 2)
        writing.result(timeout=10).check()
        lease = creating.result(timeout=10)
    assert client.read_snapshot(lease.snapshot_id).get(id) is not None
    with ThreadPoolExecutor(2) as pool:
        with connection(dsn) as conn, writer(conn):
            revoking = pool.submit(operator.set_grants, issued.principal_id)
            wait_blocked(conn, 1)
            creating = pool.submit(client.create_snapshot)
            wait_blocked(conn, 2)
        revoking.result(timeout=10)
        lease = creating.result(timeout=10)
    assert client.read_snapshot(lease.snapshot_id).get(id) is None


def test_retry_helper_recomputes_releases_and_preserves_exception(env):
    dsn, _store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["activity"])
    client = service.client(issued.token)
    id = new_id()
    client.write([Create("activity", id, {"n": 0})], SYS).check()
    calls = []

    def plan(reader):
        r = reader.get(id)
        calls.append(r.version)
        if len(calls) == 1:
            client.write([Update("activity", id, 1, {"n": 10})], SYS).check()
        return [Update("activity", id, r.version, {"n": r.payload["n"] + 1})]

    write_with_retry(client, plan, SYS).check()
    assert calls == [1, 2] and client.get(id).payload == {"n": 11}
    assert counts(dsn) == (0, 0)
    with pytest.raises(RuntimeError, match="computation failed"), client.snapshot() as snap:
        operator.revoke(issued.principal_id)
        raise RuntimeError("computation failed")
    expire(dsn, snap.snapshot_id)
    assert sweep_expired(dsn) == 1


@pytest.mark.parametrize("receipt_reads", [False, True])
def test_genuine_v2_upgrade_preserves_auth_business_receipts_and_versions(
    pg_dsn, monkeypatch, receipt_reads
):
    with connection(pg_dsn) as conn, writer(conn):
        for statement in (*DDL, *DDL_V2):
            conn.execute(statement)
        conn.execute("INSERT INTO storage.meta VALUES (true,2,9)")
    id, event, system, request = (new_id() for _ in range(4))
    ops = [
        Create("activity", id, {}),
        Update("activity", id, 1, {"n": 2}),
        RegisterDependencies("activity", id, []),
        PutSystemRecord("extraction_run", system, {}),
        MarkProcessed("extraction", event),
    ]
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 2)
        bootstrap(pg_dsn)
        issued = AuthOperator(pg_dsn).issue_principal("extraction", read_types=["activity"])
        before = State()
        txn = Transaction(before.copy(), DEFAULT_REGISTRY, "extraction", SYS, utcnow(), new_id())
        txn.run(ops)
        result = WriteResult(txn.batch_id, tuple(txn.results), events=tuple(txn.events))
        with connection(pg_dsn) as conn, writer(conn):
            persist(conn, before, txn, 9)
            conn.execute(
                "INSERT INTO storage.idempotency VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    "extraction",
                    request,
                    fingerprint(ops, SYS),
                    encode_result(result),
                    canonical_json(["activity"] if receipt_reads else []),
                    9,
                    "principal:" + issued.principal_id,
                ),
            )
    with pytest.raises(SchemaMismatch):
        StorageService(pg_dsn)
    migrate(pg_dsn)
    migrate(pg_dsn)
    service = StorageService(pg_dsn)
    client = service.client(issued.token)
    operator = AuthOperator(pg_dsn)
    other = operator.issue_principal("extraction", read_types=["activity"])
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.consumer_identities").fetchone() == (0,)
    with pytest.raises(PermissionDenied):
        client.write(ops, SYS, request_id=request)
    with pytest.raises(InvalidRequest):
        operator.reserve_consumer_identity("extraction", issued.principal_id)
    operator.adopt_consumer_markers("extraction", issued.principal_id)
    for denied_ops, denied_request in [
        (ops, new_id()),
        ([*ops[:-1], MarkProcessed("extraction", new_id())], request),
    ]:
        with pytest.raises(PermissionDenied):
            client.write(denied_ops, SYS, request_id=denied_request)
    with pytest.raises(PermissionDenied):
        service.client(other.token).write(ops, SYS, request_id=request)
    assert client.write(ops, SYS, request_id=request) == result
    operator.set_grants(issued.principal_id)
    if receipt_reads:
        with pytest.raises(PermissionDenied):
            client.write(ops, SYS, request_id=request)
    else:
        # Original P1 metadata-only receipt has no read requirements; do not narrow it.
        assert client.write(ops, SYS, request_id=request) == result
    operator.set_grants(issued.principal_id, read_types=["activity"])
    assert client.write(ops, SYS, request_id=request) == result
    assert len(client.history(id)) == 2 and client.is_processed("extraction", event)
    group = client.dependency_set(id)
    assert group is not None and group.empty
    store = PostgresStorage(pg_dsn)
    assert store.commit_seq == 9 and store.outbox() == ((9, result.events),)
    assert store.get_system_record(system) is not None
    with client.snapshot() as snap:
        assert snap.get(id) == client.get(id)


def test_active_lease_limit_is_atomic_across_clients_and_principal_scoped(env):
    dsn, _store, _service, operator = env
    service = StorageService(dsn, snapshot_limits=SnapshotLimits(max_active_leases=1))
    a, b = (operator.issue_principal("extraction") for _ in range(2))
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(service.create_snapshot, a.token) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except ProjectionLimit:
                outcomes.append(None)
    assert sum(result is None for result in outcomes) == 1
    assert counts(dsn) == (1, 0)
    service.create_snapshot(b.token)
    assert counts(dsn) == (2, 0)


def test_late_creation_failure_rolls_back_inserted_lease_and_items(env, monkeypatch):
    from ear_storage import snapshots

    dsn, _store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["activity"])
    original = snapshots.validate_lease
    observed = []

    def expire_before_return(conn, principal, sid):
        observed.append(conn.execute("SELECT count(*) FROM storage.snapshot_items").fetchone())
        conn.execute(
            "UPDATE storage.snapshot_leases SET expires_at=clock_timestamp() WHERE snapshot_id=%s",
            (sid,),
        )
        original(conn, principal, sid)

    monkeypatch.setattr(snapshots, "validate_lease", expire_before_return)
    with pytest.raises(SnapshotUnavailable):
        service.create_snapshot(issued.token)
    assert observed == [(1,)]  # Failure after durable rows were inserted, before commit.
    assert counts(dsn) == (0, 0)


def test_byte_limit_counts_historical_payload_before_loading(env, monkeypatch):
    import psycopg

    dsn, store, _service, operator = env
    id = new_id()
    store.client("extraction").write(
        [
            Create("activity", id, {"large": "中" * 20_000}),
            Update("activity", id, 1, {"small": True}),
        ],
        SYS,
    ).check()
    issued = operator.issue_principal("extraction", read_types=["activity"])
    service = StorageService(dsn, snapshot_limits=SnapshotLimits(max_bytes=10_000))
    original: Any = psycopg.Connection.execute
    queries = []

    def observed(conn, query: Any, *args, **kwargs):
        queries.append(query if isinstance(query, str) else query.as_string(conn))
        return original(conn, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", observed)
    with pytest.raises(ProjectionLimit):
        service.create_snapshot(issued.token)
    assert any("sum(octet_length" in query for query in queries)
    assert not any(query.startswith("SELECT %s::text,r.id") for query in queries)
    assert not any(query.startswith("INSERT INTO storage.snapshot") for query in queries)
    assert counts(dsn) == (0, 0)


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5, "60"])
def test_invalid_ttl_is_rejected_without_creating_anything(env, ttl):
    dsn, _store, service, operator = env
    issued = operator.issue_principal("extraction")
    with pytest.raises(InvalidRequest, match="invalid snapshot TTL"):
        service.create_snapshot(issued.token, ttl_seconds=ttl)
    assert counts(dsn) == (0, 0)


@pytest.mark.parametrize("action", ["create", "read", "release"])
def test_waiting_snapshot_operations_reauthenticate_after_revocation(env, action):
    dsn, _store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["activity"])
    client = service.client(issued.token)
    lease = client.create_snapshot()
    view = client.read_snapshot(lease.snapshot_id)
    operation = {
        "create": client.create_snapshot,
        "read": lambda: view.count(Query("activity")),
        "release": lambda: client.release_snapshot(lease.snapshot_id),
    }[action]
    from ear_storage import AuthenticationError

    with ThreadPoolExecutor(2) as pool:
        with connection(dsn) as conn, writer(conn):
            revoking = pool.submit(operator.revoke, issued.principal_id)
            wait_blocked(conn, 1)
            pending = pool.submit(operation)
            wait_blocked(conn, 2)
        revoking.result(timeout=10)
        with pytest.raises(AuthenticationError if action == "create" else SnapshotUnavailable):
            pending.result(timeout=10)
    assert counts(dsn)[0] == 1


def test_limits_config_and_internal_snapshot_remain_explicit(env):
    _dsn, store, service, operator = env
    for field in (
        "max_rows",
        "max_bytes",
        "max_active_leases",
        "default_ttl_seconds",
        "max_ttl_seconds",
    ):
        for value in (0, -1, True):
            with pytest.raises(ValueError):
                replace(SnapshotLimits(), **{field: value})
    with pytest.raises(ValueError):
        SnapshotLimits(default_ttl_seconds=61, max_ttl_seconds=60)
    with pytest.raises(NotImplementedError):
        store.client("extraction").snapshot()
    issued = operator.issue_principal("extraction")
    assert service.client(issued.token).module == "extraction"
