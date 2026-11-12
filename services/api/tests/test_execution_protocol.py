"""v2 handoff with actual ④ consumer and private SQLite; synthetic effects only."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from ear_api.execution import ExecutionPump, ExecutionService, ExternalResult
from ear_api.execution_journal import ExecutionJournal, JournalUnavailable
from ear_api.models import PUBLIC_COMMAND_FIELDS, FeedbackInput
from ear_api.upstream import GatewayError
from ear_contracts import DataChanged, NewRawRecord, SemanticRecord, Source, new_id
from ear_extraction.domain import Action, Command, Generated, Interval
from ear_extraction.orchestration import ExtractionService
from ear_extraction.suggestions import create_suggestion
from ear_storage_client import AppendRaw, Context, MemoryStorage, Query, Supersede, Update

from .test_execution import ACTION, Crash, FeedbackClient, NoModel, SyntheticAdapter


class ProtocolWorld:
    def __init__(self, path):
        self.path = path
        self.storage = MemoryStorage()
        self.client = FeedbackClient(self.storage)
        self.owner = self.storage.client("extraction")
        self.consumer = ExtractionService(self.owner, NoModel())
        self.adapter = SyntheticAdapter()
        self.journal = ExecutionJournal(path)
        self.service = ExecutionPump(self.client, self.journal, self.adapter)
        ops = create_suggestion(
            new_id(),
            1,
            Generated(action=Action.model_validate(ACTION)),
            Interval.model_validate({"start": ACTION["start"], "end": ACTION["end"]}),
        )
        self.owner.write(ops, Context.system()).check()
        self.sid = self.owner.query(Query("action_suggestion")).items[0].id

    @property
    def current(self):
        record = self.owner.get(self.sid)
        assert isinstance(record, SemanticRecord)
        return record

    def drain(self):
        for event in list(self.storage.events):
            if isinstance(event, DataChanged) and event.data_type == "feedback":
                self.consumer.handle_event(event)
        self.consumer.drain()

    def approve(self):
        fid = new_id()
        raw = NewRawRecord(
            id=fid,
            data_type="feedback",
            event_start=datetime.fromisoformat(ACTION["start"]),
            event_end=datetime.fromisoformat(ACTION["start"]),
            source=Source(device="synthetic_user", channel="api", user="default"),
            adapter_version="synthetic",
            dedup_key=fid,
            payload={
                "module": "extraction",
                "command": Command(
                    kind="review_suggestion",
                    target_id=self.sid,
                    expected_version=self.current.version,
                    decision="approved",
                ).model_dump(mode="json"),
            },
        )
        self.client.write([AppendRaw(raw)], Context.feedback(fid), request_id=fid).check()
        assert self.current.payload["state"] == "pending"
        self.drain()
        assert self.current.payload["state"] == "approved"

    def start(self):
        self.approve()
        entry = self.service.prepare(self.sid, self.current.version)
        assert entry.delivery == "accepted" and not self.adapter.calls
        self.drain()
        assert self.current.payload["state"] == "executing"
        return self.service.pump(self.sid)

    def settle(self):
        self.drain()
        return self.service.pump(self.sid)

    def restart(self):
        self.journal.close()
        self.journal = ExecutionJournal(self.path)
        self.service = ExecutionPump(self.client, self.journal, self.adapter)

    def versions(self):
        entry = self.journal.get(self.sid)
        assert entry is not None
        return {
            "expected_version": self.current.version,
            "expected_revision": entry.revision,
        }


@pytest.fixture
def protocol(tmp_path):
    world = ProtocolWorld(tmp_path / "execution.sqlite3")
    try:
        yield world
    finally:
        world.journal.close()


def test_two_phase_actual_owner_ack_and_durable_intent_before_effect(protocol):
    w = protocol
    with pytest.raises(GatewayError, match="suggestion_not_approved"):
        w.service.prepare(w.sid, 1)
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    for _ in range(3):
        assert w.service.pump(w.sid).delivery == "accepted"
    assert w.current.version == 2 and not w.adapter.calls and not w.adapter.lookups
    w.restart()
    assert w.service.pump(w.sid).revision == entry.revision
    w.drain()
    assert w.current.version == 3 and w.current.payload["state"] == "executing"
    original = w.adapter.execute

    def guarded(key, digest, action):
        local = w.journal.get(w.sid)
        assert local.state == "executing" and local.delivery == "applied"
        assert local.owner_version == w.current.version == 3
        assert local.owner_execution == w.current.payload["execution"]
        assert local.owner_receipt == w.current.payload["receipt"]
        with sqlite3.connect(w.path) as db:
            persisted = json.loads(db.execute("SELECT body FROM execution").fetchone()[0])
            assert persisted["state"] == "executing" and persisted["owner_version"] == 3
        return original(key, digest, action)

    w.adapter.execute = guarded
    entry = w.service.pump(w.sid)
    assert entry.state == "succeeded" and entry.delivery == "accepted"
    assert w.current.payload["state"] == "executing"
    assert w.settle().delivery == "applied"
    assert [r.payload["state"] for r in w.owner.history(w.sid)] == [
        "pending",
        "approved",
        "executing",
        "executed",
    ]
    w.restart()
    for _ in range(3):
        w.service.pump(w.sid)
    assert len(w.adapter.calls) == 1


@pytest.mark.parametrize("mode", ["timeout", "crash_before", "crash_after", "fail"])
def test_result_and_restart_are_not_permission_to_repeat(protocol, mode):
    w = protocol
    w.adapter.mode = mode
    if mode.startswith("crash"):
        with pytest.raises(Crash):
            w.start()
        assert w.journal.get(w.sid).state == "executing"
    else:
        w.start()
    w.restart()
    w.service.pump(w.sid)
    entry = w.settle()
    expected = "executed" if mode == "crash_after" else "failed" if mode == "fail" else "unknown"
    assert w.current.payload["state"] == expected
    for _ in range(3):
        w.service.pump(w.sid)
    assert len(w.adapter.calls) == 1
    if expected == "unknown":
        request = w.service.recovery_request(w.sid)
        assert request["requires_user_verification"] and request["can_retry"]
        assert request["expected_version"] == w.current.version
        assert request["expected_revision"] == entry.revision
        assert request["automatic_retry_allowed"] is False


def test_unknown_initial_lookup_and_reconcile_absence_never_execute(protocol):
    w = protocol
    w.adapter.lookup_unknown = True
    w.start()
    w.settle()
    key = w.journal.get(w.sid).action_key
    w.adapter.lookup_unknown = False
    for _ in range(3):
        w.service.pump(w.sid)
        w.service.reconcile(w.sid, **w.versions())
    assert not w.adapter.calls
    assert w.journal.get(w.sid).action_key == key
    assert w.current.payload["state"] == "unknown"


def test_reconcile_success_is_lookup_only_and_waits_for_terminal_owner(protocol):
    w = protocol
    w.adapter.mode = "timeout"
    entry = w.start()
    w.settle()
    w.adapter.results[entry.action_key] = w.adapter.result(
        entry.action_key, entry.action_hash, "succeeded"
    )
    result = w.service.reconcile(w.sid, **w.versions())
    assert result.delivery == "accepted" and result.state == "succeeded"
    assert w.current.payload["state"] == "unknown"
    w.settle()
    assert w.current.payload["state"] == "executed"
    assert len(w.adapter.calls) == 1


def test_user_confirmation_is_attestation_not_fabricated_external_reference(protocol):
    w = protocol
    w.adapter.mode = "timeout"
    w.start()
    w.settle()
    with pytest.raises(GatewayError, match="execution_confirmation_required"):
        w.service.confirm_executed(w.sid, **w.versions(), acknowledge_executed=False)
    with pytest.raises(TypeError):
        w.service.confirm_executed(
            w.sid, **w.versions(), acknowledge_executed=True, external_ref="forged"
        )
    entry = w.service.confirm_executed(w.sid, **w.versions(), acknowledge_executed=True)
    assert entry.external_ref is None and entry.user_confirmed
    assert w.current.payload["state"] == "unknown"
    w.settle()
    assert w.current.payload["state"] == "executed"
    assert w.current.payload["execution"]["operation"] == "confirm_executed"
    assert w.current.payload["execution"]["acknowledge_executed"] is True
    assert "external_ref" not in w.current.payload
    assert len(w.adapter.calls) == 1
    assert w.service.recovery_request(w.sid)["confirmation_source"] == "user"


@pytest.mark.parametrize("initial", ["timeout", "fail"])
def test_explicit_risk_retry_repeats_handshake_with_same_key(protocol, initial):
    w = protocol
    w.adapter.mode = initial
    first = w.start()
    w.settle()
    with pytest.raises(GatewayError, match="duplicate_risk_confirmation_required"):
        w.service.retry(w.sid, **w.versions(), acknowledge_duplicate_risk=False)
    old_versions = w.versions()
    entry = w.service.retry(w.sid, **old_versions, acknowledge_duplicate_risk=True)
    assert entry.action_key == first.action_key and entry.attempt == 2
    for _ in range(3):
        w.service.pump(w.sid)
    assert len(w.adapter.calls) == 1
    with pytest.raises(GatewayError, match="execution_revision_conflict"):
        w.service.retry(w.sid, **old_versions, acknowledge_duplicate_risk=True)
    w.drain()
    w.adapter.mode = "success"
    w.adapter.lookup_unknown = True  # explicitly acknowledged risk, same reliable key
    w.service.pump(w.sid)
    w.settle()
    assert len(w.adapter.calls) == 2
    assert {key for key, _ in w.adapter.calls} == {first.action_key}
    assert w.current.payload["state"] == "executed"


@pytest.mark.parametrize("operation", ["reconcile", "retry", "confirm_executed"])
@pytest.mark.parametrize("wrong", ["version", "revision"])
def test_stale_user_recovery_blocks_before_adapter(protocol, operation, wrong):
    w = protocol
    w.adapter.mode = "timeout"
    w.start()
    w.settle()
    args = w.versions()
    args["expected_" + wrong] -= 1
    if operation == "retry":
        args["acknowledge_duplicate_risk"] = True
    if operation == "confirm_executed":
        args["acknowledge_executed"] = True
    lookups = len(w.adapter.lookups)
    with pytest.raises(GatewayError):
        getattr(w.service, operation)(w.sid, **args)
    assert len(w.adapter.lookups) == lookups and len(w.adapter.calls) == 1


@pytest.mark.parametrize("phase", ["start", "result"])
def test_lost_feedback_reply_replays_same_envelope_never_effect(protocol, phase):
    w = protocol
    w.approve()
    w.client.fail_after = phase == "start"
    entry = w.service.prepare(w.sid, 2)
    if phase == "start":
        assert entry.delivery == "unknown"
        previous = w.client.writes[-1]
        assert w.service.pump(w.sid).delivery == "accepted"
        assert w.client.writes[-1] == previous
        assert not w.adapter.calls
    w.drain()
    w.client.fail_after = phase == "result"
    entry = w.service.pump(w.sid)
    if phase == "result":
        assert entry.delivery == "unknown"
        previous = w.client.writes[-1]
        w.restart()
        assert w.service.pump(w.sid).delivery == "accepted"
        assert w.client.writes[-1] == previous
    w.settle()
    assert len(w.adapter.calls) == 1


@pytest.mark.parametrize(
    "mutation", ["version", "receipt", "identity", "state", "action", "supersede"]
)
def test_wrong_owner_ack_never_executes(protocol, mutation):
    w = protocol
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    cmd = entry.pending_command
    payload = {
        **w.current.payload,
        "state": "executing",
        "receipt": cmd.receipt,
        "execution": cmd.execution.model_dump(mode="json"),
    }
    if mutation == "receipt":
        payload["receipt"] = "other"
    elif mutation == "identity":
        payload["execution"]["attempt"] = 9
    elif mutation == "state":
        payload["state"] = "approved"
    elif mutation == "action":
        payload["action"] = {**ACTION, "title": "Not approved"}
    w.owner.write([Update("action_suggestion", w.sid, 2, payload)], Context.system()).check()
    if mutation == "version":
        w.owner.write(
            [Update("action_suggestion", w.sid, 3, {**payload, "other": 1})], Context.system()
        ).check()
    if mutation == "supersede":
        w.owner.write([Supersede("action_suggestion", w.sid, 3)], Context.system()).check()
    assert w.service.pump(w.sid).delivery == "blocked"
    assert not w.adapter.calls


def test_mutation_during_lookup_and_after_effect_fail_closed(protocol):
    w = protocol
    w.approve()
    w.service.prepare(w.sid, 2)
    w.drain()

    def change():
        r = w.current
        w.owner.write(
            [Update("action_suggestion", w.sid, r.version, {**r.payload, "state": "unknown"})],
            Context.system(),
        ).check()

    w.adapter.before_lookup_return = change
    assert w.service.pump(w.sid).delivery == "blocked"
    assert not w.adapter.calls


@pytest.mark.parametrize("failure", ["owner_ack", "intent", "result"])
def test_journal_failure_cannot_bypass_gate_or_reexecute(protocol, monkeypatch, failure):
    w = protocol
    w.approve()
    w.service.prepare(w.sid, 2)
    w.drain()
    original = w.journal.save

    def save(entry, **kwargs):
        if (
            (failure == "owner_ack" and entry.owner_version == 3 and entry.state == "prepared")
            or (failure == "intent" and entry.state == "executing")
            or (failure == "result" and entry.state == "succeeded")
        ):
            raise JournalUnavailable()
        return original(entry, **kwargs)

    monkeypatch.setattr(w.journal, "save", save)
    with pytest.raises(JournalUnavailable):
        w.service.pump(w.sid)
    assert len(w.adapter.calls) == (failure == "result")
    w.restart()
    w.service.pump(w.sid)
    w.settle()
    assert len(w.adapter.calls) == 1


def test_parallel_and_duplicate_pumps_single_effect(protocol):
    w = protocol
    w.approve()
    w.service.prepare(w.sid, 2)
    w.drain()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: w.service.pump(w.sid), range(16)))
    assert len(w.adapter.calls) == 1
    w.settle()
    assert w.current.payload["state"] == "executed"


def test_protocol_journal_cannot_fall_through_legacy_process_or_http_verify(protocol):
    w = protocol
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    legacy = ExecutionService(w.client, w.journal, w.adapter)
    with pytest.raises(GatewayError, match="execution_protocol_pump_required"):
        legacy.process(w.sid)
    with pytest.raises(GatewayError, match="execution_protocol_pump_required"):
        legacy.verify(w.sid, entry.revision)
    with pytest.raises(GatewayError, match="both_versions"):
        w.service.verify(w.sid, entry.revision)
    assert not w.adapter.calls


def test_legacy_entry_not_promoted_or_given_new_key(protocol):
    w = protocol
    w.approve()
    w.adapter.mode = "timeout"
    legacy = ExecutionService(w.client, w.journal, w.adapter)
    entry = legacy.process(w.sid)
    with pytest.raises(GatewayError, match="legacy_execution"):
        w.service.pump(w.sid)
    assert w.journal.get(w.sid) == entry
    assert len(w.adapter.calls) == 1


def test_public_commands_cannot_submit_internal_protocol_or_fake_evidence(protocol):
    assert "action_result" not in PUBLIC_COMMAND_FIELDS
    w = protocol
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    with pytest.raises(ValueError):
        FeedbackInput(
            request_id=new_id(),
            module="extraction",
            command=entry.pending_command.model_dump(mode="json"),
        ).normalized_command()
    with pytest.raises(ValueError):
        FeedbackInput(
            request_id=new_id(),
            module="extraction",
            command={
                "kind": "review_suggestion",
                "target_id": w.sid,
                "expected_version": 2,
                "decision": "approved",
                "execution": entry.pending_command.execution.model_dump(),
            },
        ).normalized_command()


@pytest.mark.parametrize("bad", ["wrong_key", "missing_ref", "exception"])
def test_bad_adapter_evidence_is_unknown(protocol, bad):
    w = protocol

    def execute(key, digest, action):
        if bad == "exception":
            raise RuntimeError("private detail")
        return ExternalResult(
            action_key="ear_action_" + "0" * 64 if bad == "wrong_key" else key,
            action_hash=digest,
            outcome="succeeded",
            external_ref=None if bad == "missing_ref" else "synthetic",
        )

    w.adapter.execute = execute
    w.start()
    w.settle()
    assert w.current.payload["state"] == "unknown"


def test_existing_json_rows_read_without_rewrite_and_update_with_cas(protocol):
    w = protocol
    w.approve()
    w.adapter.mode = "timeout"
    old = ExecutionService(w.client, w.journal, w.adapter).process(w.sid)
    assert old is not None
    w.journal.close()
    body = old.model_dump(mode="json")
    for name in (
        "protocol",
        "pending_command",
        "owner_version",
        "owner_state",
        "owner_receipt",
        "owner_execution",
        "attempt",
        "retry_acknowledged",
        "user_confirmed",
    ):
        body.pop(name)
    text = json.dumps(body)
    with sqlite3.connect(w.path) as db:
        db.execute("UPDATE execution SET body=?", (text,))
    w.restart()
    assert w.journal.get(w.sid) == old
    with sqlite3.connect(w.path) as db:
        assert db.execute("SELECT body FROM execution").fetchone()[0] == text
    service = ExecutionService(w.client, w.journal, w.adapter)
    assert service.verify(w.sid, old.revision).state == "unknown"


def test_crash_after_initial_journal_insert_resumes_handshake_not_effect(protocol, monkeypatch):
    w = protocol
    w.approve()
    original = w.journal.save

    def save(entry, **kwargs):
        if entry.pending_command is not None:
            raise Crash()
        return original(entry, **kwargs)

    monkeypatch.setattr(w.journal, "save", save)
    with pytest.raises(Crash):
        w.service.prepare(w.sid, 2)
    assert not w.adapter.calls
    w.restart()
    assert w.service.pump(w.sid).delivery == "accepted"
    assert w.current.payload["state"] == "approved"
    assert not w.adapter.calls
    w.settle()
    w.settle()
    assert len(w.adapter.calls) == 1 and w.current.payload["state"] == "executed"


def test_crash_after_feedback_acceptance_persist_gap_observes_actual_owner(protocol, monkeypatch):
    w = protocol
    w.approve()
    original = w.journal.save

    def save(entry, **kwargs):
        if entry.delivery == "accepted":
            raise Crash()
        return original(entry, **kwargs)

    monkeypatch.setattr(w.journal, "save", save)
    with pytest.raises(Crash):
        w.service.prepare(w.sid, 2)
    assert not w.adapter.calls
    w.drain()
    w.restart()
    w.service.pump(w.sid)
    w.settle()
    assert len(w.adapter.calls) == 1
    assert w.current.payload["state"] == "executed"


def test_owner_change_after_external_success_keeps_result_but_blocks_delivery(protocol):
    w = protocol
    original = w.adapter.execute

    def execute(key, digest, action):
        result = original(key, digest, action)
        current = w.current
        w.owner.write(
            [
                Update(
                    "action_suggestion",
                    w.sid,
                    current.version,
                    {**current.payload, "other": "concurrent change"},
                )
            ],
            Context.system(),
        ).check()
        return result

    w.adapter.execute = execute
    entry = w.start()
    assert entry.state == "succeeded" and entry.delivery == "blocked"
    w.restart()
    for _ in range(3):
        assert w.service.pump(w.sid).delivery == "blocked"
    assert len(w.adapter.calls) == 1
    assert w.current.payload["state"] == "executing"


def test_authorization_failure_after_lookup_stops_effect(protocol):
    w = protocol
    w.approve()
    w.service.prepare(w.sid, 2)
    w.drain()

    def revoked(_):
        raise GatewayError(401, "revoked")

    def revoke():
        w.client.get = revoked

    w.adapter.before_lookup_return = revoke
    with pytest.raises(GatewayError, match="revoked"):
        w.service.pump(w.sid)
    assert not w.adapter.calls


def test_lookup_proves_success_before_execute(protocol):
    w = protocol
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    w.adapter.results[entry.action_key] = w.adapter.result(
        entry.action_key, entry.action_hash, "succeeded"
    )
    w.settle()
    w.settle()
    assert w.current.payload["state"] == "executed" and not w.adapter.calls


def test_risk_retry_crash_does_not_reuse_ack_to_execute_twice(protocol):
    w = protocol
    w.adapter.mode = "timeout"
    w.start()
    w.settle()
    w.service.retry(w.sid, **w.versions(), acknowledge_duplicate_risk=True)
    w.drain()
    w.adapter.mode = "crash_before"
    with pytest.raises(Crash):
        w.service.pump(w.sid)
    w.restart()
    w.service.pump(w.sid)
    w.settle()
    for _ in range(3):
        w.service.pump(w.sid)
    assert len(w.adapter.calls) == 2
    assert w.current.payload["state"] == "unknown"
    assert len({key for key, _ in w.adapter.calls}) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("action_key", "ear_action_" + "0" * 64),
        ("action_hash", "0" * 64),
        ("approved_version", 3),
        ("protocol", "legacy"),
        ("adapter", "other"),
    ],
)
def test_journal_identity_is_immutable(protocol, field, value):
    w = protocol
    w.approve()
    entry = w.service.prepare(w.sid, 2)
    with pytest.raises(JournalUnavailable):
        w.journal.save(
            entry.model_copy(update={field: value, "revision": entry.revision + 1}), previous=entry
        )
    assert w.journal.get(w.sid) == entry


def test_changed_adapter_or_missing_journal_cannot_reclaim_executing_suggestion(protocol):
    w = protocol
    w.approve()
    w.service.prepare(w.sid, 2)
    w.drain()
    w.adapter.name = "other_synthetic"
    with pytest.raises(GatewayError, match="adapter_changed"):
        w.service.pump(w.sid)
    # A new unrelated empty journal is not a recovery strategy. It must refuse.
    with ExecutionJournal(w.path.parent / "another.sqlite3") as journal:
        service = ExecutionPump(w.client, journal, w.adapter)
        with pytest.raises(GatewayError, match="suggestion_not_approved"):
            service.prepare(w.sid, w.current.version)
    assert not w.adapter.calls


def test_terminal_feedback_redelivery_and_stale_owner_commands_are_not_rebased(protocol):
    w = protocol
    w.start()
    entry = w.journal.get(w.sid)
    old_command = entry.pending_command
    w.settle()
    before = w.current
    w.drain()
    assert w.current == before
    # New feedback identity with the OLD command is blocked, not an idempotence exception.
    from ear_extraction.domain import Blocked
    from ear_extraction.suggestions import suggestion_operations

    with pytest.raises(Blocked, match="version"):
        suggestion_operations(w.owner, old_command)
    assert w.current == before
    assert len(w.adapter.calls) == 1


MALFORMED_EVIDENCE = [
    pytest.param({"outcome": "provider_timeout"}, id="unknown-outcome"),
    pytest.param({"outcome": None}, id="null-outcome"),
    pytest.param({"outcome": False}, id="bool-outcome"),
    pytest.param({"outcome": b"not_executed"}, id="bytes-outcome"),
    pytest.param({"action_key": "ear_action_" + "0" * 64}, id="wrong-key"),
    pytest.param({"action_hash": "0" * 64}, id="wrong-hash"),
    pytest.param({"action_key": None}, id="null-key"),
    pytest.param({"action_hash": False}, id="bool-hash"),
    pytest.param({"action_key": 123}, id="integer-key"),
    pytest.param({"action_hash": []}, id="list-hash"),
    pytest.param({"outcome": "succeeded", "external_ref": None}, id="success-missing-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": ""}, id="success-empty-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": "x" * 513}, id="success-long-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": 123}, id="success-integer-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": True}, id="success-bool-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": b"ref"}, id="success-bytes-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": ["ref"]}, id="success-list-ref"),
    pytest.param({"outcome": "succeeded", "external_ref": {"id": "ref"}}, id="success-dict-ref"),
    pytest.param({"external_ref": False}, id="absence-bool-ref"),
    pytest.param({"external_ref": b"ref"}, id="absence-bytes-ref"),
]


@pytest.mark.parametrize("updates", MALFORMED_EVIDENCE)
@pytest.mark.parametrize("factory", ["construct", "copy"])
@pytest.mark.parametrize("phase", ["lookup", "execute"])
@pytest.mark.parametrize("legacy", [False, True], ids=["v2", "legacy"])
def test_typed_malformed_evidence_is_unknown(protocol, updates, factory, phase, legacy):
    w = protocol
    calls = []

    def malformed(key, digest, *args):
        calls.append((key, digest))
        good = ExternalResult(action_key=key, action_hash=digest, outcome="not_executed")
        if factory == "construct":
            return ExternalResult.model_construct(**{**good.model_dump(), **updates})
        return good.model_copy(update=updates)

    setattr(w.adapter, phase, malformed)
    service = w.service
    if legacy:
        w.approve()
        service = ExecutionService(w.client, w.journal, w.adapter)
        entry = service.process(w.sid)
    else:
        entry = w.start()
    assert entry is not None
    assert entry.state == "unknown" and entry.external_ref is None
    assert len(calls) == 1 and not w.adapter.calls
    if legacy:
        assert entry.error_code == "external_result_unknown"
        assert all(
            r.payload["command"]["kind"] == "review_suggestion"
            for r in w.client.inner.query(Query("feedback")).items
        )
        assert service.verify(w.sid, entry.revision).state == "unknown"
    else:
        assert entry.pending_command is not None
        assert entry.pending_command.outcome == "unknown"
        assert w.settle().state == "unknown"
        assert w.current.payload["state"] == "unknown"
        assert w.service.reconcile(w.sid, **w.versions()).state == "unknown"
    # Once unknown, neither ordinary pumping nor process restart grants another execute.
    if not legacy:
        w.restart()
        service = w.service
    for _ in range(2):
        result = service.process(w.sid)
        assert result is not None and result.state == "unknown"
    assert not w.adapter.calls
    assert len(calls) == (1 if phase == "execute" else 2 + 2 * legacy)


@pytest.mark.parametrize("phase", ["lookup", "execute"])
@pytest.mark.parametrize("outcome", ["succeeded", "not_executed"])
@pytest.mark.parametrize("extra_source", ["copy", "subclass"])
def test_extra_fields_in_typed_evidence_are_not_silently_dropped(
    protocol, phase, outcome, extra_source
):
    from pydantic import ConfigDict

    class ExtendedResult(ExternalResult):
        model_config = ConfigDict(extra="allow")

    w = protocol
    calls = []

    def malformed(key, digest, *args):
        calls.append(key)
        fields = {
            "action_key": key,
            "action_hash": digest,
            "outcome": outcome,
            "external_ref": "synthetic" if outcome == "succeeded" else None,
        }
        if extra_source == "subclass":
            return ExtendedResult.model_construct(**fields, unexpected="private detail")
        return ExternalResult.model_construct(**fields).model_copy(
            update={"unexpected": "private detail"}
        )

    setattr(w.adapter, phase, malformed)
    assert w.start().state == "unknown"
    assert w.settle().state == "unknown"
    assert w.current.payload["state"] == "unknown"
    assert not w.adapter.calls and len(calls) == 1
    assert w.journal.get(w.sid).external_ref is None


@pytest.mark.parametrize("field", ["action_key", "action_hash"])
@pytest.mark.parametrize("phase", ["lookup", "execute"])
def test_matching_bytes_identity_is_not_coerced_to_valid_evidence(protocol, field, phase):
    w = protocol

    def malformed(key, digest, *args):
        good = w.adapter.result(key, digest, "succeeded")
        return good.model_copy(update={field: getattr(good, field).encode()})

    setattr(w.adapter, phase, malformed)
    assert w.start().state == "unknown"
    assert w.settle().state == "unknown"
    assert not w.adapter.calls


def test_malformed_lookup_retry_still_requires_explicit_risk_and_owner_ack(protocol):
    w = protocol
    original = w.adapter.lookup

    def malformed(key, digest):
        return original(key, digest).model_copy(update={"outcome": "provider_timeout"})

    w.adapter.lookup = malformed
    first = w.start()
    w.settle()
    assert not w.adapter.calls
    with pytest.raises(GatewayError, match="duplicate_risk_confirmation_required"):
        w.service.retry(w.sid, **w.versions(), acknowledge_duplicate_risk=False)
    w.service.retry(w.sid, **w.versions(), acknowledge_duplicate_risk=True)
    assert w.service.pump(w.sid).delivery == "accepted"
    assert not w.adapter.calls
    w.drain()
    assert w.service.pump(w.sid).state == "succeeded"
    assert w.settle().state == "succeeded"
    assert len(w.adapter.calls) == 1 and w.adapter.calls[0][0] == first.action_key
