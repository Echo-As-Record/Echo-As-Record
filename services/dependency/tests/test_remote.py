"""Fake server port first: no SDK implementation patch or permission inference."""

from copy import deepcopy
from typing import cast

import pytest
from ear_dependency import CommitJournal, RemoteDependencyService
from ear_dependency.authority import SchedulerBlocked
from ear_dependency.domain import Trigger
from ear_dependency.records import load
from ear_storage_client import CommitUnknown, PublishReevaluation, StorageClient
from ear_storage_client.wire import encode_request

from .fixtures import World


class FakePort:
    def __init__(self, client):
        self.client = client
        self.proof = {
            "codec": 1,
            "coverage_complete": True,
            "coverage_revision": "grant-v1",
            "owners": {
                name: {"enabled": True, "can_receive": True, "revision": 1, "online": None}
                for name in ("alpha", "beta", "gamma", "attribution")
            },
        }
        self.receipts = {}
        self.sends = []
        self.fail: str | None = None
        self.committed = False
        self.state_calls = 0
        self.state_hook = lambda: None
        self.subscribed = False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def scheduler_state(self):
        self.state_calls += 1
        self.state_hook()
        return deepcopy(self.proof)

    def subscribe(self, *args, **kwargs):
        self.subscribed = True

    def write(self, ops, context, *, request_id):
        body = encode_request(ops, context)
        self.sends.append((request_id, body))
        if request_id in self.receipts:
            old, result = self.receipts[request_id]
            assert old == body
            return result
        phase = "publish" if any(isinstance(op, PublishReevaluation) for op in ops) else "state"
        fail = self.fail == phase
        if fail:
            self.fail = None
            if not self.committed:
                raise CommitUnknown(request_id)
        result = self.client.write(ops, context)
        self.receipts[request_id] = body, result
        if fail:
            raise CommitUnknown(request_id)
        return result


def remote(w, port, path):
    journal = CommitJournal(path, namespace="synthetic-dependency")
    w.service = RemoteDependencyService(cast(StorageClient, port), journal=journal)
    return journal


def stored_triggers(w):
    # Independent fixture observer: inspect retained evidence after scheduler denial,
    # never use this to authorize a remote read or a scheduling decision.
    return [r for r, _ in load(w.client, w.service.max_records).values() if isinstance(r, Trigger)]


def world():
    w = World()
    w.seed("source")
    w.node("a", ["source"])
    w.node("b", ["a", "source"], "plugin_b")
    return w


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("phase", ["admit", "expand", "intent", "publish", "finish"])
def test_identical_replay_at_every_write_boundary(tmp_path, committed, phase, monkeypatch):
    w = world()
    event = w.change("source") if phase != "admit" else None
    port = FakePort(w.client)
    path = tmp_path / "journal"
    journal = remote(w, port, path)
    port.committed = committed
    if phase == "admit":
        port.fail = "state"
        with pytest.raises(CommitUnknown):
            w.change("source")
    elif phase in {"expand", "publish"}:
        port.fail = "state" if phase == "expand" else "publish"
        with pytest.raises(CommitUnknown):
            w.service.tick()
    elif phase == "intent":
        original = w.service._prepare

        def arm(*args):
            original(*args)
            port.fail = "state"

        monkeypatch.setattr(w.service, "_prepare", arm)
        with pytest.raises(CommitUnknown):
            w.service.tick()
    else:
        w.ticks()
        w.finish("a", "unchanged")
        port.fail = "state"
        with pytest.raises(CommitUnknown):
            w.service.tick()
    saved = journal.pending("operation")
    assert saved is not None
    journal.close()
    journal = remote(w, port, path)
    try:
        cast(
            RemoteDependencyService, w.service
        ).subscribe()  # drain despite a committed MarkProcessed suppressing delivery
        assert journal.pending("operation") is None and port.subscribed
        assert sum((rid, body) == saved for rid, body in port.sends) == 2
        w.ticks()
        if phase != "finish":
            w.finish("a", "unchanged")
        w.ticks()
        w.finish("b", "unchanged")
        w.ticks()
        assert len(w.requests()) == 2
        assert all(t.status == "closed" for t in w.triggers())
        if event is not None:
            w.service.handle(event)
            assert len(w.requests()) == 2
    finally:
        journal.close()


