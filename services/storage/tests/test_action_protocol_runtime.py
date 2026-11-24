"""Explicit action-v2 host over isolated PG/NATS and real storage/API/④ HTTP.

Synthetic suggestion + adapter only; no model, calendar, .env, ear-dev or production.
Uses trusted core_deployment bootstrap/identity helpers, never DEFAULT mutations.
Owner queue and business work are separately bounded to prove accepted != applied.
Crash windows are injected journal failures plus app/journal reopen, not OS power loss.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from contextlib import ExitStack, suppress

import httpx
import pytest
from ear_api.app import create_app as api_app
from ear_api.config import Settings
from ear_api.execution import ExternalResult
from ear_api.execution_journal import ExecutionJournal, JournalUnavailable
from ear_contracts import DEFAULT_REGISTRY, SemanticRecord, new_id
from ear_extraction.domain import Action, Generated, Interval
from ear_extraction.orchestration import CONSUMER, ExtractionService
from ear_extraction.suggestions import create_suggestion
from ear_storage import migrate
from ear_storage.auth import AuthOperator
from ear_storage.core_deployment import bootstrap_core, core_identity_policies, init_core_identities
from ear_storage.http import create_app as storage_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.service import StorageService
from ear_storage_client import Context, Query
from ear_storage_client.remote import RemoteStorageClient
from nats.js.errors import NotFoundError

from .test_api_gateway_integration import Harness, loopback
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
ACTION = {
    "kind": "reminder",
    "title": "Synthetic action-v2 reminder",
    "due_at": "2026-11-05T10:00:00Z",
    "notes": "Synthetic private action",
}


class NoModel:
    def complete(self, call):
        raise AssertionError("review/execution must never call a model")


class SyntheticAdapter:
    name = "synthetic_runtime"
    reliable_idempotency = True

    def __init__(self, path, owner, sid):
        self.path, self.owner, self.sid = path, owner, sid
        self.calls = []
        self.lookups = []
        self.results = {}
        self.mode = "success"
        self.lookup_unknown = False
        self.after_lookup = None

    @staticmethod
    def result(key, digest, outcome):
        return ExternalResult(
            action_key=key,
            action_hash=digest,
            outcome=outcome,
            external_ref="synthetic_private_external_ref" if outcome == "succeeded" else None,
        )

    def lookup(self, action_key: str, action_hash: str) -> ExternalResult:
        key, digest = action_key, action_hash
        self.lookups.append(key)
        if self.after_lookup:
            self.after_lookup()
        if self.lookup_unknown:
            return self.result(key, digest, "unknown")
        return self.results.get(key, self.result(key, digest, "not_executed"))

    def execute(self, action_key: str, action_hash: str, action: Action) -> ExternalResult:
        key, digest = action_key, action_hash
        # Observe DURABLE intent and actual remote ④ acknowledgement inside the call.
        with sqlite3.connect(self.path) as db:
            persisted = json.loads(db.execute("SELECT body FROM execution").fetchone()[0])
        current = self.owner.get(self.sid)
        assert current.payload["state"] == "executing"
        assert persisted["state"] == "executing" and persisted["delivery"] == "applied"
        assert persisted["owner_version"] == current.version
        assert persisted["owner_receipt"] == current.payload["receipt"]
        assert persisted["owner_execution"] == current.payload["execution"]
        assert persisted["action_key"] == key and persisted["action_hash"] == digest
        assert action == Action.model_validate(ACTION)
        self.calls.append(key)
        if self.mode == "timeout":
            raise TimeoutError("synthetic private timeout")
        if self.mode == "fail":
            return self.result(key, digest, "not_executed")
        self.results[key] = self.result(key, digest, "succeeded")
        return self.results[key]


@pytest.fixture
def runtime(pg_dsn, nats_server, tmp_path):
    before = DEFAULT_REGISTRY.to_json()
    migrate(pg_dsn)
    assert bootstrap_core(pg_dsn)
    output = tmp_path / "core-identities" / "identities.json"
    assert init_core_identities(pg_dsn, output)
    identities = json.loads(output.read_text())["credentials"]
    assert DEFAULT_REGISTRY.to_json() == before
    operator = AuthOperator(pg_dsn)
    no_grant = operator.issue_principal("api", read_types=["feedback"])
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_ACTION_" + suffix,
        prefix="ear_action_" + suffix,
        timeout=2,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    api_stack = ExitStack()
    try:
        with ExitStack() as stack:
            stack.callback(api_stack.close)
            url = stack.enter_context(
                loopback(storage_app(StorageService(pg_dsn, relay_config=cfg)))
            )
            owner = stack.enter_context(RemoteStorageClient(identities["extraction"]["token"], url))
            owner.write(
                create_suggestion(
                    new_id(),
                    1,
                    Generated(action=Action.model_validate(ACTION)),
                    Interval.model_validate(
                        {"start": "2026-11-05T09:00:00Z", "end": "2026-11-05T10:00:00Z"}
                    ),
                ),
                Context.system(),
            ).check()
            sid = owner.query(Query("action_suggestion")).items[0].id
            path = tmp_path / "execution.sqlite3"
            adapter = SyntheticAdapter(path, owner, sid)
            token, denied_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            config = Settings.model_validate(
                {
                    "storage_url": url,
                    "execution_action_v2": True,
                    "execution_journal": path,
                    "bindings": [
                        {
                            "subject": "synthetic",
                            "role": "user",
                            "token": token,
                            "storage_token": identities["api_user"]["token"],
                        },
                        {
                            "subject": "denied",
                            "role": "user",
                            "token": denied_token,
                            "storage_token": no_grant.token,
                        },
                    ],
                }
            )
            rt = Harness(
                dsn=pg_dsn,
                cfg=cfg,
                owner=owner,
                operator=operator,
                pid=identities["api_user"]["principal_id"],
                adapter=adapter,
                sid=sid,
                path=path,
                base=f"/v1/actions/{sid}",
                denied_token=denied_token,
            )

            def restart_api():
                api_stack.close()  # Real lifespan closes the private journal/OS lock.
                api_url = api_stack.enter_context(loopback(api_app(config, adapter=adapter)))
                rt.http = api_stack.enter_context(
                    httpx.Client(
                        base_url=api_url,
                        trust_env=False,
                        timeout=10,
                        headers={"Authorization": "Bearer " + token},
                    )
                )

            rt.restart_api = restart_api
            restart_api()
            rt.worker = ExtractionService(owner, NoModel())
            rt.worker.subscribe()
            yield rt
    finally:

        async def cleanup():
            async with reader(cfg) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(cfg.stream)

        asyncio.run(cleanup())


def state(rt):
    reply = rt.http.get(rt.base + "/execution")
    assert reply.status_code == 200, reply.text
    assert "synthetic_private_external_ref" not in reply.text
    assert "Synthetic private action" not in reply.text
    return reply.json()


def versions(rt):
    value = state(rt)
    return {k: value[k] for k in ("expected_version", "expected_revision")}


def pump(rt):
    reply = rt.http.post(rt.base + "/pump", json=versions(rt))
    assert reply.status_code == 202, reply.text
    return reply.json()


def enqueue(rt):
    stats = asyncio.run(run_relay(RelayStore(rt.dsn, rt.cfg), once=True))
    assert stats.delivered > 0
    assert rt.owner.run_until_idle(max_deliveries=50) > 0
    assert not rt.owner.deadletters(CONSUMER)


def drain(rt):
    enqueue(rt)
    rt.worker.drain()
    assert all(run.payload["status"] == "succeeded" for run in rt.worker.runs())


def approve(rt):
    reply = rt.http.post(
        "/v1/feedback",
        json={
            "request_id": new_id(),
            "module": "extraction",
            "command": {
                "kind": "review_suggestion",
                "target_id": rt.sid,
                "expected_version": 1,
                "decision": "approved",
            },
        },
    )
    assert reply.status_code == 202, reply.text
    return reply.json()["feedback_id"]


def prepare(rt):
    reply = rt.http.post(rt.base + "/prepare", json={"expected_version": 2})
    assert reply.status_code == 202, reply.text
    return reply.json()


def start(rt):
    approve(rt)
    drain(rt)
    prepare(rt)
    drain(rt)
    return pump(rt)


def settle(rt):
    drain(rt)
    return pump(rt)


def test_real_success_requires_actual_owner_ack_not_accepted_or_queued(runtime):
    rt = runtime
    assert rt.http.post(rt.base + "/prepare", json={"expected_version": 1}).status_code == 409
    fid = approve(rt)
    assert rt.http.get(f"/v1/feedback/{fid}/status").json()["status"] == "pending"
    assert rt.http.post(rt.base + "/prepare", json={"expected_version": 1}).status_code == 409
    enqueue(rt)
    assert state(rt)["suggestion_state"] == "pending"
    assert rt.http.post(rt.base + "/prepare", json={"expected_version": 1}).status_code == 409
    rt.worker.drain()
    assert state(rt)["suggestion_state"] == "approved"
    assert rt.http.post(rt.base + "/prepare", json={"expected_version": 1}).status_code == 409
    prepare(rt)
    assert pump(rt)["feedback_delivery_state"] == "accepted"
    enqueue(rt)
    assert pump(rt)["suggestion_state"] == "approved"
    assert not rt.adapter.calls and not rt.adapter.lookups
    rt.worker.drain()
    ready = state(rt)
    assert ready["suggestion_state"] == "executing"
    assert state(rt) == ready and not rt.adapter.calls
    stale = versions(rt)
    for key in stale:
        assert (
            rt.http.post(rt.base + "/pump", json={**stale, key: stale[key] - 1}).status_code == 409
        )
    result = pump(rt)
    assert result["executor_state"] == "succeeded"
    assert result["feedback_delivery_state"] == "accepted"
    assert result["suggestion_state"] == "executing"
    final = settle(rt)
    assert final["suggestion_state"] == "executed"
    assert final["feedback_delivery_state"] == "applied"
    assert final["automatic_worker_enabled"] is False
    history = rt.owner.history(rt.sid)
    assert [r.payload["state"] for r in history] == ["pending", "approved", "executing", "executed"]
    rt.restart_api()
    assert pump(rt)["feedback_delivery_state"] == "applied"
    assert len(rt.adapter.calls) == 1


@pytest.mark.parametrize("operation", ["verify", "confirm_executed", "retry"])
def test_real_unknown_lookup_user_attestation_and_explicit_risk_retry(runtime, operation):
    rt = runtime
    rt.adapter.mode = "timeout"
    start(rt)
    assert settle(rt)["suggestion_state"] == "unknown"
    key = rt.adapter.calls[0]
    recovery = state(rt)["recovery"]
    assert recovery["requires_user_verification"] and recovery["can_retry"]
    assert recovery["automatic_retry_allowed"] is False
    for _ in range(2):
        pump(rt)
    assert len(rt.adapter.calls) == 1
    args = {**versions(rt), "operation": operation}
    ack = {"confirm_executed": "acknowledge_executed", "retry": "acknowledge_duplicate_risk"}
    if operation in ack:
        assert rt.http.post(rt.base + "/recovery", json=args).status_code == 409
        args[ack[operation]] = True
    lookups = len(rt.adapter.lookups)
    for field in ("expected_version", "expected_revision"):
        assert (
            rt.http.post(rt.base + "/recovery", json={**args, field: args[field] - 1}).status_code
            == 409
        )
    assert len(rt.adapter.lookups) == lookups
    if operation == "verify":
        # A conclusive absence is still not a retry grant during recovery.
        assert rt.http.post(rt.base + "/recovery", json=args).status_code == 202
        assert state(rt)["suggestion_state"] == "unknown" and len(rt.adapter.calls) == 1
        current = rt.owner.get(rt.sid)
        rt.adapter.results[key] = rt.adapter.result(
            key, current.payload["execution"]["action_hash"], "succeeded"
        )
        args.update(versions(rt))
    response = rt.http.post(rt.base + "/recovery", json=args)
    assert response.status_code == 202, response.text
    assert state(rt)["suggestion_state"] == "unknown" and len(rt.adapter.calls) == 1
    if operation == "retry":
        pump(rt)
        enqueue(rt)
        pump(rt)
        assert len(rt.adapter.calls) == 1
        rt.worker.drain()
        rt.adapter.mode = "success"
        rt.adapter.lookup_unknown = True  # explicit risk approval, reliable same action key
        pump(rt)
    final = settle(rt)
    assert final["suggestion_state"] == "executed"
    assert len(rt.adapter.calls) == (2 if operation == "retry" else 1)
    assert set(rt.adapter.calls) == {key}
    if operation == "confirm_executed":
        assert final["recovery"]["confirmation_source"] == "user"
        assert not rt.adapter.results
        assert "external_ref" not in rt.owner.get(rt.sid).payload


def test_real_failed_risk_retry_uses_same_key_and_second_owner_handshake(runtime):
    rt = runtime
    rt.adapter.mode = "fail"
    start(rt)
    assert settle(rt)["suggestion_state"] == "failed"
    first = rt.adapter.calls[0]
    args = {**versions(rt), "operation": "retry", "acknowledge_duplicate_risk": True}
    assert rt.http.post(rt.base + "/recovery", json=args).status_code == 202
    assert rt.http.post(rt.base + "/recovery", json=args).status_code == 409
    pump(rt)
    assert rt.adapter.calls == [first]
    rt.adapter.mode = "success"
    settle(rt)
    assert settle(rt)["suggestion_state"] == "executed"
    assert rt.adapter.calls == [first, first]


def test_real_crash_window_after_effect_reopens_journal_and_only_looks_up(runtime, monkeypatch):
    rt = runtime
    original = ExecutionJournal.save
    armed = True

    def lose_result(self, entry, **kwargs):
        nonlocal armed
        if armed and entry.state == "succeeded":
            armed = False
            raise JournalUnavailable()  # Effect happened, local result was never committed.
        return original(self, entry, **kwargs)

    approve(rt)
    drain(rt)
    prepare(rt)
    drain(rt)
    args = versions(rt)
    monkeypatch.setattr(ExecutionJournal, "save", lose_result)
    reply = rt.http.post(rt.base + "/pump", json=args)
    assert reply.status_code == 503 and reply.json() == {"code": "execution_journal_unavailable"}
    assert len(rt.adapter.calls) == 1
    with sqlite3.connect(rt.path) as db:
        row = json.loads(db.execute("SELECT body FROM execution").fetchone()[0])
    assert row["state"] == "executing" and row["pending_command"] is None
    rt.restart_api()
    assert state(rt)["executor_state"] == "executing"
    assert pump(rt)["executor_state"] == "succeeded"
    assert settle(rt)["suggestion_state"] == "executed"
    assert len(rt.adapter.calls) == 1
    assert rt.path.stat().st_mode & 0o777 == 0o600


def test_real_no_grant_revocation_and_no_request_evidence(runtime):
    rt = runtime
    rt.adapter.mode = "timeout"
    start(rt)
    settle(rt)
    args = versions(rt)
    endpoints = [
        ("GET", "/execution", None),
        ("POST", "/prepare", {"expected_version": 2}),
        ("POST", "/pump", args),
        ("POST", "/recovery", {**args, "operation": "verify"}),
        (
            "POST",
            "/recovery",
            {**args, "operation": "confirm_executed", "acknowledge_executed": True},
        ),
        ("POST", "/recovery", {**args, "operation": "retry", "acknowledge_duplicate_risk": True}),
    ]
    before = len(rt.adapter.lookups)
    for method, route, body in endpoints:
        denied = rt.http.request(
            method,
            rt.base + route,
            json=body,
            headers={"Authorization": "Bearer " + rt.denied_token},
        )
        # Storage get(id) hides an unreadable object rather than leaking its existence.
        assert denied.status_code == 404 and denied.json() == {"code": "not_found"}
        assert (
            rt.http.request(
                method, rt.base + route, json=body, headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
    for key in ("url", "provider", "key", "action", "outcome", "receipt", "external_ref"):
        reply = rt.http.post(
            rt.base + "/recovery", json={**args, "operation": "verify", key: "forged"}
        )
        assert reply.status_code == 422
    rt.operator.set_grants(rt.pid, read_types=["feedback"])
    for method, route, body in endpoints:
        assert rt.http.request(method, rt.base + route, json=body).status_code == 404
    rt.operator.set_grants(rt.pid, read_types=core_identity_policies()["api_user"].reads)
    assert state(rt)["suggestion_state"] == "unknown"
    rt.operator.revoke(rt.pid)
    for method, route, body in endpoints:
        assert rt.http.request(method, rt.base + route, json=body).status_code == 401
    assert len(rt.adapter.lookups) == before and len(rt.adapter.calls) == 1


def test_real_revoke_during_lookup_blocks_effect(runtime):
    rt = runtime
    approve(rt)
    drain(rt)
    prepare(rt)
    drain(rt)
    args = versions(rt)
    rt.adapter.after_lookup = lambda: rt.operator.set_grants(rt.pid, read_types=["feedback"])
    reply = rt.http.post(rt.base + "/pump", json=args)
    assert reply.status_code == 404 and reply.json() == {"code": "not_found"}
    assert not rt.adapter.calls
    current = rt.owner.get(rt.sid)
    assert isinstance(current, SemanticRecord) and current.payload["state"] == "executing"


@pytest.mark.parametrize("phase", ["lookup", "execute"])
@pytest.mark.parametrize(
    "factory,updates",
    [
        pytest.param("copy", {"outcome": "provider_timeout"}, id="copy-unknown-outcome"),
        pytest.param("construct", {"outcome": None}, id="construct-null-outcome"),
        pytest.param("copy", {"outcome": False}, id="copy-bool-outcome"),
        pytest.param(
            "construct", {"outcome": "succeeded", "external_ref": True}, id="construct-bool-ref"
        ),
        pytest.param("copy", {"outcome": "succeeded", "external_ref": b"ref"}, id="copy-bytes-ref"),
        pytest.param(
            "copy",
            {"outcome": "succeeded", "external_ref": "synthetic", "extra": "private"},
            id="copy-extra-success",
        ),
        pytest.param("construct", {"action_hash": "0" * 64}, id="construct-wrong-hash"),
        pytest.param("copy", {"action_key": "ear_action_" + "0" * 64}, id="copy-wrong-key"),
    ],
)
def test_real_malformed_typed_evidence_stops_unknown(runtime, phase, factory, updates):
    rt = runtime
    original = getattr(rt.adapter, phase)
    callbacks = []

    def malformed(key, digest, *args):
        callbacks.append(key)
        # execute still checks durable intent and the real owner acknowledgement.
        good = original(key, digest, *args)
        if factory == "construct":
            return ExternalResult.model_construct(**{**good.model_dump(), **updates})
        return good.model_copy(update=updates)

    setattr(rt.adapter, phase, malformed)
    result = start(rt)
    assert result["executor_state"] == "unknown"
    assert result["suggestion_state"] == "executing"
    final = settle(rt)
    assert final["executor_state"] == final["suggestion_state"] == "unknown"
    assert final["recovery"]["requires_user_verification"] is True
    assert final["recovery"]["confirmation_source"] is None
    assert final["recovery"]["automatic_retry_allowed"] is False
    with sqlite3.connect(rt.path) as db:
        persisted = json.loads(db.execute("SELECT body FROM execution").fetchone()[0])
    assert persisted["state"] == "unknown" and persisted["external_ref"] is None
    assert [r.payload["state"] for r in rt.owner.history(rt.sid)] == [
        "pending",
        "approved",
        "executing",
        "unknown",
    ]
    rt.restart_api()
    for _ in range(2):
        assert pump(rt)["executor_state"] == "unknown"
    assert len(rt.adapter.calls) == (1 if phase == "execute" else 0)
    assert len(callbacks) == 1
    reply = rt.http.post(rt.base + "/recovery", json={**versions(rt), "operation": "retry"})
    assert reply.status_code == 409
    assert len(rt.adapter.calls) == (1 if phase == "execute" else 0)
