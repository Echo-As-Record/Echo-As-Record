from dataclasses import replace
from typing import cast

import pytest
from ear_contracts import DataChanged, new_id, utcnow
from ear_dependency import DependencyService
from ear_dependency.domain import Step
from ear_storage_client import (
    AppendRaw,
    CommitUnknown,
    Context,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    StorageClient,
)
from ear_storage_client.testing import make_raw

from .fixtures import World


def test_multilayer_diamond_waits_and_routes_dynamically():
    w = World()
    w.seed("source")
    w.node("a", ["source"])
    w.node("b", ["source"], "plugin_b")
    w.node("join", ["a", "b", "source"], "plugin_internal")
    # Register a new owner AFTER the worker was constructed.
    w.add_type("plugin_late", "late")
    w.node("tail", ["join"], "plugin_late")
    w.change("source")
    w.ticks()
    assert {r.target_id for r in w.requests()} == {w.ids["a"], w.ids["b"]}
    assert w.steps("a")[0].owner == "alpha"
    w.finish("a", "changed")
    w.ticks()
    assert not w.requests("join")
    w.finish("b", "unchanged")
    w.ticks()
    assert len(w.requests("join")) == 1
    assert not w.requests("tail")
    join = w.steps("join")[0]
    assert set(join.upstream_request_ids) == {
        w.requests("a")[0].event_id,
        w.requests("b")[0].event_id,
    }
    w.finish("join", "changed")
    w.ticks()
    assert w.requests("tail")[0].depth == 3
    assert w.steps("tail")[0].owner == "late"
    w.finish("tail", "unchanged")
    w.ticks()
    assert all(t.status == "closed" for t in w.triggers())
    assert len(w.requests()) == 4


def test_unchanged_stops_only_its_path_not_other_root():
    w = World()
    w.seed("x")
    w.seed("y")
    w.node("a", ["x"])
    w.node("join", ["a", "y"], "plugin_b")
    rx, ry = w.feedback(), w.feedback()
    w.change("x", root=rx)
    w.change("y", root=ry)
    w.ticks()
    assert not w.requests("join")
    w.finish("a", "unchanged")
    w.ticks()
    request = w.requests("join")[0]
    assert {request.root_feedback_id, *(t.root_feedback_id for t in request.additional_causes)} == {
        rx,
        ry,
    }
    w.finish("join", "unchanged")
    w.ticks()
    assert all(t.status == "closed" for t in w.triggers())


def test_unchanged_does_not_resurrect_gated_stale_rule():
    w = World()
    w.seed("x")
    w.seed("rule", "rule")
    w.node("a", ["x"])
    w.node("tail", ["a", "rule"], "plugin_b")
    w.change("rule")
    w.change("x")
    w.ticks()
    w.finish("a", "unchanged")
    w.ticks()
    assert not w.requests("tail")
    assert w.steps("tail")[0].state == "unaffected"


@pytest.mark.parametrize("gate", ["raw", "rule", "skip"])
def test_gates(gate):
    w = World()
    if gate == "raw":
        raw = make_raw("voiceprint", {"synthetic": True}, start=utcnow())
        event = (
            w.storage.client("adapter").write([AppendRaw(raw)], Context.system()).check().events[0]
        )
        w.service.handle(event)
    else:
        w.seed("source", "rule" if gate == "rule" else "person")
        w.node("target", ["source"])
        w.change("source", skip=gate == "skip")
    w.ticks()
    assert not w.requests()
    assert w.triggers()[0].status == "ignored"


def test_skip_is_per_change_not_root_and_feedback_is_explicitly_blocked():
    w = World()
    w.seed("reference")
    w.seed("attribution_like")
    w.node("a", ["reference"])
    w.node("b", ["attribution_like"], "plugin_b")
    root = w.feedback()
    w.change("reference", root=root, skip=True)
    w.change("attribution_like", root=root)
    raw = make_raw(
        "feedback",
        {"module": "dependency", "command": {"kind": "reevaluate_related"}},
        start=utcnow(),
    )
    event = w.storage.client("api").write([AppendRaw(raw)], Context.system()).check().events[0]
    w.service.handle(event)
    w.ticks()
    assert not w.requests("a") and len(w.requests("b")) == 1
    assert any(t.reason == "feedback_not_implemented" for t in w.triggers())