def test_crash_after_intent_before_journal_keeps_ids(tmp_path, monkeypatch):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    path = tmp_path / "journal"
    journal = remote(w, port, path)

    def crash(*args):
        raise SystemExit("synthetic crash before send")

    monkeypatch.setattr(w.service, "_publish", crash)
    with pytest.raises(SystemExit):
        w.service.tick()
    step = w.steps("a")[0]
    assert step.state == "intent" and not w.requests()
    journal.close()
    journal = remote(w, port, path)
    try:
        w.ticks()
        assert w.requests()[0].event_id == step.request_event_id
        assert any(rid == step.intent_id for rid, _ in port.sends)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("coverage", "coverage_incomplete"),
        ("denied", "coverage_denied"),
        ("changed", "scheduler_state_changed"),
    ],
)
def test_coverage_fail_closed_never_empty_success(tmp_path, mutation, reason):
    from ear_storage_client import PermissionDenied

    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")

    def hook():
        if mutation == "coverage":
            port.proof["coverage_complete"] = False
        elif mutation == "denied":
            raise PermissionDenied("synthetic")
        elif port.state_calls == 2:
            port.proof["coverage_revision"] = "grant-v2"

    port.state_hook = hook
    try:
        with pytest.raises(SchedulerBlocked, match=reason):
            w.service.tick()
        assert not w.requests()
        if mutation == "changed":
            assert w.service.records() == stored_triggers(w)
        else:
            with pytest.raises(PermissionDenied if mutation == "denied" else SchedulerBlocked):
                w.service.records()
        assert stored_triggers(w)[0].status != "closed"
        status = w.client.get_system_record(cast(RemoteDependencyService, w.service).status_id)
        assert status is not None
        assert status.payload["status"] == "blocked"
        assert status.payload["reason"] == reason
    finally:
        journal.close()


@pytest.mark.parametrize(
    "enabled,receive,reason",
    [
        (False, False, "owner_disabled"),
        (True, False, "owner_unauthorized"),
    ],
)
def test_owner_lifecycle_and_downstream_blocked(tmp_path, enabled, receive, reason):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    port.proof["owners"]["alpha"].update(enabled=enabled, can_receive=receive)
    journal = remote(w, port, tmp_path / "journal")
    try:
        w.ticks()
        assert not w.requests()
        assert w.steps("a")[0].reason == reason
        assert w.steps("b")[0].reason == "upstream_blocked"
    finally:
        journal.close()


def test_recovery_denied_retains_original_request_and_refuses_subscribe(tmp_path):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    try:
        port.fail = "publish"
        with pytest.raises(CommitUnknown):
            w.service.tick()
        pending = journal.pending("operation")
        port.proof["owners"]["alpha"].update(enabled=False, can_receive=False)
        with pytest.raises(SchedulerBlocked, match="owner_disabled"):
            cast(RemoteDependencyService, w.service).subscribe()
        assert not port.subscribed
        assert journal.pending("operation") == pending
        assert not w.requests()
    finally:
        journal.close()


def test_new_input_while_requested_does_not_reuse_publish_receipt(tmp_path):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    try:
        w.ticks()
        original = w.requests("a")[0]
        w.change("source")
        w.ticks()
        assert len(w.requests("a")) == 1
        assert w.steps("a")[0].pending_causes
        w.finish("a", "changed")
        w.ticks()
        w.finish("b", "unchanged")
        w.ticks()
        assert len(w.requests()) == 2
        assert w.requests("a")[0] == original
        assert w.steps("a")[0].absorbed_causes
        assert all(t.status == "closed" for t in w.triggers())
    finally:
        journal.close()


