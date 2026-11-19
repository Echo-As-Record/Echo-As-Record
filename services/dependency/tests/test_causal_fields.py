"""Causal field admission is separate from current staleness and DAG barriers."""

from contextlib import contextmanager
from typing import cast

import pytest
from ear_contracts import (
    Cause,
    DataChanged,
    ReevaluationResult,
    SemanticRecord,
    TracedCause,
    VersionRef,
    new_id,
    utcnow,
)
from ear_dependency import RemoteDependencyService
from ear_storage_client import (
    Context,
    DependencyInput,
    FinishReevaluation,
    MarkProcessed,
    PermissionDenied,
    RegisterDependencies,
    Update,
    WriteOp,
)

from .fixtures import World
from .test_remote import FakePort, remote


def write_payload(w, name, payload, *, skip=False, root="system", additional=()):
    current = w.client.get(w.ids[name])
    assert current is not None
    deps = []
    for edge in w.client.dependencies_of(current.id):
        source = w.client.get(edge.depends_on.id)
        assert source is not None
        deps.append(
            DependencyInput(
                VersionRef(data_type=source.data_type, id=source.id, version=source.version),
                edge.fields,
            )
        )
    ops: list[WriteOp] = [
        Update(current.data_type, current.id, current.version, payload, skip_history=skip)
    ]
    if w.client.registry.data_types[current.data_type].requires_dependencies:
        ops.append(RegisterDependencies(current.data_type, current.id, deps))
    owner = w.storage.client(w.client.registry.writer_of(current.data_type))
    result = owner.write(ops, Context(root, additional_causes=tuple(additional))).check()
    event = result.events[0]
    w.service.handle(event)
    return event


def finish_payload(w, name, payload):
    request = w.requests(name)[-1]
    current = w.client.get(w.ids[name])
    assert isinstance(current, SemanticRecord)
    owner = w.storage.client(w.client.registry.writer_of(current.data_type))
    deps = []
    for edge in w.client.dependencies_of(current.id):
        source = w.client.get(edge.depends_on.id)
        assert source is not None
        deps.append(
            DependencyInput(
                VersionRef(data_type=source.data_type, id=source.id, version=source.version),
                edge.fields,
            )
        )
    result = ReevaluationResult(
        request_event_id=request.event_id,
        target_type=current.data_type,
        target_id=current.id,
        outcome="changed",
        before_version=current.version,
        after_version=current.version + 1,
        root_feedback_id=request.root_feedback_id,
        depth=request.depth,
        additional_causes=request.additional_causes,
        completed_at=utcnow(),
    )
    owner.write(
        [
            Update(current.data_type, current.id, current.version, payload),
            RegisterDependencies(current.data_type, current.id, deps),
            FinishReevaluation(result),
            MarkProcessed(owner.module + ".causal-test", request.event_id),
        ],
        Context.of(request),
    ).check()


@pytest.mark.parametrize("backend", ["memory", "remote"])
def test_gated_reference_debt_is_not_name_change(backend, tmp_path):
    w = World()
    journal = None
    if backend == "remote":
        journal = remote(w, FakePort(w.client), tmp_path / "journal")
        cast(RemoteDependencyService, w.service).subscribe()
    try:
        w.seed("person")
        w.node("refs", ["person"], fields=("references",))
        w.node("name", ["person"], "plugin_b", fields=("name",))
        w.node("whole", ["person"], "plugin_c")
        w.node("tail", ["refs"], "plugin_internal")
        w.change("person", skip=True, payload={"name": "person", "references": [1]})
        w.ticks()
        event = w.change("person", payload={"name": "renamed", "references": [1]})
        w.ticks()
        assert not w.requests("refs") and not w.requests("tail")
        assert len(w.requests("name")) == len(w.requests("whole")) == 1
        assert w.client.stale_dependencies([w.ids["person"]])  # not debt forgiveness
        w.service.handle(event)
        w.ticks()
        assert len(w.requests()) == 2
        w.finish("name", "unchanged")
        w.finish("whole", "unchanged")
        w.ticks()
        # A subsequent explicitly allowed reference change still traverses the chain.
        w.change("person", payload={"name": "renamed", "references": [2]})
        w.ticks()
        assert len(w.requests("refs")) == 1
        assert not w.requests("tail")
        w.finish("refs", "changed")
        w.ticks()
        assert len(w.requests("tail")) == 1
    finally:
        if journal:
            journal.close()


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ({}, {"references": None}, True),
        ({"references": None}, {}, True),
        ({"references": [1, 2]}, {"references": [2, 1]}, True),
        ({"references": {"a": 1, "b": 2}}, {"references": {"b": 2, "a": 1}}, False),
        ({"references": True}, {"references": 1}, True),
        ({"references": 1}, {"references": 1.0}, True),
        ({"references": [True]}, {"references": [1]}, True),
        ({"references": [1]}, {"references": [1]}, False),
    ],
)
def test_cause_fields_use_complete_canonical_json(before, after, expected):
    w = World()
    w.node("x", [])
    write_payload(w, "x", {"references": {"registered": True}}, skip=True)
    # The registered version is intentionally stale before the allowed event.
    w.node("target", ["x"], fields=("references",))
    write_payload(w, "x", {"name": "gated", **before}, skip=True)
    write_payload(w, "x", {"name": "allowed", **after})
    w.ticks()
    assert bool(w.requests()) is expected


