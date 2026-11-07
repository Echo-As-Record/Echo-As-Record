"""Synthetic metadata only; never calls a model or opens external user data."""

import json
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, LLMCallRecord, new_id, utcnow
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    InvalidRequest,
    MemoryStorage,
    ModelBodyPolicy,
    PermissionDenied,
    PutSystemRecord,
    Query,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.wire import decode_request, encode_request
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .test_completion import capable_registry, events
from .test_http_postgres import live_server
from .test_multi_causality import publish_multi, trace

SYS = Context.system()


def payload(**updates):
    start = utcnow()
    data = dict(
        caller_module="extraction",
        purpose="synthetic_test",
        model="fixture/model",
        started_at=start,
        ended_at=start + timedelta(milliseconds=10),
        latency_ms=10,
        status="success",
        usage={"input_tokens": 0, "output_tokens": 3},
        root_feedback_id="system",
        depth=0,
    )
    data.update(updates)
    result = LLMCallRecord.model_validate(data).model_dump(mode="json")
    return {
        k: v
        for k, v in result.items()
        if k not in {"request_text", "response_text"} or v is not None
    }


@pytest.fixture(params=["memory", pytest.param("http", marks=pytest.mark.postgres)])
def calls(request):
    registry = capable_registry()
    operator = issued = dsn = None
    if request.param == "memory":
        store = MemoryStorage(registry)
        server = nullcontext(None)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        migrate(dsn)
        bootstrap(dsn, registry)
        store = PostgresStorage(dsn)
        operator = AuthOperator(dsn)
        issued = operator.issue_principal(
            "llm_gateway", read_types=["llm_call", "artifact", "rule", "person"], subscribe_types=[]
        )
        server = live_server(StorageService(dsn))
    target, source, person = new_id(), new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {}), Create("rule", source, {})], SYS
    ).check()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    with server as url:
        remote = (
            RemoteStorageClient(issued.token, str(url))
            if issued
            else nullcontext(store.client("llm_gateway"))
        )
        with remote as client:
            yield store, client, target, source, person, operator, issued, dsn


def test_metadata_roundtrip_count_snapshot_and_terminal(calls):
    store, client, *_ = calls
    rid = new_id()
    data = payload()
    op = PutSystemRecord("llm_call", rid, data)
    decoded = decode_request(encode_request([op], SYS))[0][0]
    assert isinstance(decoded, PutSystemRecord) and decoded.payload == data
    before = len(events(store))
    with client.snapshot() as snapshot:
        result = client.write([op], SYS).check()
        assert snapshot.get_system_record(rid) is None
    assert result.events == () and len(events(store)) == before
    record = client.get_system_record(rid)
    assert record.id == rid and record.writer == "llm_gateway" and record.revision == 1
    assert record.payload == data
    assert (
        client.count(
            Query("llm_call", where={"caller_module": "extraction", "root_feedback_id": "system"})
        )
        == 1
    )
    assert client.count(Query("llm_call", where={"caller_module": "dependency"})) == 0
    assert client.query(Query("llm_call")).items == (record,)
    if not isinstance(store, MemoryStorage):
        assert client.system_history(rid) == [record]
    with client.snapshot() as snapshot:
        assert snapshot.get_system_record(rid) == record
        assert snapshot.count(Query("llm_call")) == 1
    client.write([PutSystemRecord("llm_call", rid, data, expected_revision=1)], SYS).check()
    assert client.get_system_record(rid).revision == 1
    with pytest.raises(InvalidRequest):
        client.write(
            [
                PutSystemRecord(
                    "llm_call", rid, {**data, "purpose": "different"}, expected_revision=1
                )
            ],
            SYS,
        )
    assert len(events(store)) == before


@pytest.mark.parametrize(
    "field",
    ["prompt", "reply", "request_text", "response_text", "include_body", "credential", "token"],
)
def test_body_and_unknown_fields_rejected_on_write(calls, field):
    store, client, *_ = calls
    before = len(events(store))
    with pytest.raises(InvalidRequest):
        client.write(
            [
                PutSystemRecord(
                    "llm_call",
                    new_id(),
                    {**payload(), field: True if field == "include_body" else "synthetic"},
                )
            ],
            SYS,
        )
    assert client.count(Query("llm_call")) == 0
    assert len(events(store)) == before


def test_owner_and_asserted_caller_not_identity(calls):
    store, client, *_ = calls
    op = PutSystemRecord("llm_call", new_id(), payload(caller_module="dependency"))
    with pytest.raises(PermissionDenied):
        store.client("extraction").write([op], SYS)
    client.write([op], SYS).check()
    assert client.get_system_record(op.id).payload["caller_module"] == "dependency"