def test_owner_changes_between_intent_and_publish_are_blocked(tmp_path, monkeypatch):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    save = w.service._save

    def save_and_change(step, rev):
        save(step, rev)
        if getattr(step, "state", None) == "intent":
            port.proof["owners"]["alpha"]["revision"] = 2

    monkeypatch.setattr(w.service, "_save", save_and_change)
    try:
        with pytest.raises(SchedulerBlocked, match="scheduler_state_changed"):
            w.service.tick()
        assert not w.requests()
        w.ticks()
        assert w.steps("a")[0].reason == "publish_authority_changed"
        assert not w.requests()
    finally:
        journal.close()


def test_missing_authority_has_no_full_visibility_escape(tmp_path):
    w = world()
    journal = CommitJournal(tmp_path / "journal", namespace="synthetic")
    try:
        port = FakePort(w.client)
        port.scheduler_state = None  # type: ignore[assignment]
        service = RemoteDependencyService(cast(StorageClient, port), journal=journal)
        with pytest.raises(SchedulerBlocked, match="scheduler_authority_unavailable"):
            service.tick()
        with pytest.raises(TypeError):
            RemoteDependencyService(w.client, journal=journal, full_visibility=True)  # type: ignore[call-arg]
    finally:
        journal.close()


def test_filesystem_swap_immediately_before_send_keeps_evidence(tmp_path, monkeypatch):
    import ear_dependency.remote as remote_module
    from ear_contracts import new_id
    from ear_storage_client import Context, PutSystemRecord

    w = world()
    port = FakePort(w.client)
    path = tmp_path / "journal"
    journal = remote(w, port, path)
    journal.prepare(
        "operation", [PutSystemRecord("propagation_record", new_id(), {})], Context.system()
    )
    decode = remote_module.decode_request

    def swap(body):
        path.rename(tmp_path / "detached")
        path.mkdir(mode=0o700)
        return decode(body)

    monkeypatch.setattr(remote_module, "decode_request", swap)
    with pytest.raises(ValueError, match="identity changed"):
        cast(RemoteDependencyService, w.service).recover_pending()
    assert not port.sends
    assert not list(path.iterdir())
    assert journal._closed


def test_replay_keeps_primary_root_and_all_additional_context(tmp_path):
    from ear_storage_client.wire import decode_request

    w = World()
    w.seed("x")
    w.seed("y")
    w.node("a", ["x", "y"])
    first, second = w.feedback(), w.feedback()
    w.change("x", root=first)
    w.change("y", root=second)
    port = FakePort(w.client)
    path = tmp_path / "journal"
    journal = remote(w, port, path)
    port.fail, port.committed = "publish", True
    try:
        with pytest.raises(CommitUnknown):
            w.service.tick()
        pending = journal.pending("operation")
        assert pending is not None
        _, context = decode_request(pending[1])
        assert {
            context.root_feedback_id,
            *(t.root_feedback_id for t in context.additional_causes),
        } == {
            first,
            second,
        }
        assert context.depth == 1
    finally:
        journal.close()
    journal = remote(w, port, path)
    try:
        w.ticks()
        assert len(w.requests()) == 1
        assert sum(item == pending for item in port.sends) == 2
    finally:
        journal.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("codec", True),
        ("coverage_revision", ""),
        ("coverage_complete", 1),
        ("owners", []),
    ],
)
def test_malformed_coverage_is_blocked(tmp_path, field, value):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    port.proof[field] = value
    journal = remote(w, port, tmp_path / "journal")
    try:
        with pytest.raises(SchedulerBlocked):
            w.service.tick()
        assert not w.requests()
        with pytest.raises(SchedulerBlocked):
            w.service.records()
        assert stored_triggers(w) and all(t.status != "closed" for t in stored_triggers(w))
    finally:
        journal.close()