def test_causal_change_and_current_stale_are_both_required():
    w = World()
    w.seed("x")
    w.node("fields", ["x"], fields=("references",))
    w.node("whole", ["x"], "plugin_b")
    w.change("x", payload={"name": "x", "references": [1]})
    w.change("x", payload={"name": "x", "references": []})
    w.ticks()
    assert not w.requests("fields")
    assert len(w.requests("whole")) == 1
    w.change("x", terminate=True)
    w.ticks()
    assert len(w.requests("fields")) == 1


@pytest.mark.parametrize("changed_field", ["name", "references"])
def test_derived_finish_uses_its_actual_before_after(changed_field):
    w = World()
    w.seed("x")
    w.node("up", ["x"])
    write_payload(w, "up", {"value": 0, "name": "before", "references": []}, skip=True)
    w.node("refs", ["up"], "plugin_b", fields=("references",))
    w.node("name", ["up"], "plugin_c", fields=("name",))
    w.node("tail", ["refs"], "plugin_internal")
    write_payload(w, "up", {"value": 0, "name": "before", "references": [1]}, skip=True)
    root = w.feedback()
    w.change("x", root=root)
    w.ticks()
    assert not w.requests("refs") and not w.requests("name")
    finish_payload(
        w,
        "up",
        {
            "value": 0,
            "name": "after" if changed_field == "name" else "before",
            "references": [2] if changed_field == "references" else [1],
        },
    )
    w.rebuild()
    w.ticks(5)
    assert bool(w.requests("refs")) is (changed_field == "references")
    assert bool(w.requests("name")) is (changed_field == "name")
    assert not w.requests("tail")
    if changed_field == "references":
        request = w.requests("refs")[0]
        assert request.root_feedback_id == root
        assert request.trigger_request_event_id == w.requests("up")[0].event_id
        assert request.cause.old_version == 3 and request.cause.new_version == 4
        w.finish("refs", "changed")
        w.ticks(5)
        assert len(w.requests("tail")) == 1


@pytest.mark.parametrize("extra_changes_refs", [False, True])
def test_event_additional_cause_is_checked_on_its_own_edge(extra_changes_refs):
    w = World()
    w.seed("x")
    w.seed("y")
    w.node("target", ["x", "y"], fields=("references",))
    w.change("x", skip=True, payload={"name": "x", "references": [1]})
    old = w.change("y", skip=True, payload={"name": "y", "references": [10]})
    y_event = w.change(
        "y",
        skip=True,
        payload={
            "name": "changed",
            "references": [20] if extra_changes_refs else [10],
        },
    )
    rx, ry = w.feedback(), w.feedback()
    extra = TracedCause(
        cause=Cause(
            data_type="person",
            data_id=w.ids["y"],
            old_version=old.new_version,
            new_version=y_event.new_version,
        ),
        root_feedback_id=ry,
        depth=1,
    )
    write_payload(w, "x", {"name": "renamed", "references": [1]}, root=rx, additional=[extra])
    w.ticks()
    assert bool(w.requests()) is extra_changes_refs
    if extra_changes_refs:
        request = w.requests()[0]
        assert {
            request.root_feedback_id,
            *(t.root_feedback_id for t in request.additional_causes),
        } == {rx, ry}