@pytest.mark.parametrize(
    "outcome,disposition,reason",
    [
        ("failed", None, "failed"),
        ("not_applicable", "unavailable", "unavailable"),
        ("not_applicable", "kept_protected", "kept_protected"),
    ],
)
def test_failure_and_inapplicable_never_release_downstream(outcome, disposition, reason):
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.node("tail", ["a", "x"], "plugin_b")
    w.change("x")
    w.ticks()
    w.finish("a", outcome, disposition)
    w.ticks()
    assert w.steps("a")[0].reason == reason
    assert w.steps("tail")[0].reason == "upstream_blocked"
    assert not w.requests("tail")
    assert w.triggers()[0].status == "blocked"


def test_terminated_is_real_terminal_and_allows_owner_to_decide_downstream():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.node("tail", ["a"], "plugin_b")
    w.change("x")
    w.ticks()
    w.finish("a", "not_applicable", "terminated")
    w.ticks()
    result = w.steps("a")[0].result
    assert result is not None and result.disposition == "terminated"
    assert len(w.requests("tail")) == 1
    tail = w.client.get(w.ids["tail"])
    assert tail is not None and tail.version == 1  # no cascade delete/business writes


def test_current_fields_and_a_b_a():
    w = World()
    w.seed("x")
    w.node("by_name", ["x"], fields=("name",))
    w.node("by_refs", ["x"], "plugin_b", fields=("references",))
    w.change("x", payload={"name": "new", "references": []})
    w.change("x", payload={"name": "x", "references": []})
    w.ticks()
    assert not w.requests()
    assert all(s.state == "unaffected" for s in w.steps())
    w.change("x", terminate=True)
    w.ticks()
    assert len(w.requests()) == 2  # terminal even with equal field values


def test_event_duplicate_and_reconstruction_and_marker_is_not_finish():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    event = w.change("x")
    w.service.handle(event)
    w.rebuild()  # trigger persisted but not expanded
    w.ticks()
    request = w.requests("a")[0]
    w.storage.client("alpha").write(
        [MarkProcessed("alpha.queued", request.event_id)], Context.of(request)
    ).check()
    w.rebuild()  # request persisted but not completed
    w.ticks()
    assert w.steps("a")[0].state == "requested"
    assert len(w.requests()) == 1 and len(w.triggers()) == 1
    w.finish("a", "unchanged")
    w.rebuild()
    w.ticks()
    result = w.steps("a")[0].result
    assert result is not None and result.outcome == "unchanged"
    assert w.triggers()[0].status == "closed"


@pytest.mark.parametrize("absorbed", [True, False])
def test_inflight_new_root_preserved_and_followup_or_absorption(absorbed):
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    first, second = w.feedback(), w.feedback()
    w.change("x", root=first)
    w.ticks()
    if not absorbed:
        w.finish("a", "unchanged")  # committed but not polled
    w.change("x", root=second)
    w.ticks(1)
    if absorbed:
        w.finish("a", "unchanged")  # owner reads newest input
    w.rebuild()
    w.ticks(5)
    if absorbed:
        assert len(w.requests()) == 1
        assert w.steps("a")[0].absorbed_causes[0].root_feedback_id == second
    else:
        assert len(w.requests()) == 2
        assert w.requests()[-1].root_feedback_id == second
        assert w.steps("a")[0].successor_id is not None


