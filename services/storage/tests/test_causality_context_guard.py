"""Explicit request association and predeclared execution origins, on memory and HTTP/PG."""

import json
from dataclasses import replace

import pytest
from ear_contracts import ReevaluationRequested, new_id
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    InvalidRequest,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Update,
    WriteOp,
)
from ear_storage_client.wire import decode_request, encode_request

from .test_completion import SYS, deps, events, finish, mark
from .test_completion import setup as setup
from .test_multi_causality import publish_multi, trace


@pytest.mark.parametrize("mode", ["missing", "data_changed", "root", "depth", "drop"])
def test_context_association_rejected_before_business_write(setup, mode):
    store, client, target, source, *_ = setup
    a = trace(source)
    req = publish_multi(store, target, source, (a,))
    ctx = Context.of(req)
    if mode == "missing":
        ctx = replace(ctx, request_event_id=new_id())
    elif mode == "data_changed":
        ctx = replace(ctx, request_event_id=events(store)[0].event_id)
    elif mode == "root":
        ctx = replace(ctx, root_feedback_id=new_id())
    elif mode == "depth":
        ctx = ctx.deeper()
    else:
        ctx = replace(ctx, additional_causes=())
    before = events(store)
    output = new_id()
    ops, ctx = decode_request(encode_request([Create("artifact", output, {})], ctx))
    with pytest.raises(InvalidRequest):
        client.write(ops, ctx)
    assert client.get(output) is None
    assert events(store) == before


@pytest.mark.parametrize(
    "mode", ["self", "committed_self", "same_batch", "wrong_depth", "wrong_root"]
)
def test_publish_association_requires_committed_upstream(setup, mode):
    store, _, target, source, *_ = setup
    req = publish_multi(store, target, source, ())
    event_id = new_id()
    ctx = Context.of(req).deeper()
    if mode == "committed_self":
        event_id = req.event_id
    if mode in {"self", "same_batch"}:
        ctx = replace(ctx, request_event_id=event_id)
    elif mode == "wrong_depth":
        ctx = Context.of(req)
    elif mode == "wrong_root":
        ctx = replace(ctx, root_feedback_id=new_id())
    op = PublishReevaluation("artifact", target, req.cause, "synthetic", event_id=event_id)
    ops: list[WriteOp] = [op]
    if mode == "same_batch":
        ops.append(PutSystemRecord("propagation_record", new_id(), {}))
    before = events(store)
    with pytest.raises(InvalidRequest):
        store.client("dependency").write(ops, ctx)
    assert events(store) == before


def test_foreign_request_cannot_label_business_execution(setup):
    store, _, target, source, *_ = setup
    req = publish_multi(store, target, source, ())
    with pytest.raises(PermissionDenied):
        store.client("attribution").write([Create("person", new_id(), {})], Context.of(req))


@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("finish_first", [False, True])
def test_added_origin_must_be_declared_before_all_output(setup, merge, finish_first):
    store, client, target, source, *_ = setup
    a, b = trace(source), trace(source)
    req = publish_multi(store, target, source, (a,))
    ctx = replace(Context.of(req), additional_causes=(b, a, b)) if merge else Context.of(req)
    op = finish(
        req.event_id,
        target,
        root_feedback_id=req.root_feedback_id,
        depth=req.depth,
        outcome="changed",
        after_version=2,
        additional_causes=(a, b),
    )
    output = new_id()
    ops = [
        Update("artifact", target, 1, {"n": 2}),
        Create("artifact", output, {}),
        deps(target),
        mark(req.event_id),
    ]
    ops.insert(0 if finish_first else len(ops), op)
    before = events(store)
    if not merge:
        with pytest.raises(InvalidRequest):
            client.write(ops, ctx)
        assert events(store) == before
        assert client.get(target).version == 1
        assert client.get(output) is None
        assert client.get_reevaluation_result(req.event_id) is None
        assert not client.is_processed(mark(req.event_id).consumer, req.event_id)
        return
    receipt = client.write(ops, ctx).check()
    expected = Context("system", additional_causes=(a, b)).additional_causes
    for id in (target, output):
        record = client.get(id)
        assert record.additional_causes == expected
        assert record.request_event_id == req.event_id
        assert (record.root_feedback_id, record.depth) == (req.root_feedback_id, req.depth)
    assert all(e.additional_causes == expected for e in receipt.events)
    with client.snapshot() as snap:
        assert snap.get_reevaluation_result(req.event_id).additional_causes == expected
    downstream = Context.of(receipt.events[0]).deeper()
    nxt = publish_multi(store, target, source, (), downstream)
    assert isinstance(nxt, ReevaluationRequested)
    assert nxt.trigger_request_event_id == req.event_id
    assert nxt.additional_causes == downstream.additional_causes
    assert [t.depth for t in nxt.additional_causes] == [t.depth + 1 for t in expected]