def test_fresh_edge_still_waits_for_relevant_upstream():
    w = World()
    w.seed("x")
    w.seed("y")
    w.node("up", ["x"])
    w.node("join", ["up", "y"], "plugin_b")
    w.node("tail", ["join"], "plugin_c")
    rx, ry = w.feedback(), w.feedback()
    w.change("x", root=rx)
    w.change("y", root=ry)
    w.ticks()
    assert not w.requests("join")
    w.finish("up", "unchanged")
    w.ticks()
    assert len(w.requests("join")) == 1
    assert not w.requests("tail")
    w.finish("join", "changed")
    w.ticks()
    assert len(w.requests("tail")) == 1


def test_pending_name_cause_does_not_create_followup_for_gated_refs():
    w = World()
    w.seed("x")
    w.node("target", ["x"], fields=("references",))
    w.change("x", payload={"name": "x", "references": [1]})
    w.ticks()
    w.finish("target", "unchanged")  # completed, not polled
    w.change("x", skip=True, payload={"name": "x", "references": [2]})
    w.change("x", payload={"name": "renamed", "references": [2]})
    w.ticks(6)
    assert len(w.requests("target")) == 1
    assert len(w.steps("target")) == 1
    assert w.steps("target")[0].absorbed_causes
    assert w.client.stale_dependencies([w.ids["x"]])


@pytest.mark.parametrize("fault", ["old_absent", "old_missing", "new_missing", "denied"])
@pytest.mark.parametrize("fields", [("references",), None])
def test_unprovable_cause_fails_closed(monkeypatch, fault, fields):
    w = World()
    w.seed("x")
    w.node("target", ["x"], fields=fields)
    event = w.change("x", skip=True, payload={"name": "x", "references": [1]})
    # Put the malformed cause into additional_causes, avoiding the event's own
    # non-null new_version contract; a scheduler must never infer a transition.
    cause = Cause(data_type="person", data_id=w.ids["x"], old_version=1, new_version=2)
    if fault.endswith("absent"):
        cause = cause.model_copy(
            update={"old_version" if fault == "old_absent" else "new_version": None}
        )
    valid = event.model_copy(
        update={
            "event_id": new_id(),
            "skip_history": False,
            "additional_causes": (TracedCause(cause=cause, root_feedback_id="system", depth=1),),
        }
    )
    original = w.client.snapshot

    class ReaderFault:
        def __init__(self, reader):
            self.reader = reader

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def get_version(self, ident, version):
            if ident == w.ids["x"]:
                if fault == "denied":
                    raise PermissionDenied("test missing history authority")
                if (fault == "old_missing" and version == 1) or (
                    fault == "new_missing" and version == 2
                ):
                    return None
            return self.reader.get_version(ident, version)

    @contextmanager
    def snapshot():
        with original() as reader:
            yield ReaderFault(reader)

    w.service.handle(valid)
    monkeypatch.setattr(w.client, "snapshot", snapshot)
    w.ticks()
    assert not w.requests()
    assert w.steps("target")[0].state == "blocked"
    assert w.steps("target")[0].reason in {"causal_versions_unavailable", "read_denied"}


@pytest.mark.parametrize("endpoint", ["old_version", "new_version"])
def test_absent_endpoint_is_not_guessed_from_current(endpoint):
    from ear_dependency.causal_fields import CausalVersionsUnavailable, changes_edge

    w = World()
    w.seed("x")
    w.node("target", ["x"], fields=("references",))
    w.change("x", skip=True, payload={"name": "x", "references": [1]})
    cause = Cause(data_type="person", data_id=w.ids["x"], old_version=1, new_version=2)
    # new_version is non-null in the contract; test defensive handling without
    # persisting an invalid contract instance or weakening the storage schema.
    cause = cause.model_copy(update={endpoint: None})
    edge = w.client.dependencies_of(w.ids["target"])[0]
    with w.client.snapshot() as reader, pytest.raises(CausalVersionsUnavailable):
        changes_edge(reader, cause, edge)