def test_request_preserves_all_roots_and_primary(calls):
    store, client, target, source, person, *_ = calls
    a, b = trace(person, dtype="person"), trace(source)
    request = publish_multi(store, target, source, (a, b))
    data = payload(
        caller_module="dependency",
        request_event_id=request.event_id,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=(a, b),
    )
    rid = new_id()
    before = len(events(store))
    client.write(
        [PutSystemRecord("llm_call", rid, data)], Context(request.root_feedback_id, request.depth)
    ).check()
    saved = LLMCallRecord.model_validate(client.get_system_record(rid).payload)
    assert a in saved.additional_causes and b in saved.additional_causes
    assert any(
        t.cause == request.cause and t.root_feedback_id == request.root_feedback_id
        for t in saved.additional_causes
    )
    assert len(events(store)) == before


@pytest.mark.parametrize("bad", ["fake", "root", "depth", "drop", "predates", "write_root"])
def test_request_rejects_forgery_or_lost_lineage(calls, bad):
    store, client, target, source, person, *_ = calls
    a = trace(person, dtype="person")
    request = publish_multi(store, target, source, (a,))
    changes: dict[str, Any] = dict(
        request_event_id=request.event_id,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=(a,),
    )
    if bad == "fake":
        changes["request_event_id"] = new_id()
    if bad == "root":
        changes["root_feedback_id"] = new_id()
    if bad == "depth":
        changes["depth"] += 1
    if bad == "drop":
        changes["additional_causes"] = ()
    if bad == "predates":
        changes["started_at"] = request.occurred_at - timedelta(seconds=1)
        changes["latency_ms"] = None
    context = SYS if bad == "write_root" else Context(changes["root_feedback_id"], changes["depth"])
    with pytest.raises(InvalidRequest):
        client.write([PutSystemRecord("llm_call", new_id(), payload(**changes))], context)
    assert client.count(Query("llm_call")) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"usage": {"input_tokens": -1}},
        {"usage": {"output_tokens": True}},
        {"usage": {"total_tokens": 1.5}},
        {"usage": {"input_tokens": "1"}},
        {"usage": {"token": "secret"}},
        {"latency_ms": True},
        {"latency_ms": -1},
        {"latency_ms": float("nan")},
        {"latency_ms": float("inf")},
        {"latency_ms": 9},
        {"depth": True},
        {"status": "running"},
        {"started_at": "2026-01-01T00:00:00+00:00"},
        {"started_at": "2026-01-01T00:00:00"},
        {"ended_at": "2020-01-01T00:00:00Z"},
        {"status": "failed"},
        {"error_code": "raw exception secret"},
    ],
)
def test_strict_metadata(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def test_failed_and_schema():
    data = payload(status="failed", error_code="upstream_timeout")
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/records/llm-call-record.schema.json"
        ).read_text()
    )
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    validator.validate(data)
    assert list(validator.iter_errors({**data, "prompt": "synthetic"}))
    assert list(validator.iter_errors({**data, "usage": {"input_tokens": True}}))


def test_registry_capability_exact_and_legacy_names():
    spec = DEFAULT_REGISTRY.get("llm_call")
    assert (
        spec.llm_call_metadata and not spec.requires_dependencies and not spec.supports_reevaluation
    )
    assert DataTypeSpec.from_json("llm_call", spec.to_json()) == spec
    assert not DataTypeSpec("old_model_call", "system_record", "extraction").llm_call_metadata
    for name, writer, cap in [
        ("llm_call", "extraction", True),
        ("llm_call", "llm_gateway", False),
        ("other", "llm_gateway", True),
    ]:
        with pytest.raises(ValueError):
            DataTypeSpec(name, "system_record", writer, llm_call_metadata=cap)


