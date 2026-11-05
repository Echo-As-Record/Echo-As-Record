"""P1b real PostgreSQL boundary tests. No mocks for locks, grants or transactions."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, VersionRef, new_id, utcnow
from ear_storage import (
    AuthenticationError,
    AuthOperator,
    PostgresStorage,
    SchemaMismatch,
    StorageService,
    bootstrap,
    migrate,
    schema,
)
from ear_storage.auth import authenticate, token_digest
from ear_storage.codec import encode_result, fingerprint
from ear_storage.reader import ConnectionBoundReader
from ear_storage.repository import persist
from ear_storage.schema import DDL, connection, writer
from ear_storage_client import (
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
    WriteResult,
)
from ear_storage_client.engine import State, Transaction

pytestmark = pytest.mark.postgres
SYS = Context.system()


@pytest.fixture
def env(pg_dsn: str):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    store = PostgresStorage(pg_dsn)
    return pg_dsn, store, StorageService(pg_dsn), AuthOperator(pg_dsn)


def reference(dtype: str, id: str, version: int = 1) -> VersionRef:
    return VersionRef(data_type=dtype, id=id, version=version)


def test_tokens_default_deny_owner_write_and_no_self_administration(env, caplog):
    dsn, store, service, operator = env
    issued = operator.issue_principal("extraction")
    client = service.client(issued.token)
    id, request = new_id(), new_id()
    ops = [Create("activity", id, {"module": "attribution", "admin": True})]
    receipt = client.write(ops, SYS, request_id=request).check()
    assert client.get(id) is None  # Owner writes do NOT implicitly grant owner reads.
    assert client.write(ops, SYS, request_id=request) == receipt
    with pytest.raises(PermissionDenied, match=r"^access denied$"):
        client.write([Create("person", new_id(), {})], SYS)
    with pytest.raises(TypeError):
        service.client(issued.token, module="attribution")
    for name in ("operator", "issue_principal", "set_grants", "revoke", "client", "outbox"):
        assert not hasattr(client, name)
    with pytest.raises(AuthenticationError, match=r"^invalid credentials$"):
        service.client("malformed")
    with pytest.raises(AuthenticationError, match=r"^invalid credentials$"):
        service.client("a" * 43).get(id)
    assert issued.token not in repr(issued) + repr(client) + repr(operator) + caplog.text
    assert issued.token not in repr(vars(client))  # Bound clients retain only a digest.
    with connection(dsn) as conn:
        row = conn.execute("SELECT id,module,token_hash,active FROM storage.principals").fetchone()
        assert row == (
            issued.principal_id,
            "extraction",
            hashlib.sha256(issued.token.encode()).hexdigest(),
            True,
        )
        assert issued.token not in str(row)
        assert conn.execute("SELECT count(*) FROM storage.type_grants").fetchone() == (0,)
    operator.revoke(issued.principal_id)
    for operation in (
        lambda: client.get(id),
        lambda: client.registry,
        lambda: client.write(ops, SYS, request_id=request),
    ):
        with pytest.raises(AuthenticationError):
            operation()
    assert store.get(id) is not None  # Revocation retains data.


def test_explicit_grants_subscription_subset_atomic_updates_and_restart(env):
    dsn, _store, service, operator = env
    with pytest.raises(InvalidRequest):
        operator.issue_principal("extraction", subscribe_types=["activity"])
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.principals").fetchone() == (0,)
    issued = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    for reads, subscriptions in (([], ["activity"]), (["unknown"], [])):
        with pytest.raises(InvalidRequest):
            operator.set_grants(
                issued.principal_id, read_types=reads, subscribe_types=subscriptions
            )
    with connection(dsn) as conn, writer(conn):
        p = authenticate(conn, token_digest(issued.token))
        assert p.read_types == p.subscribe_types == {"activity"}
    client = StorageService(dsn).client(issued.token)
    assert client.count(Query("activity")) == 0
    operator.set_grants(issued.principal_id)
    with pytest.raises(PermissionDenied):
        client.count(Query("activity"))
    operator.revoke(issued.principal_id)
    operator.set_grants(issued.principal_id, read_types=["activity"])
    with pytest.raises(AuthenticationError):
        service.client(issued.token).get(new_id())


@pytest.mark.parametrize(
    "method",
    [
        "get",
        "get_version",
        "history",
        "get_many",
        "get_system_record",
        "system_history",
        "dependency_set",
        "dependencies_of",
        "dependents_of",
        "stale_dependencies",
    ],
)
def test_hidden_and_missing_ids_are_indistinguishable(env, method: str):
    _dsn, store, service, operator = env
    id, system = new_id(), new_id()
    store.client("extraction").write(
        [
            Create("activity", id, {"secret": "not visible"}),
            Update("activity", id, 1, {"secret": "new"}),
            PutSystemRecord("extraction_run", system, {"secret": "system"}),
            PutSystemRecord("extraction_run", system, {"secret": "new"}, 1),
            RegisterDependencies("activity", id, []),
        ],
        SYS,
    ).check()
    issued = operator.issue_principal("extraction")
    client = service.client(issued.token)

    def invoke(target):
        fn = getattr(client, method)
        if method == "get_version":
            return fn(target, 1)
        if method in {"get_many", "stale_dependencies"}:
            return fn([target])
        return fn(target)

    hidden = system if method in {"get_system_record", "system_history"} else id
    assert invoke(hidden) == invoke(new_id())
    operator.set_grants(issued.principal_id, read_types=["activity", "extraction_run"])
    assert client.get(id) is not None
    assert len(client.history(id)) == 2
    assert client.get_version(id, 1).payload == {"secret": "not visible"}
    assert len(client.system_history(system)) == 2
    assert client.get_system_record(system).revision == 2
    assert set(client.get_many([id, system, new_id()])) == {id}
    assert client.dependency_set(id).empty


def test_query_count_filters_and_unreferenced_type_authorization(env):
    _dsn, store, service, operator = env
    a, b = new_id(), new_id()
    store.client("extraction").write(
        [
            Create("activity", a, {"n": 1, "tags": ["yes"]}),
            Create("activity", b, {"n": 2, "tags": ["no"]}),
            Create("artifact", new_id(), {"parent": a}),
        ],
        SYS,
    ).check()
    issued = operator.issue_principal("adapter", read_types=["activity"])
    client = service.client(issued.token)
    for dtype in ("artifact", "does_not_exist"):
        for method in (client.query, client.count):
            with pytest.raises(PermissionDenied, match=r"^access denied$"):
                method(Query(dtype))
            with pytest.raises(PermissionDenied, match=r"^access denied$"):
                method(Query("activity", unreferenced_by=(dtype, "parent")))
    for query in (Query("activity", where={"n": 1}), Query("activity", contains={"tags": "yes"})):
        assert client.count(query) == len(client.query(query).items) == 1
    operator.set_grants(issued.principal_id, read_types=["activity", "artifact"])
    q = Query("activity", unreferenced_by=("artifact", "parent"))
    assert client.count(q) == 1 and client.query(q).items[0].id == b


def test_dependency_both_ends_complete_sets_and_filtered_reverse_stale(env):
    _dsn, store, service, operator = env
    source, allowed, secret = new_id(), new_id(), new_id()
    store.client("attribution").write([Create("person", source, {"n": 1})], SYS).check()
    store.client("extraction").write(
        [
            Create("activity", allowed, {}),
            Create("artifact", secret, {}),
            RegisterDependencies("activity", allowed, [reference("person", source)]),
            RegisterDependencies("artifact", secret, [reference("person", source)]),
        ],
        SYS,
    ).check()
    issued = operator.issue_principal("adapter", read_types=["activity"])
    client = service.client(issued.token)
    for read in (client.dependency_set, client.dependencies_of):
        with pytest.raises(PermissionDenied, match=r"^access denied$"):
            read(allowed)
    assert client.dependents_of(source) == []
    assert client.dependency_set(secret) is None
    operator.set_grants(issued.principal_id, read_types=["person", "activity"])
    assert len(client.dependency_set(allowed).dependencies) == 1
    assert [e.dependent.id for e in client.dependents_of(source)] == [allowed]
    store.client("attribution").write([Update("person", source, 1, {"n": 2})], SYS).check()
    assert [e.dependent.id for e in client.stale_dependencies([source, secret])] == [allowed]
    operator.set_grants(issued.principal_id, read_types=["person"])
    assert client.dependents_of(source, only_current_dependents=False) == []
    assert client.stale_dependencies([source], only_current_dependents=False) == []


def test_dependency_input_publish_cause_target_and_consumer_checks(env):
    _dsn, store, service, operator = env
    target, cause = new_id(), new_id()
    store.client("extraction").write([Create("activity", target, {})], SYS).check()
    store.client("attribution").write([Create("person", cause, {})], SYS).check()
    issued = operator.issue_principal("extraction", read_types=["activity"])
    client = service.client(issued.token)
    for dtype in ("person", "activity"):  # A forged ref type cannot hide the actual type.
        with pytest.raises(PermissionDenied):
            client.write([RegisterDependencies("activity", target, [reference(dtype, cause)])], SYS)
    operator.set_grants(issued.principal_id, read_types=["person"])
    client.write(
        [RegisterDependencies("activity", target, [reference("person", cause)])], SYS
    ).check()
    event = new_id()
    client.write([MarkProcessed("extraction.worker", event)], SYS).check()
    assert client.is_processed("extraction.worker", event)
    assert not client.is_processed("extraction_other", event)
    assert not service.client(operator.issue_principal("adapter").token).is_processed(
        "extraction.worker", event
    )
    with pytest.raises(PermissionDenied):
        client.write([MarkProcessed("extraction_other", new_id())], SYS)
    publication = PublishReevaluation(
        "activity",
        target,
        Cause(data_type="person", data_id=cause, old_version=None, new_version=1),
        "changed",
    )
    with pytest.raises(PermissionDenied):
        client.write([publication], SYS.deeper())
    scheduler = operator.issue_principal("dependency", read_types=["activity"])
    scheduler_client = service.client(scheduler.token)
    with pytest.raises(PermissionDenied):
        scheduler_client.write([publication], SYS.deeper())
    operator.set_grants(scheduler.principal_id, read_types=["person"])
    with pytest.raises(PermissionDenied):
        scheduler_client.write([publication], SYS.deeper())
    operator.set_grants(scheduler.principal_id, read_types=["person", "activity"])
    scheduler_client.write([publication], SYS.deeper()).check()


def test_principal_request_scopes_replay_reauthorization_and_old_internal_namespace(env):
    dsn, store, service, operator = env
    a = operator.issue_principal("extraction", read_types=["person"])
    b = operator.issue_principal("extraction", read_types=["person"])
    ca, cb = service.client(a.token), service.client(b.token)
    request, target, source = new_id(), new_id(), new_id()
    store.client("attribution").write([Create("person", source, {})], SYS).check()
    ops = [
        Create("activity", target, {}),
        RegisterDependencies("activity", target, [reference("person", source)]),
    ]
    first = ca.write(ops, SYS, request_id=request).check()
    assert ca.write(ops, SYS, request_id=request) == first
    second = cb.write(ops, SYS, request_id=request)
    assert second.conflict is not None and second.conflict.reason == "id_exists"
    store.client("extraction").write([], SYS, request_id=request).check()
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.idempotency WHERE request_id=%s", (request,)
        ).fetchone() == (3,)
    operator.set_grants(a.principal_id)
    with pytest.raises(PermissionDenied):
        ca.write(ops, SYS, request_id=request)
    operator.set_grants(a.principal_id, read_types=["person"])
    assert ca.write(ops, SYS, request_id=request) == first
    operator.revoke(a.principal_id)
    with pytest.raises(AuthenticationError):
        ca.write(ops, SYS, request_id=request)


def test_registry_and_conflicts_do_not_disclose_other_ids_or_payloads(env):
    _dsn, store, service, operator = env
    hidden = new_id()
    store.client("attribution").write(
        [Create("person", hidden, {"private": "hidden-payload"})], SYS
    ).check()
    issued = operator.issue_principal("extraction")
    client = service.client(issued.token)
    registry = client.registry
    assert "activity" in registry and "person" not in registry
    assert all(s.writer == "extraction" for s in registry.data_types.values())
    assert "principals" not in registry and "type_grants" not in registry
    result = client.write([Create("activity", hidden, {})], SYS)
    assert result.conflict.reason == "id_exists"
    assert result.conflict.id is None and result.conflict.data_type is None
    assert not result.conflict.details and result.conflict.message == "write conflict"
    assert hidden not in repr(result) and "hidden-payload" not in repr(result)
    # Unique-field/member conflicts can embed OTHER ids even on an owned type.
    other = new_id()
    person_client = service.client(operator.issue_principal("attribution").token)
    person_client.write([Create("person", other, {"references": ["shared"]})], SYS).check()
    conflict = person_client.write([Create("person", new_id(), {"references": ["shared"]})], SYS)
    assert conflict.conflict.reason == "member_taken"
    assert other not in repr(conflict) and not conflict.conflict.details
    with client.snapshot() as snapshot:
        assert snapshot.get(hidden) is None
    with pytest.raises(NotImplementedError, match="P2"):
        client.subscribe()


def wait_blocked(conn, expected: int):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted "
            "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
        ).fetchone()
        if row == (expected,):
            return
        time.sleep(0.01)
    pytest.fail("operation did not wait on the PostgreSQL writer gate")


@pytest.mark.parametrize("change", ["revoke", "grants"])
@pytest.mark.parametrize("action", ["read", "write", "replay"])
def test_waiting_requests_reauthenticate_after_revocation_commits(env, change: str, action: str):
    dsn, store, service, operator = env
    source, target, request = new_id(), new_id(), new_id()
    store.client("attribution").write([Create("person", source, {})], SYS).check()
    issued = operator.issue_principal("extraction", read_types=["person"])
    client = service.client(issued.token)
    ops = [
        Create("activity", target, {}),
        RegisterDependencies("activity", target, [reference("person", source)]),
    ]
    if action == "replay":
        client.write(ops, SYS, request_id=request).check()

    def change_fn():
        if change == "revoke":
            operator.revoke(issued.principal_id)
        else:
            operator.set_grants(issued.principal_id)

    def action_fn():
        if action == "read":
            return client.query(Query("person"))
        return client.write(ops, SYS, request_id=request)

    before = store.commit_seq
    with ThreadPoolExecutor(2) as pool:
        with connection(dsn) as conn, writer(conn):
            revocation = pool.submit(change_fn)
            wait_blocked(conn, 1)
            request_future = pool.submit(action_fn)
            wait_blocked(conn, 2)
        revocation.result(timeout=10)
        with pytest.raises(AuthenticationError if change == "revoke" else PermissionDenied):
            request_future.result(timeout=10)
    assert store.commit_seq == before
    if action == "write":
        assert store.get(target) is None


def test_genuine_v1_ddl_upgrade_preserves_records_edges_outbox_and_replay(pg_dsn, monkeypatch):
    # Build OLD DDL, not a v2 database with its revision number changed backwards.
    with connection(pg_dsn) as conn, writer(conn):
        for statement in DDL:
            conn.execute(statement)
        conn.execute("INSERT INTO storage.meta VALUES (true,1,7)")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 1)
        bootstrap(pg_dsn)
    a, b, system, event, request = (new_id() for _ in range(5))
    ops = [
        Create("activity", a, {"legacy": 1}),
        Create("activity", b, {}),
        RegisterDependencies("activity", b, [reference("activity", a)]),
        PutSystemRecord("extraction_run", system, {"legacy": True}),
        MarkProcessed("extraction", event),
    ]
    before = State()
    txn = Transaction(before.copy(), DEFAULT_REGISTRY, "extraction", SYS, utcnow(), new_id())
    txn.run(ops)
    result = WriteResult(txn.batch_id, tuple(txn.results), events=tuple(txn.events))
    with connection(pg_dsn) as conn, writer(conn):
        persist(conn, before, txn, 7)
        conn.execute(
            "INSERT INTO storage.idempotency VALUES (%s,%s,%s,%s,%s,%s)",
            (
                "extraction",
                request,
                fingerprint(ops, SYS),
                encode_result(result),
                json.dumps(["activity", "extraction_run"]),
                7,
            ),
        )
    with pytest.raises(SchemaMismatch):
        StorageService(pg_dsn)
    migrate(pg_dsn)
    migrate(pg_dsn)
    store = PostgresStorage(pg_dsn)
    assert store.commit_seq == 7
    legacy = store.get(a)
    assert legacy is not None and legacy.payload == {"legacy": 1}
    assert len(store.history(a)) == 1
    system_record = store.get_system_record(system)
    assert system_record is not None and system_record.payload == {"legacy": True}
    assert len(store.system_history(system)) == 1
    dependencies = store.dependency_set(b)
    assert dependencies is not None and dependencies.dependencies[0].depends_on.id == a
    assert store.is_processed("extraction", event)
    assert store.outbox() == ((7, result.events),)
    assert store.client("extraction").write(ops, SYS, request_id=request) == result
    assert store.commit_seq == 7
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (3,)
        assert conn.execute("SELECT scope FROM storage.idempotency").fetchone() == (
            "internal:extraction",
        )
        assert conn.execute("SELECT count(*) FROM storage.principals").fetchone() == (0,)


def test_admitted_read_holds_gate_until_data_read_finishes(env, monkeypatch):
    dsn, store, service, operator = env
    id = new_id()
    store.client("extraction").write([Create("activity", id, {"value": 1})], SYS).check()
    issued = operator.issue_principal("adapter", read_types=["activity"])
    client = service.client(issued.token)
    entered, release = threading.Event(), threading.Event()
    original = ConnectionBoundReader.get

    def paused(self, record_id):
        entered.set()
        assert release.wait(4)
        return original(self, record_id)

    monkeypatch.setattr(ConnectionBoundReader, "get", paused)
    with ThreadPoolExecutor(2) as pool:
        reading = pool.submit(client.get, id)
        try:
            assert entered.wait(3)
            revoking = pool.submit(operator.revoke, issued.principal_id)
            with connection(dsn) as conn:
                wait_blocked(conn, 1)
            assert not revoking.done()
        finally:
            release.set()
        assert reading.result(timeout=10).payload == {"value": 1}
        revoking.result(timeout=10)
    with pytest.raises(AuthenticationError):
        client.get(id)


def test_raw_read_write_and_implicit_time_source_require_explicit_grants(env):
    _dsn, store, service, operator = env
    from ear_storage_client import AppendRaw
    from ear_storage_client.testing import make_raw

    issued = operator.issue_principal("adapter")
    client = service.client(issued.token)
    raw = make_raw("voiceprint", {"n": 1}, start="2026-10-30T00:00:00Z")
    client.write([AppendRaw(raw)], SYS).check()
    assert client.get(raw.id) is None
    operator.set_grants(issued.principal_id, read_types=["voiceprint"])
    assert client.get(raw.id).payload == {"n": 1}
    assert client.get_version(raw.id, 1) == client.get(raw.id)
    attr = operator.issue_principal("attribution")
    attribution = service.client(attr.token)
    ops = [Create("attribution", new_id(), {"subject_id": raw.id})]
    with pytest.raises(PermissionDenied):
        attribution.write(ops, SYS)
    operator.set_grants(attr.principal_id, read_types=["voiceprint"])
    attribution.write(ops, SYS).check()
    assert store.count(Query("attribution")) == 1


@pytest.fixture
def reused_input(env):
    dsn, store, service, operator = env
    source, target = new_id(), new_id()
    store.client("attribution").write([Create("person", source, {})], SYS).check()
    store.client("extraction").write(
        [
            Create("activity", target, {}),
            RegisterDependencies("activity", target, [reference("person", source)]),
        ],
        SYS,
    ).check()
    issued = operator.issue_principal("extraction", read_types=["person"])
    assert not store.registry.get("activity").requires_dependencies
    return dsn, store, service, operator, issued, source, target


def receipt_count(dsn):
    with connection(dsn) as conn:
        row = conn.execute("SELECT count(*) FROM storage.idempotency").fetchone()
        assert row is not None
        return row[0]


@pytest.mark.parametrize("changed", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("mixed", [False, True], ids=["single", "atomic-batch"])
def test_reused_input_revocation_denies_before_result(reused_input, changed, mixed):
    dsn, store, service, operator, issued, source, target = reused_input
    client = service.client(issued.token)
    if changed:
        store.client("attribution").write([Update("person", source, 1, {"n": 2})], SYS).check()
    operator.set_grants(issued.principal_id)
    created, system, event, request = (new_id() for _ in range(4))
    ops = [Update("activity", target, 1, {})]
    if mixed:
        ops = [
            Create("activity", created, {}),
            PutSystemRecord("extraction_run", system, {"done": True}),
            MarkProcessed("extraction", event),
            *ops,
        ]
    before = store.commit_seq, store.outbox(), receipt_count(dsn)
    error = None
    try:
        result = client.write(ops, SYS, request_id=request)
        print("UNAUTHORIZED RESULT:", result)
    except PermissionDenied as exc:
        error = str(exc)
    assert error == "access denied"
    assert (store.commit_seq, store.outbox(), receipt_count(dsn)) == before
    assert store.get(created) is None and store.get_system_record(system) is None
    assert not store.is_processed("extraction", event)
    assert len(store.history(target)) == 1
    assert store.dependency_set(target).dependencies[0].depends_on.id == source
    assert client.get(target) is None  # No output read grant is needed or implied.


@pytest.mark.parametrize("changed", [False, True], ids=["success", "conflict"])
def test_reused_input_receipt_replay_requires_original_inputs(reused_input, changed):
    dsn, store, service, operator, issued, source, target = reused_input
    client = service.client(issued.token)
    if changed:
        store.client("attribution").write([Update("person", source, 1, {"n": 2})], SYS).check()
    request = new_id()
    ops = [Update("activity", target, 1, {})]
    receipt = client.write(ops, SYS, request_id=request)
    assert receipt.ok is not changed
    # Current graph no longer reveals the input originally used by this receipt.
    store.client("extraction").write([RegisterDependencies("activity", target, [])], SYS).check()
    operator.set_grants(issued.principal_id)
    before = store.commit_seq, receipt_count(dsn), store.outbox()
    with pytest.raises(PermissionDenied, match=r"^access denied$"):
        StorageService(dsn).client(issued.token).write(ops, SYS, request_id=request)
    assert (store.commit_seq, receipt_count(dsn), store.outbox()) == before
    with connection(dsn) as conn:
        row = conn.execute(
            "SELECT required_types_text FROM storage.idempotency WHERE request_id=%s",
            (request,),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == ["person"]
    operator.set_grants(issued.principal_id, read_types=["person"])
    assert client.write(ops, SYS, request_id=request) == receipt
    assert client.get(target) is None


@pytest.mark.parametrize("mode", ["clear-first", "clear-last", "terminal", "new-version"])
def test_reused_input_no_longer_used_needs_no_old_grant(reused_input, mode):
    _dsn, store, service, operator, issued, source, target = reused_input
    store.client("attribution").write([Update("person", source, 1, {"n": 2})], SYS).check()
    operator.set_grants(issued.principal_id)
    client = service.client(issued.token)
    update = Update("activity", target, 1, {})
    clear = RegisterDependencies("activity", target, [])
    ops = {
        "clear-first": [clear, update],
        "clear-last": [update, clear],
        "terminal": [update, Supersede("activity", target, 1)],
        "new-version": [update, Update("activity", target, 1, {"n": 1})],
    }[mode]
    request = new_id()
    receipt = client.write(ops, SYS, request_id=request).check()
    assert client.write(ops, SYS, request_id=request) == receipt
    assert client.get(target) is None
    if mode.startswith("clear"):
        assert store.dependency_set(target).empty


def test_reused_input_unknown_and_forbidden_explicit_inputs_same_error(reused_input):
    dsn, store, service, operator, issued, source, target = reused_input
    operator.set_grants(issued.principal_id)
    client = service.client(issued.token)
    before = store.commit_seq, receipt_count(dsn), store.outbox()
    for dtype, id in [("person", source), ("person", new_id()), ("unknown", new_id())]:
        with pytest.raises(PermissionDenied, match=r"^access denied$"):
            client.write([RegisterDependencies("activity", target, [reference(dtype, id)])], SYS)
    assert (store.commit_seq, receipt_count(dsn), store.outbox()) == before


@pytest.mark.parametrize("hidden_first", [False, True])
def test_reused_input_authorizes_all_groups_before_any_stale_result(reused_input, hidden_first):
    dsn, store, service, operator, issued, _source, target = reused_input
    allowed, other = new_id(), new_id()
    internal = store.client("extraction")
    internal.write(
        [
            Create("activity", allowed, {}),
            Create("activity", other, {}),
            RegisterDependencies("activity", other, [reference("activity", allowed)]),
        ],
        SYS,
    ).check()
    internal.write([Update("activity", allowed, 1, {"n": 2})], SYS).check()
    operator.set_grants(issued.principal_id, read_types=["activity"])
    ops = [Update("activity", other, 1, {}), Update("activity", target, 1, {})]
    if hidden_first:
        ops.reverse()
    before = store.commit_seq, receipt_count(dsn), store.outbox()
    with pytest.raises(PermissionDenied, match=r"^access denied$"):
        service.client(issued.token).write(ops, SYS)
    assert (store.commit_seq, receipt_count(dsn), store.outbox()) == before


def test_reused_input_historical_fields_and_unrelated_graph(reused_input):
    dsn, store, service, operator, issued, source, target = reused_input
    store.client("extraction").write(
        [
            RegisterDependencies(
                "activity", target, [DependencyInput(reference("person", source), ("name",))]
            )
        ],
        SYS,
    ).check()
    # Requires P@1 and P@2 internally even though the selected field is unchanged.
    store.client("attribution").write([Update("person", source, 1, {"other": 2})], SYS).check()
    operator.set_grants(issued.principal_id)
    client = service.client(issued.token)
    request = new_id()
    # A hidden, stale graph elsewhere is not a business input to this create.
    unrelated = new_id()
    client.write([Create("activity", unrelated, {})], SYS).check()
    before = store.commit_seq, receipt_count(dsn), store.outbox()
    with pytest.raises(PermissionDenied, match=r"^access denied$"):
        client.write([Update("activity", target, 1, {})], SYS, request_id=request)
    assert (store.commit_seq, receipt_count(dsn), store.outbox()) == before
    operator.set_grants(issued.principal_id, read_types=["person"])
    receipt = client.write([Update("activity", target, 1, {})], SYS, request_id=request).check()
    assert receipt.results[0].outcome == "unchanged"
    assert client.get(target) is None


@pytest.mark.parametrize("replay", [False, True])
def test_reused_input_waiting_write_rechecks_grants_under_gate(reused_input, replay):
    dsn, store, service, operator, issued, _source, target = reused_input
    client = service.client(issued.token)
    request = new_id()
    ops = [Update("activity", target, 1, {})]
    if replay:
        client.write(ops, SYS, request_id=request).check()
    before = store.commit_seq, receipt_count(dsn), store.outbox()
    with ThreadPoolExecutor(2) as pool:
        with connection(dsn) as conn, writer(conn):
            revoking = pool.submit(operator.set_grants, issued.principal_id)
            wait_blocked(conn, 1)
            writing = pool.submit(client.write, ops, SYS, request_id=request)
            wait_blocked(conn, 2)
        revoking.result(timeout=10)
        with pytest.raises(PermissionDenied, match=r"^access denied$"):
            writing.result(timeout=10)
    assert (store.commit_seq, receipt_count(dsn), store.outbox()) == before
