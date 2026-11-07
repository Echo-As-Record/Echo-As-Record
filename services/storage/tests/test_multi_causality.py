"""Additive origins: legacy bytes, strict commands, complete causal execution paths."""

import json
from dataclasses import replace

import pytest
from ear_contracts import Cause, ReevaluationRequested, TracedCause, new_id
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    InvalidRequest,
    PermissionDenied,
    PublishReevaluation,
    Query,
    Update,
)
from ear_storage_client.wire import decode_request, encode_request
from pydantic import ValidationError

from .test_completion import SYS, deps, events, finish, mark
from .test_completion import setup as setup


def trace(source, *, root=None, depth=4, dtype="rule", old=None, new=1):
    return TracedCause(
        cause=Cause(data_type=dtype, data_id=source, old_version=old, new_version=new),
        root_feedback_id=root or new_id(),
        depth=depth,
    )


def publish_multi(store, target, source, origins, context=None):
    receipt = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic",
                    additional_causes=origins,
                )
            ],
            context or Context(new_id(), 2),
        )
        .check()
    )
    event = receipt.events[0]
    assert isinstance(event, ReevaluationRequested)
    return event


def test_model_compatibility_and_closed_trace_wire():
    a = trace(new_id())
    b = trace(a.cause.data_id)
    context = Context("system", 1, additional_causes=(a, a, b))
    assert context.additional_causes == (a, b)
    old = json.loads(encode_request([], SYS))
    assert old["context"] == {"root_feedback_id": "system", "depth": 0}
    assert decode_request(encode_request([], context))[1] == context
    for n in (0, -1, True):
        with pytest.raises(ValidationError):
            trace(new_id(), depth=n)
    with pytest.raises(ValidationError):
        Context("system", 1, additional_causes=(a,) * 257)
    forward = TracedCause.model_validate({**a.model_dump(), "future": 42})
    assert forward.model_extra == {"future": 42}
    with pytest.raises(ValueError):
        Context("system", additional_causes=(forward,))
    for scope in ("trace", "cause"):
        body = json.loads(encode_request([], context))
        entry = body["context"]["additional_causes"][0]
        (entry if scope == "trace" else entry["cause"])["payload"] = "private"
        with pytest.raises(InvalidRequest):
            decode_request(json.dumps(body))


@pytest.mark.parametrize("changed", [False, True])
def test_multi_root_finish_snapshot_unchanged_and_next_request(setup, changed):
    store, client, target, source, *_ = setup
    a, b = trace(source), trace(source, depth=8)
    request = publish_multi(store, target, source, (a, b))
    ctx = Context.of(request)
    assert ctx.request_event_id == request.event_id and ctx.additional_causes == (a, b)
    op = finish(
        request.event_id,
        target,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=(a, b),
        outcome="changed" if changed else "unchanged",
        after_version=2 if changed else 1,
    )
    before = len(events(store))
    with client.snapshot() as snapshot:
        receipt = client.write(
            [
                Update("artifact", target, 1, {"n": 2 if changed else 1}),
                deps(target),
                op,
                mark(request.event_id),
            ],
            ctx,
        ).check()
        assert snapshot.get_reevaluation_result(request.event_id) is None
    with client.snapshot() as snapshot:
        captured = snapshot.get_reevaluation_result(request.event_id)
        assert captured == op.result
        assert snapshot.get_reevaluation_result(request.event_id) == captured
    assert len(events(store)) == before + int(changed)
    record = client.get(target)
    assert record is not None and record.version == 1 + int(changed)
    if changed:
        assert record.request_event_id == request.event_id
        assert record.additional_causes == (a, b)
        change = receipt.events[0]
        downstream = Context.of(change).deeper()
        assert downstream.request_event_id == request.event_id
        assert [t.depth for t in downstream.additional_causes] == [5, 9]
        next_request = publish_multi(store, target, source, (), downstream)
        assert next_request.trigger_request_event_id == request.event_id
        assert next_request.event_id != request.event_id
        assert Context.of(next_request).request_event_id == next_request.event_id
        assert next_request.additional_causes == downstream.additional_causes
    else:
        assert receipt.events == ()
        assert "additional_causes" not in record.model_dump()
        assert "request_event_id" not in record.model_dump()


