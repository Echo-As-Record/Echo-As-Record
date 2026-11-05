"""One terminal result per persisted request: memory AND real PG/HTTP paths."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataTypeSpec,
    ReevaluationResult,
    Registry,
    SemanticRecord,
    new_id,
    utcnow,
)
from ear_storage import PostgresStorage, SchemaMismatch, bootstrap, migrate, schema
from ear_storage.auth import AuthOperator
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    AssertTypeRevision,
    Context,
    Create,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    MemoryStorage,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
    reevaluation_result,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.wire import decode_request, encode_request
from pydantic import ValidationError

from .test_http_postgres import live_server

SYS = Context.system()
CTX = SYS.deeper()
CONSUMER = "extraction.completion"


def capable_registry():
    # Trusted TEST configuration only; no product capability flags are changed.
    types = dict(DEFAULT_REGISTRY.data_types)
    types["artifact"] = replace(types["artifact"], supports_reevaluation=True)
    return Registry(types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types)


@pytest.fixture(params=["memory", pytest.param("http", marks=pytest.mark.postgres)])
def setup(request):
    registry = capable_registry()
    operator = issued = dsn = None
    if request.param == "memory":
        store = MemoryStorage(registry)
        client = store.client("extraction")
        server = nullcontext(None)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        migrate(dsn)
        bootstrap(dsn, registry)
        store = PostgresStorage(dsn)
        service, operator = StorageService(dsn), AuthOperator(dsn)
        issued = operator.issue_principal(
            "extraction",
            read_types=["artifact", "rule", "extraction_completion"],
            subscribe_types=["artifact"],
        )
        operator.reserve_consumer_identity(CONSUMER, issued.principal_id)
        server = live_server(service)
        client = None
    target, source = new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {"n": 1}), Create("rule", source, {"n": 1})], SYS
    ).check()
    with server as url:
        if client is not None:
            remote = nullcontext(client)
        else:
            assert issued is not None and url is not None
            remote = RemoteStorageClient(issued.token, url)
        with remote as client:
            yield store, client, target, source, operator, issued, dsn


def publish(store, target, source):
    result = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic input changed",
                    event_id=new_id(),
                )
            ],
            CTX,
        )
        .check()
    )
    return result.events[0].event_id


def finish(event, target, **updates):
    fields = dict(
        request_event_id=event,
        target_type="artifact",
        target_id=target,
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id="system",
        depth=1,
        completed_at=utcnow(),
    )
    fields.update(updates)
    return FinishReevaluation(ReevaluationResult.model_validate(fields))


def deps(target):
    return RegisterDependencies("artifact", target, [])


def mark(event):
    return MarkProcessed(CONSUMER, event)


def events(store):
    if isinstance(store, MemoryStorage):
        return store.events
    return tuple(e for _, group in store.outbox() for e in group)


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("finish_first", [False, True])
def test_atomic_result_deps_marker_no_third_event(setup, changed, finish_first):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    before_events = len(events(store))
    op = finish(
        event,
        target,
        outcome="changed" if changed else "unchanged",
        after_version=2 if changed else 1,
    )
    ops = [Update("artifact", target, 1, {"n": 2 if changed else 1}), deps(target), mark(event)]
    ops.insert(0 if finish_first else len(ops), op)
    with client.snapshot() as snap:
        assert reevaluation_result(snap, event) is None
        receipt = client.write(ops, CTX).check()
        assert reevaluation_result(snap, event) is None
        assert snap.type_revision("extraction_completion") == 0
    result = reevaluation_result(client, event)
    assert result == op.result
    with client.snapshot() as captured:
        assert captured.get_reevaluation_result(event) == op.result
    assert receipt.events == () if not changed else len(receipt.events) == 1
    assert len(events(store)) == before_events + int(changed)
    assert client.is_processed(CONSUMER, event)
    group = client.dependency_set(target)
    assert group is not None and group.dependencies == ()
    assert group.dependent.version == (2 if changed else 1)
    assert client.type_revision("extraction_completion") == 1
    assert len([e for e in receipt.events if e.event_type == "data_changed"]) == int(changed)
    duplicate = client.write([deps(target), op, mark(event)], CTX)
    assert duplicate.conflict and duplicate.conflict.reason == "already_processed"


@pytest.mark.parametrize(
    "bad",
    [
        "missing_deps",
        "missing_marker",
        "wrong_root",
        "wrong_depth",
        "wrong_target",
        "fake_request",
        "before_history",
        "after_history",
        "changed_no_write",
        "unchanged_new_version",
        "bad_extra_cause",
        "ordinary_put",
    ],
)
def test_invalid_completions_roll_back_all_effects(setup, bad):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    changes = {}
    ops: list[WriteOp] = [deps(target)]
    if bad == "missing_deps":
        ops = []
    if bad == "wrong_root":
        changes["root_feedback_id"] = new_id()
    if bad == "wrong_depth":
        changes["depth"] = 2
    if bad == "wrong_target":
        changes["target_id"] = source
    if bad == "fake_request":
        changes["request_event_id"] = new_id()
    if bad == "before_history":
        changes.update(before_version=2, after_version=2)
    if bad == "after_history":
        changes.update(outcome="changed", after_version=3)
        ops.insert(0, Update("artifact", target, 1, {"n": 2}))
    if bad == "changed_no_write":
        changes.update(outcome="changed", after_version=2)
    if bad == "unchanged_new_version":
        ops.insert(0, Update("artifact", target, 1, {"n": 2}))
    if bad == "bad_extra_cause":
        changes["extra_causes"] = [
            Cause(data_type="rule", data_id=source, old_version=1, new_version=99)
        ]
    op = finish(event, target, **changes)
    ops.append(
        op
        if bad != "ordinary_put"
        else PutSystemRecord("extraction_completion", event, op.result.model_dump(mode="json"))
    )
    if bad != "missing_marker":
        ops.append(mark(event))
    prior = events(store)
    if bad == "before_history":
        receipt = client.write(ops, CTX)
        assert receipt.conflict and receipt.conflict.reason == "version_mismatch"
    else:
        with pytest.raises(InvalidRequest):
            client.write(ops, CTX)
    assert events(store) == prior and client.get(target).version == 1
    assert client.get_system_record(event) is None and client.dependency_set(target) is None
    assert not client.is_processed(CONSUMER, event)
    assert (
        client.type_revision("artifact") == 1 and client.type_revision("extraction_completion") == 0
    )


def test_terminal_failure_new_request_supersede_preserves_history(setup):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    failed = finish(event, target, outcome="failed", error_code="model_attempts_exhausted")
    receipt = client.write([failed, mark(event)], CTX).check()
    assert not receipt.events and client.get(target).version == 1
    assert client.dependency_set(target) is None
    saved = reevaluation_result(client, event)
    assert saved is not None and saved.error_code == "model_attempts_exhausted"
    with pytest.raises(InvalidRequest):
        client.write([PutSystemRecord("extraction_completion", event, {}, 1)], CTX)
    duplicate = client.write([finish(event, target), deps(target), mark(event)], CTX)
    assert duplicate.conflict and duplicate.conflict.reason == "already_processed"
    next_event = publish(store, target, source)
    client.write(
        [
            Supersede("artifact", target, 1),
            finish(next_event, target, outcome="changed", after_version=2),
            mark(next_event),
        ],
        CTX,
    ).check()
    current = client.get(target)
    assert isinstance(current, SemanticRecord) and current.superseded and current.version == 2
    assert client.get_version(target, 1).status == "active"
    assert reevaluation_result(client, event) == failed.result
    saved = reevaluation_result(client, next_event)
    assert saved is not None and saved.outcome == "changed"
    third = publish(store, target, source)
    with pytest.raises(InvalidRequest):
        client.write([finish(third, target, before_version=2, after_version=2), mark(third)], CTX)


@pytest.mark.parametrize("disposition", ["kept_protected", "terminated", "unavailable"])
def test_not_applicable_explicit_disposition_not_implicit_success(setup, disposition):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    ops = [Supersede("artifact", target, 1)] if disposition == "terminated" else []
    ops += [
        finish(
            event,
            target,
            outcome="not_applicable",
            reason_code="owner_decision",
            disposition=disposition,
            after_version=2 if ops else 1,
        ),
        mark(event),
    ]
    client.write(ops, CTX).check()
    result = reevaluation_result(client, event)
    assert result is not None
    assert result.outcome == "not_applicable" and result.disposition == disposition
    assert client.get(target).version == (2 if disposition == "terminated" else 1)
    assert client.dependency_set(target) is None  # not asserted to be refreshed/fresh


def test_failed_no_output_and_guard_atomic_rollback(setup):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    failed = finish(event, target, outcome="failed", error_code="unrecoverable")
    with pytest.raises(InvalidRequest):
        client.write([Update("artifact", target, 1, {"n": 1}), failed, mark(event)], CTX)
    receipt = client.write(
        [
            finish(event, target),
            deps(target),
            mark(event),
            AssertTypeRevision("extraction_completion", 0),
        ],
        CTX,
    )
    assert receipt.conflict and receipt.conflict.reason == "collection_changed"
    assert client.get_system_record(event) is None and client.dependency_set(target) is None
    assert client.type_revision("extraction_completion") == 0
    assert not client.is_processed(CONSUMER, event)


def test_http_replay_reauthorization_cause_target_and_marker_owner(setup):
    store, client, target, source, operator, issued, dsn = setup
    if operator is None:
        return  # shared semantics above; this case exercises authenticated HTTP
    event = publish(store, target, source)
    person = new_id()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    operator.set_grants(
        issued.principal_id,
        read_types=["artifact", "rule", "person", "extraction_completion"],
        subscribe_types=["artifact"],
    )
    extra = Cause(data_type="person", data_id=person, old_version=None, new_version=1)
    ops = [deps(target), finish(event, target, extra_causes=[extra]), mark(event)]
    request = new_id()
    receipt = client.write(ops, CTX, request_id=request).check()
    assert client.write(ops, CTX, request_id=request) == receipt
    with connection(dsn) as conn:
        row = conn.execute(
            "SELECT required_types_text FROM storage.idempotency WHERE request_id=%s", (request,)
        ).fetchone()
        assert row is not None
        assert {"artifact", "rule", "person"} <= set(json.loads(row[0]))
        assert conn.execute("SELECT count(*) FROM storage.request_completions").fetchone() == (1,)
    operator.set_grants(
        issued.principal_id,
        read_types=["artifact", "rule", "extraction_completion"],
        subscribe_types=["artifact"],
    )
    with pytest.raises(PermissionDenied):
        client.write(ops, CTX, request_id=request)
    operator.set_grants(
        issued.principal_id, read_types=["artifact", "rule", "person"], subscribe_types=["artifact"]
    )
    assert reevaluation_result(client, event) is None  # completion read is NOT receipt metadata
    other = operator.issue_principal(
        "extraction", read_types=["artifact", "rule"], subscribe_types=["artifact"]
    )
    service = StorageService(dsn)
    with pytest.raises(PermissionDenied):
        service.client(other.token).write(ops, CTX)
    operator.set_grants(issued.principal_id, read_types=["rule"])
    with pytest.raises(PermissionDenied):
        client.write(ops, CTX, request_id=request)


def test_wrong_owner_and_consumer_cannot_finish(setup):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    with pytest.raises(PermissionDenied):
        store.client("attribution").write(
            [finish(event, target), MarkProcessed("attribution.completion", event)], CTX
        )
    with pytest.raises(PermissionDenied):
        client.write(
            [deps(target), finish(event, target), MarkProcessed("attribution.x", event)], CTX
        )
    assert store.client("extraction").get_system_record(event) is None


def test_wire_strict_model_dates_and_cause_forward_read():
    op = finish(new_id(), new_id())
    text = encode_request([op], CTX)
    assert decode_request(text)[0] == (op,)
    for key, value in [
        ("after_version", True),
        ("before_version", 1.0),
        ("depth", "1"),
        ("completed_at", "2026-11-05T00:00:00+00:00"),
        ("prompt", "private"),
    ]:
        body = json.loads(text)
        body["ops"][0]["body"]["result"][key] = value
        with pytest.raises(InvalidRequest):
            decode_request(json.dumps(body))
    for when in [datetime(2026, 11, 5), datetime(2026, 11, 5, tzinfo=UTC) + timedelta(0)]:
        fields = op.result.model_dump()
        fields["completed_at"] = when
        if when.tzinfo is None:
            with pytest.raises(ValidationError):
                ReevaluationResult.model_validate(fields)
        else:
            assert ReevaluationResult.model_validate(fields).completed_at.tzinfo == UTC
    cause = Cause.model_validate(
        dict(
            data_type="rule",
            data_id=new_id(),
            old_version=None,
            new_version=1,
            future_trace={"root_feedback_id": "system", "depth": 2},
        )
    )
    fields = op.result.model_dump()
    fields["extra_causes"] = [cause.model_dump()]
    assert ReevaluationResult.model_validate(fields).model_dump()["extra_causes"][0]["future_trace"]
    for module in ("attribution", "extraction"):
        spec = DEFAULT_REGISTRY.get(module + "_completion")
        assert spec.kind == "system_record" and spec.writer == module and not spec.emits_events
    from ear_storage.management_input import ManagementInput

    for explicit in (False, True):
        body = ManagementInput.model_validate(
            dict(
                request_id=new_id(),
                action="register_plugin",
                module="sample",
                types=[
                    dict(
                        name="sample_completion", kind="system_record", reevaluation_result=explicit
                    )
                ],
            )
        )
        assert body.types[0].spec("sample").reevaluation_result is explicit
        assert ("reevaluation_result" in body.payload()["types"][0]) is explicit
    assert not DEFAULT_REGISTRY.get("artifact").supports_reevaluation
    with pytest.raises(ValueError):
        DataTypeSpec("other_completion", "system_record", "sample", reevaluation_result=True)


@pytest.mark.postgres
def test_explicit_v9_to_v10_migration_preserves_request_history(pg_dsn, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 9)
        migrate(pg_dsn)
        registry = capable_registry()
        old_types = {k: v for k, v in registry.data_types.items() if not v.reevaluation_result}
        legacy = Registry(
            old_types, registry.event_publishers, frozenset(registry.core_types & old_types.keys())
        )
        legacy = legacy.extend(
            DataTypeSpec("sample_job_completion", "system_record", "sample"),
            DataTypeSpec("sample_completion", "system_record", "sample"),
            DataTypeSpec("sample_item", "semantic", "sample", supports_reevaluation=True),
        )
        bootstrap(pg_dsn, legacy)
        store = PostgresStorage(pg_dsn)
        target, source = new_id(), new_id()
        store.client("extraction").write(
            [Create("artifact", target, {}), Create("rule", source, {})], SYS
        ).check()
        event = publish(store, target, source)
        sample = store.client("sample")
        ordinary_ids = [new_id(), new_id()]
        for name, ident in zip(
            ("sample_job_completion", "sample_completion"), ordinary_ids, strict=True
        ):
            sample.write([PutSystemRecord(name, ident, {"legacy": True})], SYS).check()
        sample_target = new_id()
        sample.write([Create("sample_item", sample_target, {})], SYS).check()
        sample_event = new_id()
        store.client("dependency").write(
            [
                PublishReevaluation(
                    "sample_item",
                    sample_target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "legacy",
                    sample_event,
                )
            ],
            CTX,
        ).check()
        old_events = events(store)
        operator = AuthOperator(pg_dsn)
        issued = operator.issue_principal("extraction", read_types=["artifact", "rule"])
        lease = StorageService(pg_dsn).create_snapshot(issued.token)
        assert "extraction_completion" not in store.registry
    with pytest.raises(SchemaMismatch):
        PostgresStorage(pg_dsn)
    migrate(pg_dsn)
    migrate(pg_dsn)
    store = PostgresStorage(pg_dsn)
    assert events(store) == old_events
    assert "extraction_completion" not in store.registry  # migration is not bootstrap
    sample = store.client("sample")
    for name, ident in zip(
        ("sample_job_completion", "sample_completion"), ordinary_ids, strict=True
    ):
        assert not store.registry.get(name).reevaluation_result
        sample.write([PutSystemRecord(name, ident, {"legacy": False}, 1)], SYS).check()
        saved_ordinary = sample.get_system_record(ident)
        assert saved_ordinary is not None and saved_ordinary.revision == 2
    with pytest.raises(InvalidRequest, match="capability"):
        sample.write(
            [
                finish(sample_event, sample_target, target_type="sample_item"),
                RegisterDependencies("sample_item", sample_target, []),
                MarkProcessed("sample.legacy", sample_event),
            ],
            CTX,
        )
    assert sample.get_system_record(sample_event) is None
    assert not sample.is_processed("sample.legacy", sample_event)
    with pytest.raises(SchemaMismatch, match="registry bootstrap conflict"):
        bootstrap(
            pg_dsn,
            Registry(
                {
                    "sample_completion": DataTypeSpec(
                        "sample_completion", "system_record", "sample", reevaluation_result=True
                    )
                }
            ),
        )
    bootstrap(pg_dsn, capable_registry())  # explicit trusted additive registration
    bootstrap(pg_dsn, capable_registry())
    old = StorageService(pg_dsn).client(issued.token).read_snapshot(lease.snapshot_id)
    record = old.get(target)
    assert record is not None
    assert record.version == 1
    assert old.type_revision("artifact") == 1
    assert old.get_system_record(event) is None
    store.client("extraction").write(
        [deps(target), finish(event, target), mark(event)], CTX
    ).check()
    saved = reevaluation_result(store, event)
    assert saved is not None and saved.request_event_id == event
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (10,)
        conn.execute("DROP TABLE storage.request_completions")
    with pytest.raises(SchemaMismatch, match="request_completions"):
        PostgresStorage(pg_dsn)


@pytest.mark.parametrize(
    "bad",
    [
        "terminated_active",
        "protected_changes",
        "failed_wrong_current",
        "missing_request_marker",
        "predates_request",
        "fake_data_event",
    ],
)
def test_terminal_dispositions_and_persisted_event_kind(setup, bad):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    changes = {}
    ops: list[WriteOp] = []
    marker = mark(event)
    if bad == "terminated_active":
        changes.update(
            outcome="not_applicable", disposition="terminated", reason_code="invalidated"
        )
    elif bad == "protected_changes":
        ops.append(Update("artifact", target, 1, {"n": 2}))
        changes.update(
            outcome="not_applicable",
            disposition="kept_protected",
            reason_code="user_confirmed",
            after_version=2,
        )
    elif bad == "failed_wrong_current":
        changes.update(outcome="failed", error_code="exhausted", before_version=2, after_version=2)
    elif bad == "missing_request_marker":
        marker = mark(events(store)[0].event_id)  # valid event, but not THIS request
    elif bad == "predates_request":
        changes.update(completed_at="2020-01-01T00:00:00Z")
    else:
        changes.update(request_event_id=events(store)[0].event_id)
    ops += [finish(event, target, **changes), marker]
    if bad == "failed_wrong_current":
        receipt = client.write(ops, CTX)
        assert receipt.conflict and receipt.conflict.reason == "version_mismatch"
    else:
        with pytest.raises((InvalidRequest, PermissionDenied)):
            client.write(ops, CTX)
    assert client.get_system_record(event) is None
    assert client.get(target).version == 1


def test_memory_request_metadata_not_aliased_and_same_batch_publish_rejected():
    store = MemoryStorage(capable_registry())
    target, source = new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {}), Create("rule", source, {})], SYS
    ).check()
    event = publish(store, target, source)
    old_state = store._state
    old = old_state.requests[event]
    assert old.cause.model_extra is not None
    old.cause.model_extra["future"] = {"trace": [1]}
    copied = old_state.copy()
    copied_extra = copied.requests[event].cause.model_extra
    assert copied_extra is not None
    copied_extra["future"]["trace"].append(2)
    assert old.cause.model_extra["future"] == {"trace": [1]}
    # Returned event metadata is a separate object from the authoritative state.
    assert events(store)[-1] is not old
    with store.client("extraction").snapshot() as snap:
        store.client("extraction").write(
            [finish(event, target), deps(target), mark(event)], CTX
        ).check()
        assert snap.get_system_record(event) is None
    assert event not in old_state.completions
    # A trusted test registry grants publishing to extraction only to exercise
    # this invariant independently of normal publisher authorization.
    reg = capable_registry()
    store.registry = Registry(
        reg.data_types, {"reevaluation_requested": ("extraction",)}, reg.core_types
    )
    new_event = new_id()
    with pytest.raises(InvalidRequest, match="persisted"):
        store.client("extraction").write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic",
                    new_event,
                ),
                deps(target),
                finish(new_event, target),
                mark(new_event),
            ],
            CTX,
        )
    assert new_event not in store._state.requests


def test_concurrent_target_update_requires_recompute_not_terminal_failure(setup):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    candidate = finish(event, target)
    store.client("extraction").write([Update("artifact", target, 1, {"n": 2})], SYS).check()
    rejected = client.write([candidate, deps(target), mark(event)], CTX)
    assert rejected.conflict and rejected.conflict.reason == "version_mismatch"
    assert rejected.conflict.retryable
    assert client.get_system_record(event) is None and not client.is_processed(CONSUMER, event)
    client.write(
        [finish(event, target, before_version=2, after_version=2), deps(target), mark(event)], CTX
    ).check()


def test_capability_and_consumer_event_kind_rechecked(setup):
    store, client, target, source, operator, issued, dsn = setup
    event = publish(store, target, source)
    ops = [finish(event, target), deps(target), mark(event)]
    if operator is None:
        store.registry = DEFAULT_REGISTRY
        with pytest.raises(InvalidRequest, match="support"):
            client.write(ops, CTX)
        return
    from ear_storage_client.engine import canonical_json

    with connection(dsn) as conn:
        # A registered consumer belonging to the correct principal, but not
        # subscribed to reevaluation, cannot mark a completion as processed.
        cfg = {"event_types": ["data_changed"], "filters": ["ear.events.data_changed.artifact.*"]}
        conn.execute(
            "INSERT INTO storage.consumers "
            "(name,principal_id,module,generation,config_text,destination,"
            "creation_cursor,ready,enabled) "
            "VALUES (%s,%s,'extraction',1,%s,'test',0,true,true)",
            (CONSUMER, issued.principal_id, canonical_json(cfg)),
        )
        conn.execute(
            "UPDATE storage.outbox SET delivered_at=clock_timestamp() WHERE event_id=%s", (event,)
        )
    with pytest.raises(PermissionDenied):
        client.write(ops, CTX)
    assert client.get_system_record(event) is None


@pytest.mark.parametrize("marker_first", [False, True])
def test_old_processed_marker_is_conflict_not_a_completion(setup, marker_first):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    client.write([mark(event)], CTX).check()
    ops = [finish(event, target), deps(target)]
    ops.insert(0 if marker_first else len(ops), mark(event))
    receipt = client.write(ops, CTX)
    assert receipt.conflict and receipt.conflict.reason == "already_processed"
    assert client.get_system_record(event) is None
    assert client.dependency_set(target) is None
    assert client.type_revision("extraction_completion") == 0


def test_request_context_and_duplicate_event_keep_original_metadata(setup):
    store, client, target, source, *_ = setup
    event = publish(store, target, source)
    original = events(store)
    altered = Context(root_feedback_id=new_id(), depth=2)
    store.client("dependency").write(
        [
            PublishReevaluation(
                "rule",
                source,
                Cause(data_type="artifact", data_id=target, old_version=None, new_version=1),
                "duplicate must not replace authoritative request",
                event,
            )
        ],
        altered,
    ).check()
    assert events(store) == original
    with pytest.raises(InvalidRequest):
        client.write([finish(event, target), deps(target), mark(event)], altered)
    assert client.get_system_record(event) is None
    client.write([finish(event, target), deps(target), mark(event)], CTX).check()


def test_owner_authorized_before_existing_marker_conflict(setup):
    store, _, target, source, *_ = setup
    event = publish(store, target, source)
    alien = store.client("attribution")
    marker = MarkProcessed("attribution.old", event)
    alien.write([marker], CTX).check()
    with pytest.raises(PermissionDenied):
        alien.write([marker, finish(event, target)], CTX)
    assert store.client("extraction").get_system_record(event) is None