@pytest.mark.postgres
@pytest.mark.parametrize("revoked", ["person", "rule", "artifact", "llm_call"])
def test_authenticated_read_revoke_and_replay(pg_dsn, revoked):
    migrate(pg_dsn)
    bootstrap(pg_dsn, capable_registry())
    store = PostgresStorage(pg_dsn)
    target, source, person = new_id(), new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {}), Create("rule", source, {})], SYS
    ).check()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    a = trace(person, dtype="person")
    request = publish_multi(store, target, source, (a,))
    operator = AuthOperator(pg_dsn)
    reads = {"llm_call", "rule", "person", "artifact"}
    issued = operator.issue_principal("llm_gateway", read_types=sorted(reads), subscribe_types=[])
    service = StorageService(pg_dsn, model_body_policy=ModelBodyPolicy(enabled=True))
    with live_server(service) as url, RemoteStorageClient(issued.token, str(url)) as client:
        rid, receipt_id = new_id(), new_id()
        ctx = Context(request.root_feedback_id, request.depth)
        op = PutSystemRecord(
            "llm_call",
            rid,
            payload(
                request_text="synthetic body",
                request_event_id=request.event_id,
                root_feedback_id=request.root_feedback_id,
                depth=request.depth,
                additional_causes=(a,),
            ),
        )
        before = len(events(store))
        first = client.write([op], ctx, request_id=receipt_id).check()
        assert client.write([op], ctx, request_id=receipt_id) == first
        saved = client.get_system_record(rid)
        assert saved is not None and saved.revision == 1
        assert len(client.system_history(rid)) == 1 and len(events(store)) == before
        with client.snapshot() as snapshot:
            assert snapshot.get_system_record(rid)
            operator.set_grants(
                issued.principal_id, read_types=sorted(reads - {revoked}), subscribe_types=[]
            )
            if revoked != "llm_call":
                with pytest.raises(PermissionDenied):
                    client.write([op], ctx, request_id=receipt_id)
            # llm_call grant, like completion grants, permits primary target metadata;
            # source causes have separate grants even for already-created snapshots.
            if revoked in {"person", "rule"}:
                for action in [
                    lambda: client.get_system_record(rid),
                    lambda: client.system_history(rid),
                    lambda: snapshot.get_system_record(rid),
                ]:
                    with pytest.raises(PermissionDenied):
                        action()
                assert client.count(Query("llm_call")) == 0
                assert client.query(Query("llm_call")).items == ()
                with client.snapshot() as hidden:
                    assert hidden.get_system_record(rid) is None
                    assert hidden.count(Query("llm_call")) == 0
            elif revoked == "llm_call":
                assert client.get_system_record(rid) is None
                with pytest.raises(PermissionDenied):
                    client.count(Query("llm_call"))
        if revoked != "llm_call":
            with pytest.raises(PermissionDenied):
                client.write([PutSystemRecord("llm_call", new_id(), op.payload)], ctx)


@pytest.mark.postgres
def test_existing_database_requires_explicit_bootstrap(pg_dsn):
    from ear_contracts import Registry
    from ear_storage.schema import SCHEMA_VERSION, connection

    old = Registry(
        {k: v for k, v in DEFAULT_REGISTRY.data_types.items() if k != "llm_call"},
        DEFAULT_REGISTRY.event_publishers,
        DEFAULT_REGISTRY.core_types - {"llm_call"},
    )
    migrate(pg_dsn)
    bootstrap(pg_dsn, old)
    migrate(pg_dsn)
    assert "llm_call" not in PostgresStorage(pg_dsn).registry
    bootstrap(pg_dsn)
    assert PostgresStorage(pg_dsn).registry.get("llm_call").llm_call_metadata
    with connection(pg_dsn) as conn:
        row = conn.execute("SELECT schema_revision FROM storage.meta WHERE singleton").fetchone()
        assert row == (SCHEMA_VERSION,)


def test_max_request_origins_remain_complete(calls):
    store, client, target, source, *_ = calls
    origins = tuple(trace(source, depth=d) for d in range(1, 257))
    request = publish_multi(store, target, source, origins)
    rid = new_id()
    client.write(
        [
            PutSystemRecord(
                "llm_call",
                rid,
                payload(
                    request_event_id=request.event_id,
                    root_feedback_id=request.root_feedback_id,
                    depth=request.depth,
                    additional_causes=origins,
                ),
            )
        ],
        Context(request.root_feedback_id, request.depth),
    ).check()
    call = LLMCallRecord.model_validate(client.get_system_record(rid).payload)
    assert len(call.additional_causes) == 257
    assert all(t in call.additional_causes for t in origins)


@pytest.mark.parametrize("value", [True, 0, 1.0, float("inf"), float("nan")])
def test_timestamps_are_not_numeric(value):
    with pytest.raises(ValidationError):
        payload(started_at=value)


@pytest.mark.parametrize("bad", ["missing", "wrong_type", "old_version", "body", "bool_version"])
def test_call_additional_cause_validation(calls, bad):
    _store, client, _, source, *_ = calls
    cause = trace(
        new_id() if bad == "missing" else source,
        dtype="person" if bad == "wrong_type" else "rule",
        old=99 if bad == "old_version" else None,
    ).model_dump(mode="json")
    if bad == "body":
        cause["cause"]["prompt"] = "synthetic"
    if bad == "bool_version":
        cause["cause"]["new_version"] = True
    data = {**payload(), "additional_causes": [cause]}
    with pytest.raises(InvalidRequest):
        client.write([PutSystemRecord("llm_call", new_id(), data)], SYS)
    assert client.count(Query("llm_call")) == 0


@pytest.mark.postgres
def test_authenticated_producer_cannot_be_self_reported(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    operator = AuthOperator(pg_dsn)
    issued = operator.issue_principal("extraction", read_types=["llm_call"], subscribe_types=[])
    service = StorageService(pg_dsn)
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as client:
        with pytest.raises(PermissionDenied):
            client.write(
                [PutSystemRecord("llm_call", new_id(), payload(caller_module="llm_gateway"))], SYS
            )
        assert client.count(Query("llm_call")) == 0