@pytest.mark.postgres
@pytest.mark.parametrize("denied", ["target", "primary", "secondary"])
def test_association_authorization_and_replay_without_finish(setup, denied):
    store, _, target, source, operator, issued, dsn = setup
    if operator is None:
        pytest.skip("requires authenticated PostgreSQL")
    person = new_id()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    operator.set_grants(
        issued.principal_id, read_types=["artifact", "rule", "person", "extraction_completion"]
    )
    req = publish_multi(store, target, source, (trace(person, dtype="person"),))
    client = StorageService(dsn).client(issued.token)
    ctx = Context.of(req)
    rid, output = new_id(), new_id()
    ops = [Create("rule", output, {})]
    client.write(ops, ctx, request_id=rid).check()
    denied_type = {"target": "artifact", "primary": "rule", "secondary": "person"}[denied]
    operator.set_grants(
        issued.principal_id,
        read_types=[
            t for t in ["artifact", "rule", "person", "extraction_completion"] if t != denied_type
        ],
    )
    for replay in (rid, new_id()):
        with pytest.raises(PermissionDenied, match="access denied"):
            client.write(ops, ctx, request_id=replay)


@pytest.mark.postgres
def test_new_context_origin_permission_and_source_metadata_precedence(setup):
    from ear_storage.schema import connection

    store, client, target, source, operator, _issued, dsn = setup
    if operator is None:
        pytest.skip("requires authenticated PostgreSQL")
    person = new_id()
    store.client("attribution").write([Create("person", person, {})], SYS).check()
    req = publish_multi(store, target, source, ())
    ctx = replace(Context.of(req), additional_causes=(trace(person, dtype="person"),))
    before = events(store)
    with pytest.raises(PermissionDenied):
        client.write([Update("artifact", target, 1, {"n": 2})], ctx)
    assert events(store) == before
    assert client.get(target).version == 1
    # A malformed unreadable source must not reveal model-validation details.
    with connection(dsn) as conn:
        metadata = req.model_dump(mode="json")
        metadata.update(target_type="person", target_id=person, depth=-1)
        conn.execute(
            "UPDATE storage.outbox SET event_text=%s WHERE event_id=%s",
            (json.dumps(metadata), req.event_id),
        )
    with pytest.raises(PermissionDenied):
        client.write([Create("artifact", new_id(), {})], Context.of(req))


@pytest.mark.postgres
def test_dependency_can_read_foreign_upstream_and_publish(setup):
    store, client, target, source, operator, _, dsn = setup
    if operator is None:
        pytest.skip("requires authenticated PostgreSQL")
    req = publish_multi(store, target, source, ())
    receipt = client.write([Update("artifact", target, 1, {"n": 2})], Context.of(req)).check()
    issued = operator.issue_principal("dependency", read_types=["artifact", "rule"])
    publisher = StorageService(dsn).client(issued.token)
    ctx = Context.of(receipt.events[0]).deeper()
    op = PublishReevaluation("artifact", target, req.cause, "synthetic")
    nxt = publisher.write([op], ctx).check().events[0]
    assert isinstance(nxt, ReevaluationRequested)
    assert nxt.trigger_request_event_id == req.event_id
    assert (nxt.root_feedback_id, nxt.depth) == (req.root_feedback_id, req.depth + 1)
    operator.set_grants(issued.principal_id, read_types=["rule"])
    with pytest.raises(PermissionDenied):
        publisher.write([op], ctx)