def test_recovery_rechecks_proof_after_registry_lookup(tmp_path):
    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    try:
        port.fail = "publish"
        with pytest.raises(CommitUnknown):
            w.service.tick()
        original = journal.pending("operation")
        count = port.state_calls

        def change_on_second_read():
            if port.state_calls == count + 2:
                port.proof["coverage_revision"] = "changed-during-recovery"

        port.state_hook = change_on_second_read
        with pytest.raises(SchedulerBlocked, match="scheduler_state_changed"):
            cast(RemoteDependencyService, w.service).recover_pending()
        assert journal.pending("operation") == original
        assert not w.requests()
    finally:
        journal.close()


def test_denied_publication_keeps_original_body_for_authorized_recovery(tmp_path, monkeypatch):
    from ear_storage_client import PermissionDenied

    w = world()
    w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    write = port.write

    def denied(ops, context, *, request_id):
        if any(isinstance(op, PublishReevaluation) for op in ops):
            raise PermissionDenied("synthetic cause ACL refusal")
        return write(ops, context, request_id=request_id)

    monkeypatch.setattr(port, "write", denied)
    try:
        with pytest.raises(SchedulerBlocked, match="publish_denied"):
            w.service.tick()
        assert journal.pending("operation") is not None
        status = w.client.get_system_record(cast(RemoteDependencyService, w.service).status_id)
        assert status is not None and status.payload["reason"] == "publish_denied"
        assert not w.requests()
    finally:
        journal.close()


def test_remote_admission_avoids_full_snapshot_but_tick_keeps_it(tmp_path, monkeypatch):
    w = world()
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    snapshots = []
    original = w.client.snapshot

    def snapshot():
        snapshots.append(True)
        return original()

    monkeypatch.setattr(port, "snapshot", snapshot, raising=False)
    try:
        event = w.change("source")
        assert snapshots == []
        assert w.client.is_processed(w.service.consumer, event.event_id)
        w.service.handle(event)
        assert snapshots == []  # duplicates still read current + processed state
        w.ticks()
        assert snapshots and len(w.requests("a")) == 1
    finally:
        journal.close()


@pytest.mark.parametrize("fault", ["missing", "denied"])
def test_remote_admission_still_proves_initial_version(tmp_path, monkeypatch, fault):
    from ear_storage_client import PermissionDenied

    w = World()
    w.seed("x")
    created = w.storage.events[-1]
    w.node("a", ["x"])
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")

    def get_version(ident, version):
        assert ident == w.ids["x"] and version == 1
        if fault == "denied":
            raise PermissionDenied("synthetic initial version denial")
        return None

    monkeypatch.setattr(port, "get_version", get_version, raising=False)
    try:
        w.service.handle(created)
        w.ticks()
        assert not w.requests() and not w.steps()
        assert w.triggers()[0].status == "blocked"
        assert w.triggers()[0].reason == (
            "read_denied" if fault == "denied" else "causal_versions_unavailable"
        )
    finally:
        journal.close()


def test_remote_admission_rechecks_authority_after_current_read(tmp_path, monkeypatch):
    w = world()
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    created = w.storage.events[0]

    def revoked_get(ident):
        value = w.client.get(ident)
        port.proof["coverage_complete"] = False
        return value

    monkeypatch.setattr(port, "get", revoked_get, raising=False)
    try:
        with pytest.raises(SchedulerBlocked):
            w.service.handle(created)
        assert not w.client.is_processed(w.service.consumer, created.event_id)
        with pytest.raises(SchedulerBlocked, match="coverage_incomplete"):
            w.service.records()
        assert not w.requests() and not stored_triggers(w)
    finally:
        journal.close()