def test_multiple_causes_same_source_keep_all_roots_and_permit_real_ref_change():
    w = World()
    w.seed("x")
    w.node("target", ["x"], fields=("references",))
    w.change("x", skip=True, payload={"name": "x", "references": [1]})
    name_root, ref_root = w.feedback(), w.feedback()
    w.change("x", root=name_root, payload={"name": "renamed", "references": [1]})
    w.change("x", root=ref_root, payload={"name": "renamed", "references": [2]})
    w.ticks()
    assert len(w.requests()) == 1
    request = w.requests()[0]
    assert {request.root_feedback_id, *(t.root_feedback_id for t in request.additional_causes)} == {
        name_root,
        ref_root,
    }


def test_followup_waits_for_derived_name_change_without_reviving_gated_refs():
    w = World()
    w.seed("x")
    w.node("up", ["x"])
    write_payload(w, "up", {"value": 0, "name": "first", "references": []}, skip=True)
    w.node("target", ["up"], "plugin_b", fields=("references",))
    w.change("x")
    w.ticks()
    finish_payload(w, "up", {"value": 0, "name": "first", "references": [1]})
    w.ticks()
    w.finish("target", "unchanged")  # unpolled; pending new x must retain the barrier
    write_payload(w, "up", {"value": 0, "name": "first", "references": [2]}, skip=True)
    w.change("x")
    w.ticks(5)
    assert len(w.requests("up")) == 2
    assert len(w.requests("target")) == 1
    finish_payload(w, "up", {"value": 0, "name": "second", "references": [2]})
    w.ticks(6)
    assert len(w.requests("target")) == 1
    assert w.steps("target")[-1].state == "unaffected"
    assert w.client.stale_dependencies([w.ids["up"]])


def test_delayed_event_uses_new_version_not_later_gated_current():
    w = World()
    w.seed("x")
    w.node("refs", ["x"], fields=("references",))
    w.node("name", ["x"], "plugin_b", fields=("name",))
    w.change("x", payload={"name": "renamed", "references": []})
    w.change("x", skip=True, payload={"name": "renamed", "references": [1]})
    w.ticks()
    assert not w.requests("refs")
    assert len(w.requests("name")) == 1


def test_delayed_finish_uses_after_version_not_later_gated_current():
    w = World()
    w.seed("x")
    w.node("up", ["x"])
    write_payload(w, "up", {"name": "first", "references": []}, skip=True)
    w.node("refs", ["up"], "plugin_b", fields=("references",))
    w.node("name", ["up"], "plugin_c", fields=("name",))
    w.change("x")
    w.ticks()
    finish_payload(w, "up", {"name": "second", "references": []})
    write_payload(w, "up", {"name": "second", "references": [1]}, skip=True)
    w.ticks(5)
    assert not w.requests("refs")
    assert len(w.requests("name")) == 1


@pytest.mark.parametrize("backend", ["memory", "remote"])
@pytest.mark.parametrize("current_ahead", [False, True])
@pytest.mark.parametrize("fields", [("name",), ("references",), None])
def test_delayed_created_is_incorporated_not_authority_for_current_debt(
    backend, current_ahead, fields, tmp_path
):
    w = World()
    journal = remote(w, FakePort(w.client), tmp_path / "journal") if backend == "remote" else None
    try:
        w.seed("x")
        created = w.storage.events[-1]
        assert isinstance(created, DataChanged) and created.operation == "created"
        assert created.old_version is None and created.new_version == 1
        w.node("target", ["x"], fields=fields)
        edges = w.client.dependencies_of(w.ids["target"])
        if current_ahead:
            w.change("x", skip=True, payload={"name": "gated", "references": [1]})
        stale = w.client.stale_dependencies([w.ids["x"]])
        assert bool(stale) is current_ahead
        w.service.handle(created)
        w.ticks()
        assert not w.requests()
        assert not w.steps()
        assert w.triggers()[-1].status == "ignored"
        assert w.triggers()[-1].reason == "semantic_created"
        # Not a Finish or debt forgiveness: neither edge nor current stale changes.
        assert w.client.dependencies_of(w.ids["target"]) == edges
        assert w.client.stale_dependencies([w.ids["x"]]) == stale
        w.service.handle(created)
        w.ticks()
        assert not w.steps() and not w.requests()
        assert sum(t.event.event_id == created.event_id for t in w.triggers()) == 1
        cause = Cause(data_type="person", data_id=w.ids["x"], old_version=None, new_version=1)
        from ear_dependency.causal_fields import changes_edge

        with w.client.snapshot() as reader:
            assert changes_edge(reader, cause, edges[0]) is False
    finally:
        if journal:
            journal.close()