@pytest.mark.parametrize("committed", [False, True])
def test_commit_unknown_does_not_blindly_issue_another_request(monkeypatch, committed):
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.change("x")
    original = w.client.write

    def fault(ops, context):
        if any(isinstance(op, PublishReevaluation) for op in ops):
            assert any(
                isinstance(op, PutSystemRecord) and op.payload["state"] == "requested" for op in ops
            )
            if committed:
                original(ops, context).check()
            raise CommitUnknown(new_id())
        return original(ops, context)

    monkeypatch.setattr(w.client, "write", fault)
    with pytest.raises(CommitUnknown):
        w.service.tick()
    original_step = w.steps("a")[0]
    w.rebuild()
    w.ticks()
    assert w.steps("a")[0].request_event_id == original_step.request_event_id
    assert w.steps("a")[0].intent_id == original_step.intent_id
    assert len(w.requests()) == int(committed)
    assert w.steps("a")[0].state == ("requested" if committed else "blocked")
    if not committed:
        assert w.steps("a")[0].reason == "commit_unknown"


def test_publish_batch_conflict_is_atomic_and_handler_conflicts_not_acked(monkeypatch):
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    event = w.change("x")
    original = w.client.write

    def reject(ops, context):
        changed = []
        for op in ops:
            if isinstance(op, PutSystemRecord) and op.payload.get("state") == "requested":
                op = replace(op, expected_revision=999)
            changed.append(op)
        return original(changed, context)

    monkeypatch.setattr(w.client, "write", reject)
    with pytest.raises(Exception, match="revision_mismatch"):
        w.service.tick()
    assert not w.requests()
    assert w.steps("a")[0].state == "intent"
    assert w.client.is_processed("dependency.changes", event.event_id)


def test_timeout_late_finish_recovers_without_resend():
    now = [0.0]
    w = World(clock=lambda: now[0], request_timeout=10)
    w.seed("x")
    w.node("a", ["x"])
    w.node("tail", ["x", "a"], "plugin_b")
    w.change("x")
    w.ticks()
    now[0] = 11
    w.ticks()
    assert w.steps("a")[0].reason == "timeout"
    assert w.triggers()[0].status == "blocked"
    w.finish("a", "unchanged")
    w.ticks()
    assert len(w.requests("a")) == 1 and len(w.requests("tail")) == 1


def test_capability_and_limits_are_visible_not_success():
    w = World(max_nodes=1)
    w.seed("x")
    w.node("a", ["x"])
    w.node("b", ["a"], "plugin_b")
    w.change("x")
    w.ticks()
    assert not w.requests()
    assert w.triggers()[0].reason == "node_limit"
    assert w.triggers()[0].status == "blocked"
    w = World()
    w.seed("x")
    w.add_type("plugin_no_rejudge", "incapable", capable=False)
    w.node("a", ["x"], "plugin_no_rejudge")
    w.change("x")
    w.ticks()
    assert w.steps("a")[0].reason == "not_reevaluable"


def test_visibility_remote_and_read_denial_are_not_empty_success(monkeypatch):
    w = World()
    with pytest.raises(ValueError, match="coverage_incomplete"):
        DependencyService(w.client)
    with pytest.raises(ValueError, match="scheduler_authority_unavailable"):
        DependencyService(cast(StorageClient, object()), full_visibility=True)
    w.seed("x")
    event = w.change("x")
    # Missing current input is explicit, never an empty graph success.
    missing = event.model_copy(update={"event_id": new_id(), "data_id": new_id()})
    w.service.handle(missing)
    assert w.triggers()[-1].reason == "coverage_incomplete"
    w.node("a", ["x"])
    w.change("x")
    original = w.client.write

    def denied(ops, context):
        if any(isinstance(op, PublishReevaluation) for op in ops):
            raise PermissionDenied("synthetic revoked access")
        return original(ops, context)

    monkeypatch.setattr(w.client, "write", denied)
    w.ticks()
    assert not w.requests()
    assert w.steps("a")[0].reason == "publish_denied"


def test_single_writer_and_only_existing_event_types():
    w = World()
    with pytest.raises(PermissionDenied):
        w.storage.client("alpha").write(
            [PutSystemRecord("propagation_record", new_id(), {})], Context.system()
        )
    w.seed("x")
    w.node("a", ["x"])
    w.change("x")
    w.ticks()
    assert {e.event_type for e in w.storage.events} == {"data_changed", "reevaluation_requested"}
    assert all(
        isinstance(e, DataChanged) or e.event_type == "reevaluation_requested"
        for e in w.storage.events
    )
    assert all(isinstance(s, Step) for s in w.steps())


