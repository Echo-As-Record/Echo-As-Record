"""Real isolated PostgreSQL, HTTP, authorization and schema-8 upgrade regression."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import LiteralString, cast

import httpx
import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, VersionRef, new_id
from ear_storage import PostgresStorage, SchemaMismatch, bootstrap, migrate, schema
from ear_storage.auth import AuthOperator
from ear_storage.management import FEEDBACK, RESULT, ManagementProcessor, submit
from ear_storage.schema import connection, writer
from ear_storage.service import StorageService
from ear_storage.snapshots import SnapshotUnavailable
from ear_storage_client import (
    AppendRaw,
    AssertTypeRevision,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.testing import make_raw
from ear_storage_client.wire import encode_request
from psycopg import sql

from .test_auth import env as env
from .test_http_postgres import live_server
from .test_management import declaration

pytestmark = pytest.mark.postgres
SYS = Context.system()


@pytest.mark.parametrize("first", [True, False])
@pytest.mark.parametrize("expected", [0, 1])
def test_guard_final_and_start_atomic(env, first, expected):
    _, store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["rule"])
    client = service.client(issued.token)
    guard, op = AssertTypeRevision("rule", expected), Create("rule", new_id(), {})
    result = client.write([guard, op] if first else [op, guard], SYS)
    assert result.conflict and result.conflict.reason == "collection_changed"
    assert client.type_revision("rule") == 0
    assert store.count(Query("rule")) == 0 and store.outbox() == ()


def test_real_http_snapshot_phantom_replay_and_required_types(env):
    dsn, store, service, operator = env
    issued = operator.issue_principal(
        "extraction", read_types=["rule", "activity", "extraction_run"], subscribe_types=["rule"]
    )
    operator.reserve_consumer_identity("extraction.collection", issued.principal_id)
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as client:
        with client.snapshot() as snap:
            revision = snap.type_revision("rule")
            assert revision == 0 and snap.query(Query("rule")).items == ()
            # A distinct thread, backend and DB connection commit a phantom candidate
            # after snapshot capture but before the collection consumer commits.
            with ThreadPoolExecutor(1) as pool:
                added = (
                    pool.submit(
                        PostgresStorage(dsn).client("extraction").write,
                        [Create("rule", new_id(), {"new_candidate": True})],
                        SYS,
                    )
                    .result()
                    .check()
                )
            assert snap.type_revision("rule") == 0 and snap.count(Query("rule")) == 0
            assert client.type_revision("rule") == 1
            aid, sid = new_id(), new_id()
            failed = client.write(
                [
                    Create("activity", aid, {}),
                    RegisterDependencies("activity", aid, []),
                    PutSystemRecord("extraction_run", sid, {}),
                    MarkProcessed("extraction.collection", added.events[0].event_id),
                    AssertTypeRevision("rule", revision),
                ],
                SYS,
            )
            assert failed.conflict and failed.conflict.reason == "collection_changed"
            assert client.get(aid) is None and client.get_system_record(sid) is None
            assert client.dependency_set(aid) is None
            assert not client.is_processed("extraction.collection", added.events[0].event_id)
            assert client.type_revision("activity") == client.type_revision("extraction_run") == 0
            assert len(store.outbox()) == 1
        ops = [Create("activity", aid, {}), AssertTypeRevision("rule", 1)]
        request_id = new_id()
        receipt = client.write(ops, SYS, request_id=request_id).check()
        store.client("extraction").write([Create("rule", new_id(), {})], SYS).check()
        assert client.write(ops, SYS, request_id=request_id) == receipt
        assert client.type_revision("activity") == 1 and client.type_revision("rule") == 2
        with connection(dsn) as conn:
            row = conn.execute(
                "SELECT required_types_text FROM storage.idempotency "
                "WHERE scope=%s AND request_id=%s",
                ("principal:" + issued.principal_id, request_id),
            ).fetchone()
            assert row and "rule" in json.loads(row[0])
        operator.set_grants(issued.principal_id, read_types=["activity"])
        with pytest.raises(PermissionDenied):
            client.write(ops, SYS, request_id=request_id)
        assert store.type_revision("activity") == 1


def test_http_strict_and_default_deny_without_existence_leak(env):
    _, store, service, operator = env
    issued = operator.issue_principal("extraction")
    with (
        live_server(service) as url,
        httpx.Client(base_url=url, headers={"Authorization": "Bearer " + issued.token}) as http,
    ):
        for value in [-1, True, False, 1.0, "0", None]:
            body = json.loads(encode_request([AssertTypeRevision("rule", 0)], SYS))
            body["ops"][0]["body"]["expected_revision"] = value
            body["request_id"] = new_id()
            assert http.post("/v1/batches", json=body).status_code == 422
        with RemoteStorageClient(issued.token, url) as client, client.snapshot() as snap:
            errors = []
            for dtype in ("rule", "unknown_type"):
                for reader in (client, snap):
                    with pytest.raises(PermissionDenied) as caught:
                        reader.type_revision(dtype)
                    errors.append(str(caught.value))
                with pytest.raises(PermissionDenied) as caught:
                    client.write([AssertTypeRevision(dtype, 0)], SYS)
                errors.append(str(caught.value))
            assert len(set(errors)) == 1
    assert store.type_revision("rule") == 0


def test_actual_raw_semantic_system_changes_and_metadata_noops(env):
    _, store, _, _ = env
    client = store.client("extraction")
    raw = make_raw("gps_point", {}, start="2026-10-30T00:00:00Z")
    store.client("adapter").write([AppendRaw(raw), AppendRaw(raw)], SYS).check()
    assert store.type_revision("gps_point") == 1
    rid, aid, sid, event = (new_id() for _ in range(4))
    client.write(
        [
            Create("rule", rid, {"name": "old", "refs": []}),
            Create("activity", aid, {}),
            PutSystemRecord("extraction_run", sid, {}),
            PutSystemRecord("extraction_run", sid, {"n": 2}, 1),
        ],
        SYS,
    ).check()
    assert store.type_revision("extraction_run") == 2
    ref = VersionRef(data_type="rule", id=rid, version=1)
    client.write(
        [RegisterDependencies("activity", aid, [DependencyInput(ref, ("refs",))])], SYS
    ).check()
    client.write([Update("rule", rid, 1, {"name": "new", "refs": []})], SYS).check()
    assert store.type_revision("rule") == 2
    assert client.stale_dependencies([rid]) == []  # unchanged selected field is not unchanged type
    before = store.outbox()
    client.write(
        [
            AssertTypeRevision("rule", 2),
            Update("rule", rid, 2, {"name": "new", "refs": []}),
            RegisterDependencies("activity", aid, [DependencyInput(ref, ("refs",))]),
            PutSystemRecord("extraction_run", sid, {"n": 2}, 2),
            MarkProcessed("extraction.collection", event),
        ],
        SYS,
    ).check()
    assert store.outbox() == before
    request = PublishReevaluation(
        "activity",
        aid,
        Cause(data_type="rule", data_id=rid, old_version=1, new_version=2),
        "synthetic",
        event_id=new_id(),
    )
    for _ in range(2):
        store.client("dependency").write([request], SYS.deeper()).check()
    client.write([Supersede("rule", rid, 2), Supersede("rule", rid, 3)], SYS).check()
    assert store.type_revision("rule") == 3
    result = client.write(
        [
            Create("activity", new_id(), {}),
            PutSystemRecord("extraction_run", sid, {"bad": True}, 2),
            MarkProcessed("extraction.collection", event),
        ],
        SYS,
    )
    assert result.conflict and result.conflict.reason == "already_processed"
    assert store.type_revision("activity") == 1 and store.type_revision("extraction_run") == 2


def test_management_dynamic_registration_and_internal_result_counter(env):
    dsn, store, service, operator = env
    admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
    body = declaration()
    accepted = submit(service.client(admin.token), body)
    assert store.type_revision(FEEDBACK) == store.type_revision(RESULT) == 1
    processor = ManagementProcessor(store)
    assert processor.process(accepted["feedback_id"])["status"] == "succeeded"
    revision = store.type_revision(RESULT)
    assert revision > 1 and store.type_revision("sample_item") == 0
    assert len(store.system_history(accepted["result_id"])) == revision
    assert processor.process(accepted["feedback_id"])["status"] == "succeeded"
    assert submit(service.client(admin.token), body) == accepted
    assert store.type_revision(RESULT) == revision and store.type_revision(FEEDBACK) == 1
    bad = declaration("broken").model_copy(
        update={"read_types": ("unknown",), "subscribe_types": ()}
    )
    rejected = submit(service.client(admin.token), bad)
    assert processor.process(rejected["feedback_id"])["status"] == "failed"
    with connection(dsn) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM storage.type_revisions WHERE data_type='broken_item'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT 1 FROM storage.registry WHERE name='broken_item'").fetchone()
            is None
        )
    assert len([e for _, events in store.outbox() for e in events]) == 2


def test_genuine_schema8_upgrade_preserves_all_rows_and_old_snapshot(pg_dsn, monkeypatch):
    with connection(pg_dsn) as conn, writer(conn):
        for statements in (
            schema.DDL,
            schema.DDL_V2,
            schema.DDL_V3,
            schema.DDL_V4,
            schema.DDL_V5,
            schema.DDL_V6,
            schema.DDL_V7,
            schema.DDL_V8,
        ):
            for statement in statements:
                conn.execute(cast(LiteralString, statement))
        conn.execute("INSERT INTO storage.meta VALUES (true,8,0)")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 8)
        bootstrap(pg_dsn)
        store = PostgresStorage(pg_dsn)
        client = store.client("extraction")
        rid, request_id = new_id(), new_id()
        ops = [Create("rule", rid, {"n": 1}), Create("activity", new_id(), {})]
        receipt = client.write(ops, SYS, request_id=request_id).check()
        client.write([Update("rule", rid, 1, {"n": 2})], SYS).check()
        client.write([Update("rule", rid, 2, {"n": 2})], SYS).check()
        issued = AuthOperator(pg_dsn).issue_principal("extraction", read_types=["rule"])
        lease = StorageService(pg_dsn).create_snapshot(issued.token)
        with pytest.raises(InvalidRequest, match="schema migration"):
            client.type_revision("rule")
    with pytest.raises(SchemaMismatch):
        StorageService(pg_dsn)
    with connection(pg_dsn) as conn:
        tables = conn.execute(
            "SELECT schemaname,tablename FROM pg_tables WHERE "
            "schemaname='storage' OR schemaname LIKE 'ear_m_%'"
        ).fetchall()
        before = {
            tuple(t): conn.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {}.{} t ORDER BY 1").format(
                    *(sql.Identifier(n) for n in t)
                )
            ).fetchall()
            for t in tables
        }
        seed_row = conn.execute(
            "SELECT max(commit_seq) FROM ear_m_extraction.versions WHERE data_type='rule'"
        ).fetchone()
        assert seed_row is not None
        seed = seed_row[0]
    migrate(pg_dsn)
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            schema.SCHEMA_VERSION,
        )
        for t, rows in before.items():
            if t == ("storage", "meta"):
                continue
            assert (
                conn.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {}.{} t ORDER BY 1").format(
                        *(sql.Identifier(n) for n in t)
                    )
                ).fetchall()
                == rows
            )
    store = PostgresStorage(pg_dsn)
    assert store.type_revision("rule") == seed
    assert store.client("extraction").write(ops, SYS, request_id=request_id) == receipt
    assert store.type_revision("rule") == seed
    old = StorageService(pg_dsn).client(issued.token).read_snapshot(lease.snapshot_id)
    old_record = old.get(rid)
    assert old_record is not None and old_record.version == 2
    with pytest.raises(SnapshotUnavailable, match="lacks collection revisions"):
        old.type_revision("rule")
    store.client("extraction").write([Supersede("rule", rid, 2)], SYS).check()
    assert store.type_revision("rule") == seed + 1
    assert store.commit_seq > store.type_revision("rule")
    with StorageService(pg_dsn).client(issued.token).snapshot() as snap:
        assert snap.type_revision("rule") == seed + 1
        assert snap.count(Query("rule")) == 0
    bootstrap(pg_dsn, DEFAULT_REGISTRY.extend(DataTypeSpec("fresh_item", "semantic", "fresh")))
    assert store.type_revision("fresh_item") == 0


def test_guard_covers_implicit_management_results_and_rollback(env):
    dsn, store, service, operator = env
    admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
    body = declaration()
    raw = make_raw(FEEDBACK, body.payload(), start="2026-10-30T00:00:00Z")
    client = service.client(admin.token)
    ops = [AppendRaw(raw), AssertTypeRevision(RESULT, 0)]
    request_id = new_id()
    receipt = client.write(ops, SYS, request_id=request_id)
    assert receipt.conflict and receipt.conflict.reason == "collection_changed"
    assert client.write(ops, SYS, request_id=request_id) == receipt
    assert store.type_revision(FEEDBACK) == store.type_revision(RESULT) == 0
    assert store.get(raw.id) is None and store.outbox() == ()
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.management_tasks").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM storage.management_authority").fetchone() == (0,)


def test_concurrent_writers_do_not_lose_counter_increments(env):
    dsn, store, _, _ = env

    def write_one(index):
        return (
            PostgresStorage(dsn)
            .client("extraction")
            .write(
                [
                    Create("rule", new_id(), {"synthetic": index}),
                    Create("rule", new_id(), {"synthetic": index}),
                ],
                SYS,
            )
            .check()
        )

    with ThreadPoolExecutor(3) as pool:
        receipts = list(pool.map(write_one, range(6)))
    assert len(receipts) == 6
    assert store.type_revision("rule") == store.count(Query("rule")) == 12
    assert sum(len(events) for _, events in store.outbox()) == 12


def test_guard_needs_read_not_ownership_and_snapshot_rechecks_revoke(env):
    _, store, service, operator = env
    issued = operator.issue_principal("attribution", read_types=["rule"])
    client = service.client(issued.token)
    receipt = client.write([AssertTypeRevision("rule", 0)], SYS).check()
    assert receipt.events == () and store.outbox() == ()
    with client.snapshot() as snap:
        assert snap.type_revision("rule") == 0
        operator.set_grants(issued.principal_id, read_types=[])
        with pytest.raises(PermissionDenied, match="access denied"):
            snap.type_revision("rule")
        with pytest.raises(PermissionDenied, match="access denied"):
            client.write([AssertTypeRevision("rule", 0)], SYS)


def test_current_schema_missing_counter_table_fails_closed(env):
    dsn, _, _, _ = env
    with connection(dsn) as conn:
        conn.execute("DROP TABLE storage.type_revisions")
    for operation in (StorageService, migrate, bootstrap):
        with pytest.raises(SchemaMismatch, match="incomplete schema: type_revisions"):
            operation(dsn)