@pytest.mark.parametrize("backend", ["memory", "remote"])
@pytest.mark.parametrize("merge_before_tick", [False, True])
def test_delayed_created_does_not_poison_later_rename_or_reference_correction(
    backend, merge_before_tick, tmp_path
):
    w = World()
    journal = remote(w, FakePort(w.client), tmp_path / "journal") if backend == "remote" else None
    try:
        w.seed("x")
        created = w.storage.events[-1]
        w.node("name", ["x"], fields=("name",))
        w.node("refs", ["x"], "plugin_b", fields=("references",))
        w.node("whole", ["x"], "plugin_c")
        w.node("tail", ["refs"], "plugin_internal")
        w.change("x", skip=True, payload={"name": "x", "references": [1]})
        w.service.handle(created)
        if not merge_before_tick:
            w.ticks(5)
            assert not w.requests() and not w.steps()
        rename_root, correction_root = w.feedback(), w.feedback()
        creation_trace = TracedCause(
            cause=Cause(data_type="person", data_id=w.ids["x"], old_version=None, new_version=1),
            root_feedback_id="system",
            depth=1,
        )
        write_payload(
            w,
            "x",
            {"name": "renamed", "references": [1]},
            root=rename_root,
            additional=[creation_trace] if merge_before_tick else [],
        )
        w.ticks(5)
        assert len(w.requests("name")) == len(w.requests("whole")) == 1
        assert not w.requests("refs") and not w.requests("tail")
        for request in w.requests():
            # Merged creation remains traceable, not discarded or promoted to
            # permission for the gated references. Primary root is not special.
            traces = [
                TracedCause(
                    cause=request.cause,
                    root_feedback_id=request.root_feedback_id,
                    depth=request.depth,
                ),
                *request.additional_causes,
            ]
            assert {t.root_feedback_id for t in traces} == (
                {"system", rename_root} if merge_before_tick else {rename_root}
            )
            assert any(
                t.root_feedback_id == rename_root
                and t.cause.old_version == 2
                and t.cause.new_version == 3
                for t in traces
            )
            if merge_before_tick:
                assert any(
                    t.root_feedback_id == "system"
                    and t.cause.old_version is None
                    and t.cause.new_version == 1
                    for t in traces
                )
        w.finish("name", "unchanged")
        w.finish("whole", "unchanged")
        w.ticks(5)
        w.change("x", root=correction_root, payload={"name": "renamed", "references": [2]})
        w.ticks(5)
        assert len(w.requests("refs")) == 1 and not w.requests("tail")
        assert len(w.requests("name")) == 1
        assert w.requests("refs")[0].root_feedback_id == correction_root
        w.finish("refs", "changed")
        w.finish("whole", "unchanged")
        w.ticks(5)
        assert len(w.requests("tail")) == 1
        assert w.requests("tail")[0].root_feedback_id == correction_root
        assert not any(s.state == "blocked" for s in w.steps())
    finally:
        if journal:
            journal.close()


@pytest.mark.parametrize("fault", ["missing", "denied", "id", "data_type", "version", "terminated"])
def test_created_requires_proven_active_initial_version(monkeypatch, fault):
    w = World()
    w.seed("x")
    created = w.storage.events[-1]
    w.node("target", ["x"])
    original = w.client.snapshot

    class ReaderFault:
        def __init__(self, reader):
            self.reader = reader

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def get_version(self, ident, version):
            record = self.reader.get_version(ident, version)
            if ident != w.ids["x"] or version != 1:
                return record
            if fault == "missing":
                return None
            if fault == "denied":
                raise PermissionDenied("synthetic initial version authority failure")
            return record.model_copy(
                update={
                    "id": {"id": new_id()},
                    "data_type": {"data_type": "place"},
                    "version": {"version": 2},
                    "terminated": {"status": "superseded"},
                }[fault]
            )

    @contextmanager
    def snapshot():
        with original() as reader:
            yield ReaderFault(reader)

    monkeypatch.setattr(w.client, "snapshot", snapshot)
    w.service.handle(created)
    w.ticks()
    assert not w.requests() and not w.steps()
    assert w.triggers()[-1].status == "blocked"
    assert w.triggers()[-1].reason == (
        "read_denied" if fault == "denied" else "causal_versions_unavailable"
    )
    from ear_dependency.causal_fields import CausalVersionsUnavailable, changes_edge

    cause = Cause(data_type="person", data_id=w.ids["x"], old_version=None, new_version=1)
    edge = w.client.dependencies_of(w.ids["target"])[0]
    error = PermissionDenied if fault == "denied" else CausalVersionsUnavailable
    with w.client.snapshot() as reader, pytest.raises(error):
        changes_edge(reader, cause, edge)