@pytest.mark.parametrize("loss", ["result", "context", "identity", "context_identity"])
def test_finish_cannot_drop_or_reassign_origins(setup, loss):
    store, client, target, source, *_ = setup
    a = trace(source)
    request = publish_multi(store, target, source, (a,))
    ctx = Context.of(request)
    op = finish(
        request.event_id,
        target,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=() if loss == "result" else (a,),
    )
    if loss == "context":
        ctx = replace(ctx, additional_causes=())
    if loss == "identity":
        ctx = replace(ctx, request_event_id=new_id())
    if loss == "context_identity":
        ctx = replace(ctx, request_event_id=None)
    with pytest.raises(InvalidRequest):
        client.write([deps(target), op, mark(request.event_id)], ctx)
    assert client.get_reevaluation_result(request.event_id) is None
    assert not client.is_processed(mark(request.event_id).consumer, request.event_id)


@pytest.mark.parametrize("bad", ["missing", "old_history", "type"])
def test_additional_typed_versions_required(setup, bad):
    store, _, target, source, *_ = setup
    a = trace(
        new_id() if bad == "missing" else source,
        old=99 if bad == "old_history" else None,
        dtype="person" if bad == "type" else "rule",
    )
    before = len(events(store))
    with pytest.raises(InvalidRequest):
        publish_multi(store, target, source, (a,))
    assert len(events(store)) == before


def test_actual_history_and_explicit_additions(setup):
    store, client, target, source, *_ = setup
    store.client("extraction").write([Update("rule", source, 1, {"n": 2})], SYS).check()
    a = trace(source, old=1, new=2)
    request = publish_multi(store, target, source, (a,))
    extra = trace(source, root=new_id())
    op = finish(
        request.event_id,
        target,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=(a, extra),
    )
    client.write(
        [deps(target), op, mark(request.event_id)],
        replace(Context.of(request), additional_causes=(a, extra)),
    ).check()
    assert client.get_reevaluation_result(request.event_id) == op.result


@pytest.mark.postgres
def test_secondary_revocation_write_replay_reads_and_snapshots(pg_dsn):
    from ear_storage import PostgresStorage, bootstrap, migrate
    from ear_storage.auth import AuthOperator

    from .test_completion import CONSUMER, capable_registry

    migrate(pg_dsn)
    bootstrap(pg_dsn, capable_registry())
    store = PostgresStorage(pg_dsn)
    operator = AuthOperator(pg_dsn)
    issued = operator.issue_principal(
        "extraction",
        read_types=["artifact", "rule", "person", "extraction_completion"],
        subscribe_types=["artifact"],
    )
    operator.reserve_consumer_identity(CONSUMER, issued.principal_id)
    client = StorageService(pg_dsn).client(issued.token)
    target, source, person = new_id(), new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {"n": 1}), Create("rule", source, {})], SYS
    ).check()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    a = trace(person, dtype="person")
    request = publish_multi(store, target, source, (a,))
    op = finish(
        request.event_id,
        target,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=(a,),
        outcome="changed",
        after_version=2,
    )
    ops = [Update("artifact", target, 1, {"n": 2}), deps(target), op, mark(request.event_id)]
    rid = new_id()
    client.write(ops, Context.of(request), request_id=rid).check()
    with client.snapshot() as snapshot:
        assert snapshot.get_reevaluation_result(request.event_id) == op.result
        operator.set_grants(
            issued.principal_id,
            read_types=["artifact", "rule", "extraction_completion"],
            subscribe_types=["artifact"],
        )
        for read in (
            lambda: client.get(target),
            lambda: client.get_reevaluation_result(request.event_id),
            lambda: client.get_system_record(request.event_id),
            lambda: snapshot.get_reevaluation_result(request.event_id),
        ):
            with pytest.raises(PermissionDenied):
                read()
        with pytest.raises(PermissionDenied):
            client.write(ops, Context.of(request), request_id=rid)
        assert client.count(Query("artifact")) == 0
        assert client.count(Query("extraction_completion")) == 0
        assert client.query(Query("artifact")).items == ()
        with client.snapshot() as hidden:
            assert hidden.get(target) is None
            assert hidden.get_reevaluation_result(request.event_id) is None
            assert hidden.count(Query("extraction_completion")) == 0
