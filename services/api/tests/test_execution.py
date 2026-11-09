"""Synthetic adapters only: no real calendar, no production storage, no external URLs."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from ear_api.app import create_app
from ear_api.execution import PROTOCOL_BLOCKER, ExecutionService, ExternalResult
from ear_api.execution_journal import ExecutionJournal, JournalUnavailable
from ear_api.upstream import GatewayError
from ear_contracts import DataChanged, RawRecord, new_id
from ear_extraction.orchestration import ExtractionService
from ear_storage_client import Context, Create, MemoryStorage, Query, Supersede, Update
from ear_storage_client.engine import canonical_json
from ear_storage_client.remote_errors import CommitUnknown
from fastapi.testclient import TestClient

from .conftest import headers

ACTION = {
    "kind": "calendar",
    "title": "Synthetic meeting",
    "notes": "Synthetic note",
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-01T01:00:00Z",
}


class Crash(BaseException):
    pass


class SyntheticAdapter:
    name = "synthetic"
    reliable_idempotency = True

    def __init__(self):
        self.calls = []
        self.lookups = []
        self.results = {}
        self.mode = "success"
        self.lookup_unknown = False
        self.before_lookup_return = None

    def result(self, key, digest, outcome):
        return ExternalResult(
            action_key=key,
            action_hash=digest,
            outcome=outcome,
            external_ref="synthetic_external_ref" if outcome == "succeeded" else None,
        )

    def lookup(self, action_key, action_hash):
        self.lookups.append(action_key)
        if self.before_lookup_return:
            self.before_lookup_return()
        if self.lookup_unknown:
            return self.result(action_key, action_hash, "unknown")
        return self.results.get(action_key, self.result(action_key, action_hash, "not_executed"))

    def execute(self, action_key, action_hash, action):
        self.calls.append((action_key, action.model_dump(mode="json")))
        if self.mode == "timeout":
            raise TimeoutError("PRIVATE adapter detail")
        if self.mode == "fail":
            return self.result(action_key, action_hash, "not_executed")
        if self.mode == "crash_before":
            raise Crash()
        result = self.result(action_key, action_hash, "succeeded")
        self.results[action_key] = result
        if self.mode == "crash_after":
            raise Crash()
        return result


class FeedbackClient:
    """Models stable remote request receipts, including a lost reply after commit."""

    module = "api"

    def __init__(self, storage):
        self.inner = storage.client("api")
        self.receipts = {}
        self.writes = []
        self.fail_after = False

    def get(self, id):
        return self.inner.get(id)

    def write(self, ops, context, *, request_id):
        fingerprint = canonical_json({"ops": [op.record.model_dump(mode="json") for op in ops]})
        self.writes.append((request_id, fingerprint, context))
        if request_id in self.receipts:
            old, result = self.receipts[request_id]
            assert old == fingerprint
        else:
            result = self.inner.write(ops, context)
            self.receipts[request_id] = (fingerprint, result)
        if self.fail_after:
            self.fail_after = False
            raise CommitUnknown("PRIVATE storage detail")
        return result


class NoModel:
    def complete(self, call):
        raise AssertionError("Suggestion review/results must not call a model")


class World:
    def __init__(self, path):
        self._path = path
        self.storage = MemoryStorage()
        self.client = FeedbackClient(self.storage)
        self.owner = self.storage.client("extraction")
        self.consumer = ExtractionService(self.owner, NoModel())
        self.adapter = SyntheticAdapter()
        self.journal = ExecutionJournal(path)
        self.service = ExecutionService(self.client, self.journal, self.adapter)
        self.sid = new_id()
        self.owner.write(
            [
                Create(
                    "action_suggestion",
                    self.sid,
                    {
                        "artifact_id": new_id(),
                        "artifact_version": 1,
                        "state": "approved",
                        "action": ACTION,
                    },
                )
            ],
            Context.system(),
        ).check()

    def drain_feedback(self):
        for event in list(self.storage.events):
            if isinstance(event, DataChanged) and event.data_type == "feedback":
                self.consumer.handle_event(event)
        self.consumer.drain()

    def restart(self):
        path = self.path
        self.journal.close()
        self.journal = ExecutionJournal(path)
        self.service = ExecutionService(self.client, self.journal, self.adapter)

    @property
    def path(self):
        return self._path


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "execution.sqlite3"
    value = World(path)
    try:
        yield value
    finally:
        value.journal.close()


def test_terminal_feedback_requires_actual_owner_consumer(world):
    entry = world.service.process(world.sid)
    assert entry.state == "succeeded" and entry.delivery == "accepted"
    assert world.client.get(world.sid).payload["state"] == "approved"
    feedback = world.client.inner.query(Query("feedback")).items
    assert len(feedback) == 1 and isinstance(feedback[0], RawRecord)
    assert feedback[0].payload["command"]["kind"] == "action_result"
    assert feedback[0].payload["command"]["outcome"] == "executed"
    assert all(op[2].root_feedback_id == op[0] and op[2].depth == 0 for op in world.client.writes)
    world.drain_feedback()
    assert world.client.get(world.sid).payload["state"] == "executed"
    assert world.service.process(world.sid).delivery == "applied"
    assert len(world.adapter.calls) == 1
    assert len(world.owner.history(world.sid)) == 2


def test_explicit_failure_does_not_masquerade_as_success_or_retry(world):
    world.adapter.mode = "fail"
    entry = world.service.process(world.sid)
    assert entry.state == "failed" and entry.delivery == "accepted"
    world.drain_feedback()
    assert world.client.get(world.sid).payload["state"] == "failed"
    world.restart()
    assert world.service.process(world.sid).state == "failed"
    assert len(world.adapter.calls) == 1
    run = world.owner.query(Query("extraction_run")).items[0]
    assert run.payload["status"] == "succeeded"  # processing the failure receipt succeeded


@pytest.mark.parametrize("mode", ["timeout", "crash_before", "crash_after"])
def test_unknown_and_crash_recovery_never_automatically_execute_again(world, mode):
    world.adapter.mode = mode
    if mode.startswith("crash"):
        with pytest.raises(Crash):
            world.service.process(world.sid)
        assert world.journal.get(world.sid).state == "executing"
    else:
        assert world.service.process(world.sid).state == "unknown"
    world.restart()
    entry = world.service.process(world.sid)
    assert entry.state == ("succeeded" if mode == "crash_after" else "unknown")
    for _ in range(3):
        world.service.process(world.sid)
    assert len(world.adapter.calls) == 1
    assert len(world.client.inner.query(Query("feedback")).items) == (mode == "crash_after")
    assert world.client.get(world.sid).payload["state"] == "approved"


def test_unknown_lookup_stops_even_first_execution_and_does_not_write_failed(world):
    world.adapter.lookup_unknown = True
    assert world.service.process(world.sid).state == "unknown"
    world.adapter.lookup_unknown = False  # absence later is not permission to repeat
    assert world.service.process(world.sid).state == "unknown"
    assert not world.adapter.calls
    assert not world.client.inner.query(Query("feedback")).items


def test_recover_unknown_by_authoritative_lookup_success_only(world):
    world.adapter.mode = "timeout"
    entry = world.service.process(world.sid)
    world.adapter.results[entry.action_key] = world.adapter.result(
        entry.action_key, entry.action_hash, "succeeded"
    )
    world.restart()
    assert world.service.process(world.sid).state == "succeeded"
    assert len(world.adapter.calls) == 1


def test_lost_feedback_ack_replays_identical_envelope_not_external_call(world):
    world.client.fail_after = True
    entry = world.service.process(world.sid)
    assert entry.state == "succeeded" and entry.delivery == "unknown"
    world.restart()
    assert world.service.process(world.sid).delivery == "accepted"
    assert len(world.adapter.calls) == 1
    assert world.client.writes[0] == world.client.writes[1]
    assert len(world.client.inner.query(Query("feedback")).items) == 1


def test_duplicate_events_and_concurrent_calls_have_one_external_effect(world):
    event = world.storage.events[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: world.service.handle_event(event), range(16)))
    assert len(world.adapter.calls) == 1
    assert len(world.client.inner.query(Query("feedback")).items) == 1
    world.restart()
    world.service.handle_event(event)
    assert len(world.adapter.calls) == 1


@pytest.mark.parametrize("state", ["pending", "rejected", "executed", "failed", "unknown"])
def test_nonapproved_current_state_never_executes_even_if_event_is_stale(world, state):
    current = world.client.get(world.sid)
    world.owner.write(
        [
            Update(
                "action_suggestion", world.sid, current.version, {**current.payload, "state": state}
            )
        ],
        Context.system(),
    ).check()
    world.service.handle_event(world.storage.events[0])
    assert not world.adapter.calls
    assert world.journal.get(world.sid) is None


def test_current_superseded_and_mutated_during_lookup_are_not_executed(world):
    def supersede():
        world.owner.write([Supersede("action_suggestion", world.sid, 1)], Context.system()).check()

    world.adapter.before_lookup_return = supersede
    assert world.service.process(world.sid).error_code == "suggestion_changed"
    assert not world.adapter.calls


def test_frozen_approved_action_cannot_be_replaced_after_unknown(world):
    world.adapter.mode = "timeout"
    old = world.service.process(world.sid)
    current = world.client.get(world.sid)
    world.owner.write(
        [
            Update(
                "action_suggestion",
                world.sid,
                1,
                {
                    **current.payload,
                    "action": {**ACTION, "title": "Changed"},
                },
            )
        ],
        Context.system(),
    ).check()
    entry = world.service.process(world.sid)
    assert entry.action_hash == old.action_hash
    assert entry.state == "unknown" and entry.delivery == "blocked"
    assert len(world.adapter.calls) == 1


@pytest.mark.parametrize(
    "action",
    [
        {**ACTION, "url": "https://do-not-contact.invalid"},
        {**ACTION, "kind": "shell", "command": "anything"},
        {**ACTION, "start": 1},
        {**ACTION, "title": " "},
        {**ACTION, "notes": "x" * 8193},
    ],
)
def test_no_arbitrary_actions_or_url_inputs(world, action):
    current = world.client.get(world.sid)
    world.owner.write(
        [
            Update(
                "action_suggestion",
                world.sid,
                1,
                {
                    **current.payload,
                    "action": action,
                },
            )
        ],
        Context.system(),
    ).check()
    with pytest.raises(ValueError if "url" in action or action["kind"] == "shell" else Exception):
        world.service.process(world.sid)
    assert not world.adapter.calls
    assert world.journal.get(world.sid) is None


def test_mismatched_adapter_evidence_is_unknown_not_success(world):
    def wrong(key, digest, action):
        return ExternalResult(
            action_key="ear_action_" + "0" * 64,
            action_hash=digest,
            outcome="succeeded",
            external_ref="not_for_this_action",
        )

    world.adapter.execute = wrong
    assert world.service.process(world.sid).state == "unknown"
    assert not world.client.inner.query(Query("feedback")).items


def test_changed_version_blocks_feedback_without_reexecuting(world):
    original = world.service._current
    calls = 0

    def changed(id):
        nonlocal calls
        calls += 1
        current = original(id)
        if calls == 3:
            world.owner.write(
                [
                    Update(
                        "action_suggestion",
                        id,
                        current.version,
                        {
                            **current.payload,
                            "state": "rejected",
                        },
                    )
                ],
                Context.system(),
            ).check()
            return original(id)
        return current

    world.service._current = changed
    entry = world.service.process(world.sid)
    assert entry.state == "succeeded" and entry.delivery == "blocked"
    assert len(world.adapter.calls) == 1
    assert not world.client.inner.query(Query("feedback")).items


def test_journal_is_protected_single_owner_and_corruption_fails_closed(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionJournal(path):
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(JournalUnavailable):
            ExecutionJournal(path)
    path.write_bytes(b"corrupt journal")
    with pytest.raises(JournalUnavailable):
        ExecutionJournal(path)


def test_journal_refuses_insecure_directory_symlinks_hardlinks_and_other_schema(tmp_path):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(JournalUnavailable):
        ExecutionJournal(insecure / "journal.db")
    path = tmp_path / "journal.db"
    with ExecutionJournal(path):
        pass
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    with pytest.raises(JournalUnavailable):
        ExecutionJournal(alias)
    alias.unlink()
    os.link(path, alias)
    with pytest.raises(JournalUnavailable):
        ExecutionJournal(path)
    alias.unlink()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(JournalUnavailable):
        ExecutionJournal(path)


def test_journal_failure_before_effect_does_not_execute(world):
    world.journal.close()
    with pytest.raises(JournalUnavailable):
        world.service.process(world.sid)
    assert not world.adapter.calls


def test_journal_failure_after_effect_recovers_by_lookup(world, monkeypatch):
    original = world.journal.save

    def fail_terminal(entry, **kwargs):
        if entry.state == "succeeded":
            raise JournalUnavailable()
        return original(entry, **kwargs)

    monkeypatch.setattr(world.journal, "save", fail_terminal)
    with pytest.raises(JournalUnavailable):
        world.service.process(world.sid)
    world.restart()
    assert world.service.process(world.sid).state == "succeeded"
    assert len(world.adapter.calls) == 1


def test_api_execution_is_explicitly_disabled_no_self_report_or_risk_bypass(gateway):
    client, fake, _ = gateway
    sid = new_id()
    fake.storage.client("extraction").write(
        [
            Create(
                "action_suggestion",
                sid,
                {
                    "action": ACTION,
                    "state": "approved",
                },
            )
        ],
        Context.system(),
    ).check()
    path = "/v1/actions/" + sid
    status = client.get(path + "/execution", headers=headers()).json()
    assert status["execution_enabled"] is False
    assert status["suggestion_state"] == "approved" and status["executor_state"] == "disabled"
    assert status["blocker"] == PROTOCOL_BLOCKER
    assert client.get(path + "/execution").status_code == 401
    before = len(fake.storage.events)
    for op in ("verify", "confirm_executed", "retry"):
        data = {"operation": op, "expected_revision": 1}
        expected = 409 if op == "retry" else 501
        assert client.post(path + "/recovery", headers=headers(), json=data).status_code == expected
        if op == "retry":
            data["acknowledge_duplicate_risk"] = True
            r = client.post(path + "/recovery", headers=headers(), json=data)
            assert r.status_code == 501 and r.json()["code"] == PROTOCOL_BLOCKER
    for extra in ({"outcome": "executed"}, {"receipt": "fake"}, {"url": "https://invalid"}):
        assert (
            client.post(
                path + "/recovery",
                headers=headers(),
                json={
                    "operation": "confirm_executed",
                    "expected_revision": 1,
                    **extra,
                },
            ).status_code
            == 422
        )
    assert len(fake.storage.events) == before


def test_local_status_requires_current_upstream_authorization(gateway, world):
    _, fake, config = gateway
    sid = world.sid
    fake.storage.client("extraction").write(
        [
            Create(
                "action_suggestion",
                sid,
                {
                    "action": ACTION,
                    "state": "approved",
                },
            )
        ],
        Context.system(),
    ).check()
    world.adapter.mode = "timeout"
    world.service.process(sid)
    with TestClient(create_app(config, execution=world.service)) as client:
        path = "/v1/actions/" + sid + "/execution"
        result = client.get(path, headers=headers())
        assert result.json()["executor_state"] == "unknown"
        assert "synthetic_external_ref" not in result.text
        fake.revoked.add(config.bindings[0].storage_token.get_secret_value())
        assert client.get(path, headers=headers()).status_code == 401


def test_http_review_and_terminal_result_have_distinct_actual_owner_states(gateway, world):
    client, fake, _ = gateway
    sid = new_id()
    owner = fake.storage.client("extraction")
    owner.write(
        [
            Create(
                "action_suggestion",
                sid,
                {
                    "state": "pending",
                    "action": ACTION,
                    "artifact_id": new_id(),
                    "artifact_version": 1,
                },
            )
        ],
        Context.system(),
    ).check()
    accepted = client.post(
        "/v1/feedback",
        headers=headers(),
        json={
            "request_id": new_id(),
            "module": "extraction",
            "command": {
                "kind": "review_suggestion",
                "target_id": sid,
                "expected_version": 1,
                "decision": "approved",
            },
        },
    )
    assert accepted.status_code == 202
    assert owner.get(sid).payload["state"] == "pending"
    consumer = ExtractionService(owner, NoModel())
    for event in list(fake.storage.events):
        if isinstance(event, DataChanged) and event.data_type == "feedback":
            consumer.handle_event(event)
    consumer.drain()
    assert owner.get(sid).payload["state"] == "approved"
    url = "/v1/feedback/" + accepted.json()["feedback_id"] + "/status"
    result = client.get(url, headers=headers()).json()
    assert result["status"] == "succeeded"
    assert result["status_scope"] == "feedback_processing_not_external_execution"
    assert not world.adapter.calls
    executor = ExecutionService(FeedbackClient(fake.storage), world.journal, world.adapter)
    execution = executor.process(sid)
    assert execution is not None and execution.delivery == "accepted"
    assert owner.get(sid).payload["state"] == "approved"
    for event in list(fake.storage.events):
        if isinstance(event, DataChanged) and event.data_type == "feedback":
            consumer.handle_event(event)
    consumer.drain()
    assert owner.get(sid).payload["state"] == "executed"
    execution = executor.process(sid)
    assert execution is not None and execution.delivery == "applied"
    assert len(world.adapter.calls) == 1


def seed_gateway_suggestion(fake, sid):
    fake.storage.client("extraction").write(
        [Create("action_suggestion", sid, {"action": ACTION, "state": "approved"})],
        Context.system(),
    ).check()


def test_verify_is_lookup_only_even_when_absence_becomes_conclusive(world):
    with pytest.raises(GatewayError, match="execution_not_started"):
        world.service.verify(world.sid, 1)
    assert not world.adapter.lookups and not world.adapter.calls
    world.adapter.lookup_unknown = True
    entry = world.service.process(world.sid)
    world.adapter.lookup_unknown = False
    for _ in range(3):
        entry = world.service.verify(world.sid, entry.revision)
        assert entry.state == "unknown"
    assert not world.adapter.calls
    assert not world.client.inner.query(Query("feedback")).items
    with pytest.raises(GatewayError, match="external_result_unconfirmed"):
        world.service._deliver(entry, world.client.get(world.sid))


def test_http_owned_journal_lifecycle_lookup_recovery_and_owner_audit(gateway, world):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.adapter.mode = "timeout"
    entry = world.service.process(world.sid)
    world.adapter.results[entry.action_key] = world.adapter.result(
        entry.action_key, entry.action_hash, "succeeded"
    )
    world.journal.close()
    managed = config.model_copy(update={"execution_journal": world.path})
    app = create_app(managed, adapter=world.adapter)
    path = "/v1/actions/" + world.sid
    before_lookups = len(world.adapter.lookups)
    with TestClient(app) as client:
        with pytest.raises(JournalUnavailable):
            ExecutionJournal(world.path)
        status = client.get(path + "/execution", headers=headers()).json()
        assert status["executor_state"] == "unknown"
        assert status["verification_enabled"] is True and status["execution_enabled"] is False
        data = {"operation": "verify", "expected_revision": entry.revision - 1}
        result = client.post(path + "/recovery", headers=headers(), json=data)
        assert result.status_code == 409
        assert result.json()["code"] == "execution_revision_conflict"
        assert len(world.adapter.lookups) == before_lookups
        data["expected_revision"] = entry.revision
        result = client.post(path + "/recovery", headers=headers(), json=data)
        assert result.status_code == 200
        assert result.json()["executor_state"] == "succeeded"
        assert result.json()["feedback_delivery_state"] == "accepted"
        assert "synthetic_external_ref" not in result.text
        assert len(world.adapter.calls) == 1
        feedback = fake.storage.client("api").query(Query("feedback")).items
        assert len(feedback) == 1
        assert feedback[0].payload["command"]["outcome"] == "executed"
        owner = fake.storage.client("extraction")
        assert owner.get(world.sid).payload["state"] == "approved"
        consumer = ExtractionService(owner, NoModel())
        for event in list(fake.storage.events):
            if isinstance(event, DataChanged) and event.data_type == "feedback":
                consumer.handle_event(event)
        consumer.drain()
        assert owner.get(world.sid).payload["state"] == "executed"
        data["expected_revision"] = result.json()["journal_revision"]
        applied = client.post(path + "/recovery", headers=headers(), json=data)
        assert applied.json()["feedback_delivery_state"] == "applied"
    with ExecutionJournal(world.path) as reopened:
        restored = reopened.get(world.sid)
        assert restored is not None and restored.delivery == "applied"
    # Same configured app can restart without deleting/reinitializing durable intent.
    with TestClient(app) as client:
        status = client.get(path + "/execution", headers=headers()).json()
        assert status["executor_state"] == "succeeded"
        assert status["feedback_delivery_state"] == "applied"
    assert len(world.adapter.calls) == 1


@pytest.mark.parametrize("operation", ["verify", "confirm_executed", "retry"])
def test_recovery_checks_current_authority_and_never_risk_reexecutes(gateway, world, operation):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.adapter.mode = "timeout"
    entry = world.service.process(world.sid)
    with TestClient(create_app(config, execution=world.service)) as client:
        path = "/v1/actions/" + world.sid + "/recovery"
        data = {
            "operation": operation,
            "expected_revision": entry.revision,
            "acknowledge_duplicate_risk": True,
        }
        before = len(world.adapter.lookups)
        if operation != "verify":
            result = client.post(path, headers=headers(), json=data)
            assert result.status_code == 501 and result.json()["code"] == PROTOCOL_BLOCKER
            assert len(world.adapter.lookups) == before
        fake.revoked.add(config.bindings[0].storage_token.get_secret_value())
        assert client.post(path, headers=headers(), json=data).status_code == 401
        assert len(world.adapter.lookups) == before
    assert world.journal.get(world.sid).state == "unknown"  # borrowed journal remains open
    assert len(world.adapter.calls) == 1
    assert not fake.storage.client("api").query(Query("feedback")).items


def test_http_configured_journal_without_adapter_is_read_only_and_restart_safe(gateway, world):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.adapter.mode = "timeout"
    entry = world.service.process(world.sid)
    world.journal.close()
    managed = config.model_copy(update={"execution_journal": world.path})
    path = "/v1/actions/" + world.sid
    with TestClient(create_app(managed)) as client:
        status = client.get(path + "/execution", headers=headers()).json()
        assert status["executor_state"] == "unknown" and not status["verification_enabled"]
        result = client.post(
            path + "/recovery",
            headers=headers(),
            json={
                "operation": "verify",
                "expected_revision": entry.revision,
            },
        )
        assert result.status_code == 501
    with ExecutionJournal(world.path) as journal:
        restored = journal.get(world.sid)
        assert restored is not None and restored.state == "unknown"
    assert len(world.adapter.calls) == 1


def test_journal_unavailable_is_sanitized_503_not_missing_or_empty(gateway, world):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.journal.close()
    with TestClient(create_app(config, execution=world.service)) as client:
        result = client.get("/v1/actions/" + world.sid + "/execution", headers=headers())
        assert result.status_code == 503
        assert result.json() == {"code": "execution_journal_unavailable"}
        assert str(world.path) not in result.text


def test_managed_journal_bad_path_fails_startup_and_configuration_is_unambiguous(gateway, world):
    _, _, config = gateway
    invalid = config.model_copy(update={"execution_journal": world.path.parent / "missing" / "db"})
    with pytest.raises(JournalUnavailable), TestClient(create_app(invalid)):
        pass
    with pytest.raises(ValueError, match="execution_journal_required"):
        create_app(config, adapter=world.adapter)
    with pytest.raises(ValueError, match="ambiguous_execution_configuration"):
        create_app(config, execution=world.service, adapter=world.adapter)


@pytest.mark.parametrize("evidence_mode", ["unknown", "not_executed", "bad_key", "missing_ref"])
def test_http_verification_never_executes_or_emits_unconfirmed_terminal_feedback(
    gateway, world, evidence_mode
):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.adapter.lookup_unknown = True
    entry = world.service.process(world.sid)
    assert entry is not None and not world.adapter.calls

    def lookup(key, digest):
        return ExternalResult(
            action_key="ear_action_" + "0" * 64 if evidence_mode == "bad_key" else key,
            action_hash=digest,
            outcome=(
                "unknown"
                if evidence_mode == "unknown"
                else "not_executed"
                if evidence_mode == "not_executed"
                else "succeeded"
            ),
            external_ref=None if evidence_mode == "missing_ref" else "synthetic_external_ref",
        )

    world.adapter.lookup = lookup
    path = "/v1/actions/" + world.sid + "/recovery"
    with TestClient(create_app(config, execution=world.service)) as client:
        revision = entry.revision
        for _ in range(3):
            result = client.post(
                path,
                headers=headers(),
                json={
                    "operation": "verify",
                    "expected_revision": revision,
                },
            )
            assert result.status_code == 200
            status = result.json()
            assert status["executor_state"] == "unknown"
            assert status["automatic_retry_allowed"] is False
            assert status["requires_verification"] is True
            revision = status["journal_revision"]
        result = client.post(
            path,
            headers=headers(),
            json={
                "operation": "retry",
                "expected_revision": revision,
            },
        )
        assert result.status_code == 409
        assert result.json()["code"] == "duplicate_risk_confirmation_required"
        result = client.post(
            path,
            headers=headers(),
            json={
                "operation": "retry",
                "expected_revision": revision,
                "acknowledge_duplicate_risk": True,
            },
        )
        assert result.status_code == 501
    assert not world.adapter.calls
    assert not fake.storage.client("api").query(Query("feedback")).items


def test_http_lost_audit_ack_replays_same_feedback_without_external_reexecution(gateway, world):
    _, fake, config = gateway
    seed_gateway_suggestion(fake, world.sid)
    world.adapter.mode = "timeout"
    entry = world.service.process(world.sid)
    world.adapter.results[entry.action_key] = world.adapter.result(
        entry.action_key, entry.action_hash, "succeeded"
    )
    fake.fail_after_write = True
    path = "/v1/actions/" + world.sid + "/recovery"
    with TestClient(create_app(config, execution=world.service)) as client:
        first = client.post(
            path,
            headers=headers(),
            json={
                "operation": "verify",
                "expected_revision": entry.revision,
            },
        )
        assert first.status_code == 200
        assert first.json()["feedback_delivery_state"] == "unknown"
        revision = first.json()["journal_revision"]
        second = client.post(
            path,
            headers=headers(),
            json={
                "operation": "verify",
                "expected_revision": revision,
            },
        )
        assert second.status_code == 200
        assert second.json()["feedback_delivery_state"] == "accepted"
        assert len(fake.receipts) == 1
        assert len(fake.storage.client("api").query(Query("feedback")).items) == 1
    assert len(world.adapter.calls) == 1