@pytest.mark.parametrize("late_finish", [False, True])
def test_remote_idle_and_finish_poll_do_not_materialize_business_graph(
    tmp_path, monkeypatch, late_finish
):
    """Record-only work must not hydrate all business history per worker cycle."""
    w = World()
    w.seed("source")
    w.node("a", ["source"])
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    original = w.client.snapshot
    snapshots = []

    def snapshot():
        snapshots.append(True)
        return original()

    monkeypatch.setattr(port, "snapshot", snapshot, raising=False)
    try:
        w.service.tick()
        assert snapshots == []
        w.change("source")
        w.service.tick()
        assert snapshots and len(w.requests("a")) == 1
        snapshots.clear()
        for _ in range(3):
            w.service.tick()  # no Finish yet: not an empty or inferred completion
        assert snapshots == []
        assert w.steps("a")[0].state == "requested"
        assert snapshots == []
        if late_finish:
            now = w.service.clock()
            w.service.clock = lambda: now + w.service.request_timeout + 1
            w.service.tick()
            assert w.steps("a")[0].reason == "timeout"
            assert snapshots == [] and len(w.requests("a")) == 1
        w.finish("a", "unchanged")
        w.service.tick()
        assert snapshots == []
        assert w.steps("a")[0].state == "done"
        assert w.triggers()[0].status == "closed"
        assert snapshots == []
        # No cross-tick cache: another input must rebuild the graph and publish.
        w.change("source")
        w.service.tick()
        assert snapshots and len(w.requests("a")) == 2
    finally:
        journal.close()


def test_remote_record_only_tick_still_fences_authority(tmp_path, monkeypatch):
    w = World()
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    query = w.client.query

    def changed(query_args):
        result = query(query_args)
        port.proof["coverage_revision"] = "changed-during-record-read"
        return result

    monkeypatch.setattr(port, "query", changed, raising=False)
    try:
        with pytest.raises(SchedulerBlocked, match="scheduler_state_changed"):
            w.service.tick()
        assert not w.requests()
    finally:
        journal.close()