def test_finished_but_pending_step_merges_new_input_without_duplicate_generation():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    roots = [w.feedback() for _ in range(3)]
    w.change("x", root=roots[0])
    w.ticks()
    w.finish("a", "unchanged")
    w.change("x", root=roots[1])
    w.ticks(1)  # completion observed; pending input not yet resolved
    assert w.steps("a")[0].state == "done"
    assert w.steps("a")[0].pending_causes
    w.change("x", root=roots[2])
    w.ticks(5)
    assert len(w.requests("a")) == 2
    assert len(w.steps("a")) == 2
    request = w.requests("a")[-1]
    assert {
        request.root_feedback_id,
        *(c.root_feedback_id for c in request.additional_causes),
    } == set(roots[1:])


def test_failed_upstream_and_other_unchanged_branch_do_not_hide_failure():
    w = World()
    w.seed("x")
    w.seed("y")
    w.node("a", ["x"])
    w.node("b", ["y"], "plugin_b")
    w.node("join", ["a", "b"], "plugin_c")
    w.change("x", root=w.feedback())
    w.change("y", root=w.feedback())
    w.ticks()
    w.finish("a", "failed")
    w.finish("b", "unchanged")
    w.ticks()
    assert not w.requests("join")
    assert w.steps("join")[0].reason == "upstream_blocked"
    assert all(t.status == "blocked" for t in w.triggers())


def test_unrelated_stalled_chain_does_not_globally_stop_other_root():
    w = World()
    w.seed("x")
    w.seed("y")
    w.node("stalled", ["x"])
    w.node("independent", ["y"], "plugin_b")
    w.change("x")
    w.change("y")
    w.ticks()
    w.finish("independent", "unchanged")
    w.ticks()
    assert w.steps("stalled")[0].state == "requested"
    assert w.steps("independent")[0].state == "done"
    assert {t.status for t in w.triggers()} == {"closed", "waiting"}


def test_crash_after_intent_before_publish_freezes_same_identity(monkeypatch):
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.change("x")

    def crash(*args):
        raise RuntimeError("worker disappeared")

    monkeypatch.setattr(w.service, "_publish", crash)
    with pytest.raises(RuntimeError, match="disappeared"):
        w.service.tick()
    before = w.steps("a")[0]
    assert before.state == "intent" and before.request_event_id
    w.rebuild()
    w.ticks()
    assert not w.requests()
    after = w.steps("a")[0]
    assert after.reason == "commit_unknown"
    assert after.request_event_id == before.request_event_id


def test_trigger_marker_rollback_has_no_lost_work(monkeypatch):
    from ear_storage_client import ConflictError

    w = World()
    w.seed("x")
    w.node("a", ["x"])
    original = w.client.write

    def reject_trigger(ops, context):
        return original(
            [
                replace(op, expected_revision=999)
                if isinstance(op, PutSystemRecord) and op.payload.get("record_kind") == "trigger"
                else op
                for op in ops
            ],
            context,
        )

    monkeypatch.setattr(w.client, "write", reject_trigger)
    with pytest.raises(ConflictError):
        w.change("x")
    event = w.storage.events[-1]
    assert not w.client.is_processed(w.service.consumer, event.event_id)
    assert not w.triggers()
    monkeypatch.setattr(w.client, "write", original)
    w.service.handle(event)
    w.ticks()
    assert len(w.requests()) == 1


def test_duplicate_bus_delivery_does_not_duplicate_work():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.client.subscribe(w.service.consumer, w.service.handle, event_types=["data_changed"])
    w.change("x")
    w.storage.run_until_idle()
    w.ticks()
    assert len(w.triggers()) == 1 and len(w.requests()) == 1
    assert not w.storage.dead_letters


