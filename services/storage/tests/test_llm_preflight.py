"""Caller-authenticated preflight: synthetic data, memory and isolated real PG/HTTP."""

from contextlib import nullcontext
from datetime import timedelta
from typing import Any

import httpx
import pytest
from ear_contracts import Cause, LLMCallRecord, TracedCause, new_id, utcnow
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.http import create_app
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    InvalidRequest,
    LLMCallPreflight,
    MemoryStorage,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Query,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, ProtocolError
from ear_storage_client.testing import make_raw
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .test_completion import capable_registry, events
from .test_http import TestTransport

READS = {"artifact", "rule", "person", "feedback"}


def seed(store):
    target, source, person, root, extra_root = (new_id() for _ in range(5))
    store.client("extraction").write(
        [Create("artifact", target, {"private": "synthetic target"}), Create("rule", source, {})],
        Context.system(),
    ).check()
    store.client("attribution").write([Create("person", person, {})], Context.system()).check()
    for rid in (root, extra_root):
        store.client("api").write(
            [
                AppendRaw(
                    make_raw("feedback", {"private": "synthetic feedback"}, start=utcnow(), id=rid)
                )
            ],
            Context.system(),
        ).check()
    extra = TracedCause(
        cause=Cause(data_type="person", data_id=person, old_version=None, new_version=1),
        root_feedback_id=extra_root,
        depth=3,
    )
    request = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic change description must not be returned",
                    additional_causes=(extra,),
                )
            ],
            Context(root, 1),
        )
        .check()
        .events[0]
    )
    call = LLMCallPreflight(
        caller_module="extraction",
        request_event_id=request.event_id,
        root_feedback_id=root,
        depth=1,
        started_at=utcnow(),
        additional_causes=(extra,),
    )
    return call, request, target, source, person, root, extra_root


@pytest.fixture(params=["memory", pytest.param("http", marks=pytest.mark.postgres)])
def preflight(request):
    dsn = operator = issued = service = None
    if request.param == "memory":
        store = MemoryStorage(capable_registry())
        client_context = nullcontext(store.client("extraction"))
        server = nullcontext(None)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        migrate(dsn)
        bootstrap(dsn, capable_registry())
        store = PostgresStorage(dsn)
        operator = AuthOperator(dsn)
        issued = operator.issue_principal("extraction", read_types=READS, subscribe_types=[])
        service = StorageService(dsn)
        server = TestClient(create_app(service))
        client_context = None
    data = seed(store)
    with server as http:
        if client_context is None:
            assert issued is not None and http is not None
            client_context = RemoteStorageClient(
                issued.token, "http://synthetic.test", transport=TestTransport(http)
            )
        with client_context as client:
            yield store, client, data, operator, issued, dsn, service


def test_canonical_metadata_only_no_side_effects(preflight, monkeypatch):
    from ear_storage.management import ManagementProcessor

    def forbidden(*args, **kwargs):
        pytest.fail("preflight must not reconcile management state")

    monkeypatch.setattr(ManagementProcessor, "reconcile", forbidden)
    store, client, data, _, _, dsn, _ = preflight
    call, request, *_ = data
    before = events(store)
    counts = None
    state = store._state if isinstance(store, MemoryStorage) else store.commit_seq
    if dsn:
        with connection(dsn) as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM storage.idempotency), "
                "(SELECT count(*) FROM storage.request_completions), "
                "(SELECT count(*) FROM storage.processed)"
            ).fetchone()
    result = client.preflight_llm_call(call)
    assert result != call  # Canonical primary path was added, request object untouched.
    assert len(call.additional_causes) == 1
    assert len(result.additional_causes) == 2
    assert request.additional_causes[0] in result.additional_causes
    assert any(t.cause == request.cause for t in result.additional_causes)
    assert "private" not in result.model_dump_json()
    assert "change_description" not in result.model_dump_json()
    assert client.preflight_llm_call(result) == result
    assert events(store) == before
    assert (store._state if isinstance(store, MemoryStorage) else store.commit_seq) == state
    assert store.client("llm_gateway").count(Query("llm_call")) == 0
    assert client.get_reevaluation_result(call.request_event_id) is None
    if dsn:
        with connection(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT (SELECT count(*) FROM storage.idempotency), "
                    "(SELECT count(*) FROM storage.request_completions), "
                    "(SELECT count(*) FROM storage.processed)"
                ).fetchone()
                == counts
            )
    # Preflight and actual audit persistence use the SAME causal canonicalization.
    record = LLMCallRecord(
        **call.model_dump(), purpose="synthetic_test", ended_at=utcnow(), status="success"
    )
    rid = new_id()
    store.client("llm_gateway").write(
        [PutSystemRecord("llm_call", rid, record.model_dump(mode="json"))],
        Context(call.root_feedback_id, call.depth),
    ).check()
    saved = store.client("llm_gateway").get_system_record(rid)
    assert saved is not None
    assert LLMCallRecord.model_validate(saved.payload).additional_causes == result.additional_causes


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "data_changed",
        "root",
        "depth",
        "drop",
        "predates",
        "future",
        "extra_root_missing",
        "extra_root_wrong_type",
        "cause_missing",
        "cause_type",
        "cause_old_version",
        "cause_new_version",
        "caller",
        "owner",
    ],
)
def test_invalid_preflight_never_writes(preflight, bad):
    store, client, data, *_ = preflight
    call, event, target, source, _, _, _ = data
    updates: dict[str, Any] = {}
    if bad == "missing":
        updates["request_event_id"] = new_id()
    elif bad == "data_changed":
        updates["request_event_id"] = events(store)[0].event_id
    elif bad == "root":
        updates["root_feedback_id"] = "system"
    elif bad == "depth":
        updates["depth"] = 2
    elif bad == "drop":
        updates["additional_causes"] = ()
    elif bad == "predates":
        updates["started_at"] = event.occurred_at - timedelta(microseconds=1)
    elif bad == "future":
        updates["started_at"] = utcnow() + timedelta(days=1)
    elif bad == "caller":
        updates["caller_module"] = "llm_gateway"
    elif bad == "owner":
        owned = (
            store.client("dependency")
            .write(
                [PublishReevaluation("person", data[4], event.cause, "synthetic")],
                Context(call.root_feedback_id, 1),
            )
            .check()
            .events[0]
        )
        updates["request_event_id"] = owned.event_id
        updates["started_at"] = utcnow()
    else:
        cause = {"data_type": "rule", "data_id": source, "old_version": None, "new_version": 1}
        root = "system"
        if bad == "extra_root_missing":
            root = new_id()
        elif bad == "extra_root_wrong_type":
            root = target
        elif bad == "cause_missing":
            cause["data_id"] = new_id()
        elif bad == "cause_type":
            cause["data_type"] = "person"
        elif bad == "cause_old_version":
            cause["old_version"] = 99
        elif bad == "cause_new_version":
            cause["new_version"] = 99
        extra = TracedCause(cause=Cause.model_validate(cause), root_feedback_id=root, depth=2)
        updates["additional_causes"] = (*call.additional_causes, extra)
    candidate = LLMCallPreflight.model_validate({**call.model_dump(), **updates})
    before = events(store)
    with pytest.raises((InvalidRequest, PermissionDenied)):
        client.preflight_llm_call(candidate)
    assert events(store) == before
    assert store.client("llm_gateway").count(Query("llm_call")) == 0