@pytest.mark.parametrize("terminate", [False, True])
@pytest.mark.parametrize("old_absent", [False, True])
def test_delayed_created_does_not_relax_updated_or_terminated_history(
    monkeypatch, terminate, old_absent
):
    w = World()
    w.seed("x")
    created = w.storage.events[-1]
    w.node("target", ["x"], fields=("references",))
    w.service.handle(created)
    w.ticks()
    assert not w.requests() and not w.steps()
    assert w.triggers()[-1].reason == "semantic_created"
    # Keep the real event operation and versions; only inject the history fault.
    event = w.change("x", skip=True, terminate=terminate, payload={"references": [1]})
    event = event.model_copy(update={"event_id": new_id(), "skip_history": False})
    assert event.operation == ("superseded" if terminate else "updated")
    assert event.old_version == 1 and event.new_version == 2
    if old_absent:
        event = event.model_copy(update={"old_version": None})
    original = w.client.snapshot

    class ReaderFault:
        def __init__(self, reader):
            self.reader = reader

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def get_version(self, ident, version):
            if not old_absent and ident == w.ids["x"] and version == 1:
                return None
            return self.reader.get_version(ident, version)

    @contextmanager
    def snapshot():
        with original() as reader:
            yield ReaderFault(reader)

    w.service.handle(event)
    monkeypatch.setattr(w.client, "snapshot", snapshot)
    w.ticks()
    assert not w.requests()
    if old_absent:
        assert not w.steps()
        assert w.triggers()[-1].status == "blocked"
        assert w.triggers()[-1].reason == "causal_versions_unavailable"
    else:
        assert w.steps("target")[-1].state == "blocked"
        assert w.steps("target")[-1].reason == "causal_versions_unavailable"


@pytest.mark.parametrize(
    "operation,old,new",
    [
        ("created", 1, 1),
        ("created", None, 2),
        ("updated", None, 1),
        ("updated", None, 2),
        ("superseded", None, 1),
        ("superseded", None, 2),
    ],
)
def test_no_before_exception_is_only_for_valid_creation_event(operation, old, new):
    w = World()
    w.seed("x")
    created = w.storage.events[-1]
    w.node("target", ["x"])
    w.change("x", skip=True)
    malformed = created.model_copy(
        update={
            "event_id": new_id(),
            "operation": operation,
            "old_version": old,
            "new_version": new,
        }
    )
    w.service.handle(malformed)
    w.ticks()
    assert not w.requests() and not w.steps()
    assert w.triggers()[-1].status == "blocked"
    assert w.triggers()[-1].reason == "causal_versions_unavailable"


@pytest.mark.parametrize("backend", ["memory", "remote"])
def test_created_gate_does_not_discard_independent_additional_cause(backend, tmp_path):
    w = World()
    journal = remote(w, FakePort(w.client), tmp_path / "journal") if backend == "remote" else None
    try:
        w.seed("x")
        created = w.storage.events[-1]
        w.node("name", ["x"], fields=("name",))
        w.node("refs", ["x"], "plugin_b", fields=("references",))
        root = w.feedback()
        # Commit a real update, but deliver it only as a traced additional cause.
        w.storage.client("attribution").write(
            [Update("person", w.ids["x"], 1, {"name": "renamed", "references": []})],
            Context(root),
        ).check()
        extra = TracedCause(
            cause=Cause(data_type="person", data_id=w.ids["x"], old_version=1, new_version=2),
            root_feedback_id=root,
            depth=1,
        )
        w.service.handle(created.model_copy(update={"additional_causes": (extra,)}))
        w.ticks()
        assert not w.requests("refs")
        [request] = w.requests("name")
        assert request.cause.old_version is None and request.cause.new_version == 1
        assert request.root_feedback_id == "system"
        assert request.additional_causes == (extra.model_copy(update={"depth": 2}),)
    finally:
        if journal:
            journal.close()