def test_inherited_origins_and_parent_context_survive_changed_chain():
    from ear_contracts import Cause, TracedCause
    from ear_storage_client import Update

    w = World()
    w.seed("x")
    w.seed("y")
    w.node("a", ["x"])
    w.node("tail", ["a"], "plugin_b")
    rx, ry = w.feedback(), w.feedback()
    extra = TracedCause(
        cause=Cause(data_type="person", data_id=w.ids["y"], old_version=None, new_version=1),
        root_feedback_id=ry,
        depth=1,
    )
    result = (
        w.storage.client("attribution")
        .write(
            [Update("person", w.ids["x"], 1, {"name": "changed", "references": []})],
            Context(rx, additional_causes=(extra,)),
        )
        .check()
    )
    w.service.handle(result.events[0])
    w.ticks()
    assert w.requests("a")[0].additional_causes[0].depth == 2
    w.finish("a", "changed")
    w.ticks()
    request = w.requests("tail")[0]
    assert request.trigger_request_event_id == w.requests("a")[0].event_id
    assert request.root_feedback_id == rx and request.depth == 2
    assert any(t.root_feedback_id == ry and t.depth == 3 for t in request.additional_causes)


def test_cause_and_depth_limits_preserve_all_origins():
    w = World(max_depth=1)
    w.seed("x")
    w.node("a", ["x"])
    w.node("tail", ["a"], "plugin_b")
    w.change("x")
    w.ticks()
    w.finish("a", "changed")
    w.ticks()
    assert not w.requests("tail")
    assert w.steps("tail")[0].reason == "cause_or_depth_limit"
    # Exercise the >256 additional-origins bound without building a large graph.
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    for _ in range(258):
        w.change("x", root=w.feedback())
    w.ticks(1)
    step = w.steps("a")[0]
    assert len(step.causes) == 258
    assert step.reason == "cause_or_depth_limit"
    assert not w.requests()


def test_inflight_descendant_waits_for_new_generation_of_indirect_upstream():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.node("tail", ["a"], "plugin_b")
    first, second = w.feedback(), w.feedback()
    w.change("x", root=first)
    w.ticks()
    w.finish("a", "changed")
    w.ticks()
    w.finish("tail", "unchanged")  # not yet polled
    w.change("x", root=second)
    w.ticks(4)
    assert len(w.requests("a")) == 2
    assert len(w.requests("tail")) == 1
    w.finish("a", "changed")
    w.ticks(4)
    assert len(w.requests("tail")) == 2  # pending x was NOT falsely absorbed
    request = w.requests("tail")[-1]
    assert second in {
        request.root_feedback_id,
        *(t.root_feedback_id for t in request.additional_causes),
    }


def test_graph_read_denial_persists_block_instead_of_empty_success(monkeypatch):
    from contextlib import contextmanager

    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.change("x")
    original = w.client.snapshot

    class DeniedReader:
        def __init__(self, reader):
            self.reader = reader

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def dependents_of(self, *args, **kwargs):
            raise PermissionDenied("synthetic incomplete visibility")

    @contextmanager
    def denied_snapshot():
        with original() as reader:
            yield DeniedReader(reader)

    monkeypatch.setattr(w.client, "snapshot", denied_snapshot)
    w.ticks()
    assert not w.requests()
    assert w.triggers()[0].status == "blocked"
    assert w.triggers()[0].reason == "read_denied"


def test_no_downstream_is_closed_only_with_explicit_complete_visibility():
    w = World()
    w.seed("x")
    w.change("x")
    w.ticks()
    assert w.triggers()[0].status == "closed"
    assert w.triggers()[0].summary == {}
    assert not w.requests()


def test_unchanged_finish_refreshes_without_business_version_or_event():
    w = World()
    w.seed("x")
    w.node("a", ["x"])
    w.change("x")
    w.ticks()
    event_count = len(w.storage.events)
    before = w.client.get(w.ids["a"])
    assert before is not None
    w.finish("a", "unchanged")
    w.rebuild()
    w.ticks()
    after = w.client.get(w.ids["a"])
    assert after is not None and before.version == after.version
    assert len(w.storage.events) == event_count
    assert not w.client.stale_dependencies([w.ids["x"]])
    assert w.triggers()[0].status == "closed"