def test_legacy_request_invalid_primary_is_rejected(preflight):
    store, client, data, *_ = preflight
    call, _, target, *_ = data
    # Legacy Publish accepts a single-cause unverified version; call preflight must not.
    request = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=new_id(), old_version=None, new_version=1),
                    "synthetic",
                )
            ],
            Context("system", 1),
        )
        .check()
        .events[0]
    )
    candidate = LLMCallPreflight.model_validate(
        {
            **call.model_dump(),
            "request_event_id": request.event_id,
            "root_feedback_id": "system",
            "additional_causes": (),
            "started_at": utcnow(),
        }
    )
    with pytest.raises(InvalidRequest):
        client.preflight_llm_call(candidate)


@pytest.mark.parametrize(
    "revoked", ["artifact", "rule", "person", "feedback", "identity", "disabled"]
)
def test_current_auth_and_all_grants(preflight, revoked):
    _, client, data, operator, issued, dsn, service = preflight
    if operator is None:
        pytest.skip("requires authenticated PostgreSQL")
    call, *_ = data
    assert client.preflight_llm_call(call)
    # Neither result nor llm_call read grants were needed to run the live preflight.
    if revoked == "identity":
        operator.revoke(issued.principal_id)
    elif revoked == "disabled":
        from ear_storage.schema import writer

        with connection(dsn) as conn, writer(conn):
            conn.execute(
                "INSERT INTO storage.module_lifecycle (module,enabled,resources_ready) "
                "VALUES ('extraction',false,false) ON CONFLICT (module) "
                "DO UPDATE SET enabled=false,resources_ready=false"
            )
    else:
        operator.set_grants(issued.principal_id, read_types=READS - {revoked}, subscribe_types=[])
    error = AuthenticationError if revoked in {"identity", "disabled"} else PermissionDenied
    with pytest.raises(error):
        client.preflight_llm_call(call)
    if revoked not in {"identity", "disabled"}:
        # A previously constructed service client also reauthenticates every call.
        with pytest.raises(PermissionDenied):
            service.client(issued.token).preflight_llm_call(call)


@pytest.mark.parametrize(
    "bad", ["body", "cause_body", "trace_body", "bool_version", "numeric_time", "snapshot"]
)
def test_closed_preflight_wire(bad):
    store = MemoryStorage(capable_registry())
    call, *_ = seed(store)
    data = call.model_dump(mode="json")
    if bad == "body":
        data["request_text"] = "SECRET_SYNTHETIC"
    elif bad == "snapshot":
        data["snapshot_id"] = new_id()
    elif bad == "cause_body":
        data["additional_causes"][0]["cause"]["prompt"] = "SECRET_SYNTHETIC"
    elif bad == "trace_body":
        data["additional_causes"][0]["token"] = "SECRET_SYNTHETIC"
    elif bad == "bool_version":
        data["additional_causes"][0]["cause"]["new_version"] = True
    else:
        data["started_at"] = 1
    with pytest.raises(ValidationError):
        LLMCallPreflight.model_validate(data)


def test_sdk_no_retry_sanitized_mismatched_response():
    call, *_ = seed(MemoryStorage(capable_registry()))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**call.model_dump(mode="json"), "caller_module": "api"})

    with (
        RemoteStorageClient(
            "t" * 43, "http://synthetic.test", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="invalid preflight response"),
    ):
        client.preflight_llm_call(call)
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/llm/preflight"
    assert seen[0].headers["authorization"] == "Bearer " + "t" * 43