def test_remote_pending_cause_followup_still_requires_graph_snapshot(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from ear_storage_client import PermissionDenied

    w = World()
    w.seed("source")
    w.node("a", ["source"])
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    try:
        w.change("source")
        w.service.tick()
        w.change("source")
        w.service.tick()
        w.finish("a", "unchanged")
        w.service.tick()  # Finish is authoritative; pending causes still need a graph decision.
        step = w.steps("a")[0]
        assert step.state == "done" and step.pending_causes

        @contextmanager
        def denied_snapshot():
            raise PermissionDenied("synthetic graph access revoked")
            yield  # pragma: no cover

        monkeypatch.setattr(port, "snapshot", denied_snapshot, raising=False)
        with pytest.raises(SchedulerBlocked, match="read_or_write_denied"):
            w.service.tick()
        assert w.steps("a")[0].pending_causes == step.pending_causes
        assert len(w.requests("a")) == 1
        assert w.triggers()[0].status != "closed"
    finally:
        journal.close()


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("fault", ["revoked", "incomplete", "malformed", "missing", "legacy"])
def test_remote_records_require_current_authority(tmp_path, monkeypatch, populated, fault):
    from ear_storage_client import PermissionDenied
    from ear_storage_client.remote_errors import ProtocolError

    w = world()
    if populated:
        w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    try:
        before = w.service.records()
        assert bool(before) == populated
        calls = []

        def unexpected_query(*args):
            calls.append("query")
            return w.client.query(*args)

        monkeypatch.setattr(port, "query", unexpected_query, raising=False)
        if fault in {"revoked", "legacy"}:

            def denied():
                raise PermissionDenied("access denied")

            port.state_hook = denied
            error, reason = PermissionDenied, "access denied"
        elif fault == "incomplete":
            port.proof["coverage_complete"] = False
            error, reason = SchedulerBlocked, "coverage_incomplete"
        elif fault == "malformed":

            def malformed():
                raise ProtocolError("invalid scheduler state")

            port.state_hook = malformed
            error, reason = SchedulerBlocked, "scheduler_state_invalid"
        else:
            port.scheduler_state = None  # type: ignore[assignment]
            error, reason = SchedulerBlocked, "scheduler_authority_unavailable"
        # A matching module string and a previous successful read are not authority.
        assert port.module == "dependency"
        with pytest.raises(error, match=reason):
            w.service.records()
        assert calls == [] and port.sends == [] and not journal.pending_ids()
    finally:
        journal.close()


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("mutation", ["revision", "owner", "revoked"])
def test_remote_records_fence_entire_read_before_return(tmp_path, monkeypatch, populated, mutation):
    from dataclasses import replace

    from ear_storage_client import PermissionDenied

    w = world()
    if populated:
        for _ in range(3):
            w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    pages = []

    def query(args):
        page = w.client.query(replace(args, limit=1))
        pages.append(page)
        if page.next_cursor is None:
            if mutation == "revision":
                port.proof["coverage_revision"] = "changed-during-records"
            elif mutation == "owner":
                port.proof["owners"]["alpha"]["revision"] = 2
            else:

                def denied():
                    raise PermissionDenied("access denied")

                port.state_hook = denied
        return page

    monkeypatch.setattr(port, "query", query, raising=False)
    try:
        error = PermissionDenied if mutation == "revoked" else SchedulerBlocked
        with pytest.raises(error):
            w.service.records()
        assert len(pages) == (3 if populated else 1)
        assert not port.sends and not journal.pending_ids()
    finally:
        journal.close()


def test_remote_records_are_fresh_bounded_reads_without_snapshot(tmp_path, monkeypatch):
    from dataclasses import replace

    from ear_dependency.records import put
    from ear_storage_client import Context

    w = world()
    for _ in range(3):
        w.change("source")
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    queries = []

    def query(args):
        queries.append(args)
        return w.client.query(replace(args, limit=1))

    def no_snapshot():
        pytest.fail("record reads must not hydrate business snapshots")

    monkeypatch.setattr(port, "query", query, raising=False)
    monkeypatch.setattr(port, "snapshot", no_snapshot, raising=False)
    try:
        first = w.service.records()
        assert len(first) == len(queries) == 3
        assert port.state_calls == 2
        # No record cache or proof cache between public reads.
        record = first[0].model_copy(deep=True)
        envelope = w.client.get_system_record(record.id)
        assert envelope is not None
        record.reason = "synthetic_new_reason"
        w.client.write([put(record, envelope.revision)], Context.system()).check()
        port.proof["coverage_revision"] = "new-authorized-round"
        second = w.service.records()
        assert second[0].reason == "synthetic_new_reason" and first[0].reason is None
        assert len(queries) == 6 and port.state_calls == 4
        assert cast(RemoteDependencyService, w.service)._round is None
        w.service.max_records = 2
        with pytest.raises(RuntimeError, match="record_limit"):
            w.service.records()
        assert not port.sends and not journal.pending_ids()
    finally:
        journal.close()


def test_remote_record_scan_waits_for_unknown_journal_recovery(tmp_path, monkeypatch):
    w = world()
    port = FakePort(w.client)
    journal = remote(w, port, tmp_path / "journal")
    queries = []

    def query(args):
        assert journal.pending("operation") is None
        queries.append(args)
        return w.client.query(args)

    monkeypatch.setattr(port, "query", query, raising=False)
    try:
        port.fail = "state"
        with pytest.raises(CommitUnknown):
            w.change("source")
        pending = journal.pending("operation")
        assert pending is not None
        port.fail = "state"
        with pytest.raises(CommitUnknown):
            w.service.tick()
        assert journal.pending("operation") == pending
        assert queries == [] and not w.requests()
        w.service.tick()
        assert journal.pending("operation") is None and queries
        assert sum(item == pending for item in port.sends) == 3
        assert len(w.requests("a")) == 1
    finally:
        journal.close()
