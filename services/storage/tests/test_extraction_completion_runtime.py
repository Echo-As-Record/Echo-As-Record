"""Opt-in real ④→⑦→① HTTP/PG/NATS, synthetic provider, no production endpoints.

Reuses the storage suite's isolated containers, relay and loopback server. Only
this fixture opts artifact into reevaluation/dependency enforcement. Completion
is a reserved marker identity, NOT a second job subscription. A fresh child
process exercises recovery without retaining worker/client state or credentials
in argv. The model provider is deliberately in-process and never uses a network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    LLMCallRecord,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    TracedCause,
    VersionRef,
    new_id,
    utcnow,
)
from ear_extraction.call_safety import DurableCalls
from ear_extraction.domain import Blocked, Call, Command, Rule
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import (
    COMPLETION_CONSUMER,
    CONSUMER,
    ExtractionService,
    reevaluation_run_id,
)
from ear_extraction.segmentation import RAW_TYPES
from ear_extraction.worker import tick
from ear_llm_gateway import Gateway, GatewayClient, ProviderResult
from ear_llm_gateway.http import create_app as gateway_app
from ear_llm_gateway.journal import Journal
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.http import create_app as storage_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.testing import make_raw, ts
from nats.js.errors import NotFoundError
from starlette.responses import Response

from .test_api_gateway_integration import Harness, loopback
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
READS = set(RAW_TYPES) | {
    "voiceprint",
    "person",
    "place",
    "attribution",
    "activity",
    "artifact",
    "rule",
    "action_suggestion",
    "feedback",
    "extraction_run",
    "extraction_completion",
    "llm_call",
}
START, END = ts("2026-11-05T09:00:00Z"), ts("2026-11-05T10:00:00Z")


@pytest.fixture
def completion_registry():
    types = dict(DEFAULT_REGISTRY.data_types)
    types["artifact"] = replace(
        types["artifact"], supports_reevaluation=True, requires_dependencies=True
    )
    return Registry(types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types)


class SyntheticProvider:
    def __init__(self):
        self.calls = []
        self.changed = True
        self.unknown = False
        self.before_return = lambda: None
        self.client: RemoteStorageClient | None = None

    def complete(self, call, *, model, timeout):
        self.calls.append(call)
        assert self.client is not None
        run = self.client.get_system_record(call.run_id)
        assert run is not None
        intent = run.payload["gateway_calls"][-1]
        assert intent["state"] == "sending" and intent["call_id"] == call.call_id
        fingerprint = hashlib.sha256(
            json.dumps(
                call.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        assert intent["request_fingerprint"] == fingerprint
        assert set(intent) == {"call_id", "purpose", "state", "request_fingerprint"}
        self.before_return()
        if self.unknown:
            raise TimeoutError("synthetic provider response lost")
        return ProviderResult(
            {
                "body_md": "synthetic revised output",
                "evidence_ids": [call.data["evidence"][0]["id"]],
            }
            if self.changed
            else {"unchanged": True}
        )


@pytest.fixture
def completion_env(pg_dsn, nats_server, completion_registry):
    # Bootstrap the explicit fixture registry into a NEW database; never upgrade
    # an already bootstrapped registry or mutate DEFAULT_REGISTRY globally.
    migrate(pg_dsn)
    bootstrap(pg_dsn, completion_registry)
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_IT_" + suffix,
        prefix="ear_it_" + suffix,
        timeout=2,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    try:
        yield pg_dsn, PostgresStorage(pg_dsn), cfg
    finally:

        async def cleanup():
            async with reader(cfg) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(cfg.stream)

        asyncio.run(cleanup())


@pytest.fixture
def runtime(completion_env, tmp_path):
    dsn, store, cfg = completion_env
    operator = AuthOperator(dsn)
    caller = operator.issue_principal(
        "extraction",
        read_types=READS,
        subscribe_types=["artifact", "feedback", "attribution", "person"],
    )
    for name in (CONSUMER, COMPLETION_CONSUMER):
        operator.reserve_consumer_identity(name, caller.principal_id)
    writer_id = operator.issue_principal("llm_gateway", read_types=READS)
    app = storage_app(StorageService(dsn, relay_config=cfg))
    fault = Harness(
        phase=None, committed=False, fired=False, contexts=[], preflights=[], gateway_loss=False
    )

    @app.middleware("http")
    async def observe_and_lose_response(request, call_next):
        if request.url.path == "/v1/llm/preflight":
            fault.preflights.append(await request.json())
        lose = False
        if request.url.path == "/v1/batches":
            body = await request.json()
            ops = body["ops"]
            fault.contexts.append(body["context"])
            phase = "finish" if any(op["op"] == "FinishReevaluation" for op in ops) else None
            for op in ops:
                if op["op"] == "PutSystemRecord" and op["body"]["data_type"] == "extraction_run":
                    payload = op["body"]["payload"]
                    calls = payload.get("gateway_calls", [])
                    if phase is None and payload.get("status") == "running":
                        phase = calls[-1]["state"] if calls else "claim"
            lose = fault.phase is not None and phase == fault.phase and not fault.fired
            if lose:
                fault.fired = True
                if not fault.committed:
                    return Response("synthetic unavailable before commit", status_code=503)
        response = await call_next(request)
        if lose:
            assert response.status_code == 200
            # Discard the successful reply on the actual TCP boundary. The SDK
            # must decode this as CommitUnknown, not infer whether it committed.
            return Response("synthetic lost commit response", status_code=200)
        return response

    provider = SyntheticProvider()
    journal = Journal(str(tmp_path / "synthetic.sqlite3"))
    try:
        with loopback(app) as url, RemoteStorageClient(writer_id.token, url) as writer:
            engine = Gateway(
                callers=lambda token: RemoteStorageClient(token, url),
                writer=writer,
                provider=provider,
                journal=journal,
                aliases={"default": "synthetic/model"},
            )
            assert engine.verifier is None
            gateway_http = gateway_app(engine)

            @gateway_http.middleware("http")
            async def discard_gateway_reply(request, call_next):
                response = await call_next(request)
                if request.url.path == "/v1/complete" and fault.gateway_loss:
                    assert response.status_code == 200
                    return Response("synthetic discarded provider success", status_code=200)
                return response

            with (
                loopback(gateway_http) as gateway_url,
                RemoteStorageClient(caller.token, url, module="extraction") as client,
                GatewayClient(gateway_url, caller.token, system=SYSTEM) as gateway,
            ):
                provider.client = client
                worker = ExtractionService(client, gateway)
                worker.subscribe()
                yield Harness(
                    dsn=dsn,
                    store=store,
                    cfg=cfg,
                    operator=operator,
                    caller=caller,
                    client=client,
                    worker=worker,
                    provider=provider,
                    writer=writer,
                    url=url,
                    gateway_url=gateway_url,
                    fault=fault,
                )
    finally:
        journal.close()


def relay(rt):
    outcome = asyncio.run(run_relay(RelayStore(rt.dsn, rt.cfg), once=True))
    assert outcome.delivered > 0


def seed_request(rt):
    transcript, voice, person, attr, activity, rule, artifact = [new_id() for _ in range(7)]
    rt.store.client("adapter").write(
        [
            AppendRaw(
                make_raw(
                    "transcript_segment",
                    {"text": "synthetic only"},
                    id=transcript,
                    start=START,
                    end=END,
                )
            ),
            AppendRaw(
                make_raw("voiceprint", {"local_speaker": "A"}, id=voice, start=START, end=END)
            ),
        ],
        Context.system(),
    ).check()
    rt.store.client("attribution").write(
        [
            Create("person", person, {"name": "Synthetic A"}),
            Create(
                "attribution",
                attr,
                {"subject_id": voice, "target_type": "person", "target_id": person},
            ),
        ],
        Context.system(),
    ).check()
    rt.client.write(
        [
            Create(
                "activity",
                activity,
                {"title_md": "Synthetic activity"},
                event_start=START,
                event_end=END,
            ),
            Create(
                "rule",
                rule,
                Rule(scope="synthetic", instruction="synthetic").model_dump(mode="json"),
            ),
            Create(
                "artifact",
                artifact,
                {
                    "activity_id": activity,
                    "rule_id": rule,
                    "rule_version": 1,
                    "body_md": "synthetic original output",
                    "evidence_ids": [transcript],
                },
                event_start=START,
                event_end=END,
            ),
            RegisterDependencies(
                "artifact",
                artifact,
                [
                    VersionRef(data_type=kind, id=id, version=1)
                    for kind, id in (("activity", activity), ("rule", rule), ("attribution", attr))
                ],
            ),
        ],
        Context.system(),
    ).check()
    roots = [make_raw("feedback", {"module": "attribution"}, start=utcnow()) for _ in range(3)]
    rt.store.client("api").write([AppendRaw(r) for r in roots], Context.system()).check()
    rt.store.client("attribution").write(
        [
            Update(
                "attribution",
                attr,
                1,
                {
                    "subject_id": voice,
                    "target_type": "person",
                    "target_id": person,
                    "revised": True,
                },
            ),
            Update("person", person, 1, {"name": "Synthetic B"}),
        ],
        Context.feedback(roots[0].id),
    ).check()
    cause = Cause(data_type="attribution", data_id=attr, old_version=1, new_version=2)
    traces = (
        TracedCause(cause=cause, root_feedback_id=roots[1].id, depth=5),
        TracedCause(
            cause=Cause(data_type="person", data_id=person, old_version=1, new_version=2),
            root_feedback_id=roots[2].id,
            depth=2,
        ),
    )
    event = (
        rt.store.client("dependency")
        .write(
            [PublishReevaluation("artifact", artifact, cause, "synthetic multiple causes")],
            Context(roots[0].id, 3, additional_causes=traces),
        )
        .check()
        .events[0]
    )
    assert isinstance(event, ReevaluationRequested)
    relay(rt)
    return event, artifact, attr


def admit(rt, event):
    assert rt.client.run_until_idle() > 0
    assert rt.client.is_processed(CONSUMER, event.event_id)
    assert not rt.client.is_processed(COMPLETION_CONSUMER, event.event_id)
    run = rt.client.get_system_record(reevaluation_run_id(event.event_id))
    assert run is not None and run.payload["status"] == "queued"
    assert rt.client.get_system_record(event.event_id) is None
    return run


def assert_finished(rt, event, artifact, attr, changed):
    client = rt.client
    finish = client.get_reevaluation_result(event.event_id)
    assert finish is not None
    assert (finish.outcome, finish.before_version, finish.after_version) == (
        "changed" if changed else "unchanged",
        1,
        1 + changed,
    )
    assert (finish.root_feedback_id, finish.depth, finish.additional_causes) == (
        event.root_feedback_id,
        event.depth,
        event.additional_causes,
    )
    completion = client.get_system_record(event.event_id)
    run = client.get_system_record(reevaluation_run_id(event.event_id))
    assert completion is not None and completion.data_type == "extraction_completion"
    assert run is not None and run.payload["status"] == "succeeded"
    assert run.id != completion.id
    assert client.is_processed(COMPLETION_CONSUMER, event.event_id)
    current = client.get(artifact)
    assert isinstance(current, SemanticRecord) and current.version == 1 + changed
    assert len(client.history(artifact)) == 1 + changed
    assert not client.stale_dependencies([attr])
    assert len(rt.provider.calls) == 1
    call = rt.provider.calls[0]
    assert (
        call.run_id,
        call.request_event_id,
        call.root_feedback_id,
        call.depth,
        call.additional_causes,
    ) == (
        run.id,
        event.event_id,
        event.root_feedback_id,
        3,
        event.additional_causes,
    )
    record = rt.writer.get_system_record(call.call_id)
    assert record is not None
    audit = LLMCallRecord.model_validate(record.payload)
    assert audit.caller_module == "extraction" and audit.status == "success"
    assert (audit.request_event_id, audit.root_feedback_id, audit.depth) == (
        event.event_id,
        event.root_feedback_id,
        3,
    )
    assert sorted(t.model_dump_json() for t in audit.additional_causes) == sorted(
        t.model_dump_json()
        for t in (
            *event.additional_causes,
            TracedCause(
                cause=event.cause, root_feedback_id=event.root_feedback_id, depth=event.depth
            ),
        )
    )
    assert audit.request_text is None and audit.response_text is None
    assert rt.fault.preflights
    contexts = [c for c in rt.fault.contexts if c.get("request_event_id") == event.event_id]
    assert contexts and all(
        c
        == {
            "request_event_id": event.event_id,
            "root_feedback_id": event.root_feedback_id,
            "depth": 3,
            "additional_causes": [t.model_dump(mode="json") for t in event.additional_causes],
        }
        for c in contexts
    )


def restart_worker_process(rt):
    script = """import json,sys
from datetime import timedelta
from ear_contracts import utcnow
from ear_storage_client.remote import RemoteStorageClient
from ear_llm_gateway import GatewayClient
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService
from ear_extraction.worker import tick
cfg=json.load(sys.stdin)
with (
    RemoteStorageClient(cfg['token'],cfg['storage']) as client,
    GatewayClient(cfg['gateway'],cfg['token'],system=SYSTEM) as gateway,
):
    worker=ExtractionService(client,gateway,clock=lambda: utcnow()+timedelta(hours=1))
    worker.subscribe()
    for _ in range(3): tick(worker)
    print(json.dumps([
        {'status':r.payload['status'],'error':r.payload.get('error')}
        for r in worker.runs()
    ]))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps({"token": rt.caller.token, "storage": rt.url, "gateway": rt.gateway_url}),
        capture_output=True,
        text=True,
        timeout=45,
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, "fresh worker process failed (credentials not logged)"
    return json.loads(result.stdout)


@pytest.mark.parametrize("changed", [False, True])
def test_live_finish_and_redelivery_with_reserved_principal(runtime, changed):
    rt = runtime
    rt.provider.changed = changed
    event, artifact, attr = seed_request(rt)
    admit(rt, event)
    before = len(rt.store.outbox())
    # A second principal of the same module is not the reserved consumer owner.
    peer = rt.operator.issue_principal("extraction", read_types=READS, subscribe_types=["artifact"])
    for name in (CONSUMER, COMPLETION_CONSUMER):
        with pytest.raises(InvalidRequest):
            rt.operator.reserve_consumer_identity(name, peer.principal_id)
        with (
            RemoteStorageClient(peer.token, rt.url) as other,
            pytest.raises(PermissionDenied),
        ):
            other.write([MarkProcessed(name, event.event_id)], Context.of(event))
    rt.worker.drain()
    assert_finished(rt, event, artifact, attr, changed)
    assert len(rt.store.outbox()) == before + changed
    assert restart_worker_process(rt) == [{"status": "succeeded", "error": None}]

    # Force an actual duplicate broker message, bypassing only the broker's
    # finite Nats-Msg-Id window. Database identity/markers remain authoritative.
    async def duplicate_on_bus():
        async with reader(rt.cfg) as js:
            before = (await js.stream_info(rt.cfg.stream)).state.messages
            await js.publish(
                rt.cfg.prefix + ".reevaluation_requested.artifact.extraction",
                event.model_dump_json().encode(),
                headers={"Nats-Msg-Id": new_id()},
            )
            assert (await js.stream_info(rt.cfg.stream)).state.messages == before + 1

    asyncio.run(duplicate_on_bus())
    rt.client.run_until_idle()
    rt.worker.handle_event(event)
    rt.worker.drain()
    assert_finished(rt, event, artifact, attr, changed)
    if changed:
        update = rt.store.outbox()[-1][1][0]
        assert (update.request_event_id, update.depth, update.additional_causes) == (
            event.event_id,
            event.depth,
            event.additional_causes,
        )
        relay(rt)
    with connection(rt.dsn) as conn:
        assert conn.execute(
            "SELECT name,principal_id FROM storage.consumer_identities ORDER BY name"
        ).fetchall() == [
            (COMPLETION_CONSUMER, rt.caller.principal_id),
            (CONSUMER, rt.caller.principal_id),
        ]
        assert conn.execute("SELECT name FROM storage.consumers ORDER BY name").fetchall() == [
            (CONSUMER,)
        ]
    assert not rt.client.deadletters(CONSUMER)


@pytest.mark.parametrize("phase", ["claim", "sending", "returned", "finish"])
@pytest.mark.parametrize("committed", [False, True])
def test_real_http_commit_unknown_and_process_restart(runtime, phase, committed):
    rt = runtime
    event, artifact, attr = seed_request(rt)
    run = admit(rt, event)
    before = len(rt.store.outbox())
    rt.fault.phase, rt.fault.committed = phase, committed
    if phase == "claim":
        with pytest.raises(CommitUnknown):
            rt.worker.drain()
    else:
        rt.worker.drain()
    assert rt.fault.fired
    count = len(rt.provider.calls)
    assert count == int(phase in {"returned", "finish"})
    recovered = restart_worker_process(rt)
    final = rt.client.get_system_record(run.id)
    assert final is not None
    success = phase == "claim" or (phase == "finish" and committed)
    assert recovered[0]["status"] == ("succeeded" if success else "blocked")
    assert len(rt.provider.calls) == (1 if phase == "claim" else count)
    assert len(rt.store.outbox()) == before + success
    if success:
        assert_finished(rt, event, artifact, attr, True)
    else:
        assert rt.client.get_reevaluation_result(event.event_id) is None
        assert not rt.client.is_processed(COMPLETION_CONSUMER, event.event_id)
        assert rt.client.stale_dependencies([attr])
        assert len(rt.client.history(artifact)) == 1
        with pytest.raises(ValueError, match="gateway_"):
            rt.worker.retry(run.id)
        rt.worker.handle_event(event)
        rt.worker.drain()
        assert len(rt.provider.calls) == count


def test_finish_late_cas_conflict_rolls_back_business_dependencies_and_marker(runtime, monkeypatch):
    rt = runtime
    event, artifact, attr = seed_request(rt)
    run = admit(rt, event)
    before = len(rt.store.outbox())
    write = rt.client.write
    conflicts = []

    def stale_final(ops, context):
        if any(isinstance(op, FinishReevaluation) for op in ops):
            assert isinstance(ops[-1], PutSystemRecord)
            ops = [*ops[:-1], replace(ops[-1], expected_revision=run.revision)]
            result = write(ops, context)
            assert result.conflict is not None
            conflicts.append(result.conflict.reason)
            return result
        return write(ops, context)

    monkeypatch.setattr(rt.client, "write", stale_final)
    rt.worker.drain()
    assert conflicts == ["revision_mismatch"]
    assert len(rt.store.outbox()) == before
    assert len(rt.client.history(artifact)) == 1
    assert rt.client.stale_dependencies([attr])
    assert rt.client.get_reevaluation_result(event.event_id) is None
    assert rt.client.get_system_record(event.event_id) is None
    assert not rt.client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert restart_worker_process(rt)[0]["status"] == "blocked"
    assert len(rt.provider.calls) == 1


@pytest.mark.parametrize("loss", ["provider", "gateway_reply"])
def test_real_unknown_provider_blocks_after_restart(runtime, loss):
    rt = runtime
    event, artifact, attr = seed_request(rt)
    run = admit(rt, event)
    rt.provider.unknown = loss == "provider"
    rt.fault.gateway_loss = loss == "gateway_reply"
    rt.worker.drain()
    assert restart_worker_process(rt)[0]["status"] == "blocked"
    with pytest.raises(ValueError, match="gateway_outcome_unknown"):
        rt.worker.retry(run.id)
    receipt = rt.worker.reconcile_run(run.id)
    assert receipt["record_saved"] and not receipt["response_recoverable"]
    assert receipt["status"] == ("success" if loss == "gateway_reply" else "failed")
    assert len(rt.provider.calls) == 1
    assert len(rt.client.history(artifact)) == 1 and rt.client.stale_dependencies([attr])
    assert rt.client.get_reevaluation_result(event.event_id) is None


def test_worker_tick_really_pumps_nats_and_finishes(runtime):
    rt = runtime
    event, artifact, attr = seed_request(rt)
    for _ in range(20):
        tick(rt.worker)
        if rt.client.get_reevaluation_result(event.event_id) is not None:
            break
    assert_finished(rt, event, artifact, attr, True)


def test_concurrent_http_intent_cas_sends_only_winner(runtime):
    rt = runtime
    event, _, _ = seed_request(rt)
    run = admit(rt, event)
    start = Barrier(2)
    sent = []

    class Port:
        def complete(self, call):
            sent.append(call.call_id)
            return {}

    def attempt():
        with RemoteStorageClient(rt.caller.token, rt.url) as client:
            tracker = DurableCalls(
                client, Port(), run.id, run.payload, run.revision, Context.of(event)
            )
            start.wait(timeout=10)
            try:
                tracker.complete(
                    Call(
                        root_feedback_id=event.root_feedback_id,
                        depth=event.depth,
                        request_event_id=event.event_id,
                        additional_causes=event.additional_causes,
                        run_id=run.id,
                        purpose="reevaluation",
                        data={},
                        output_schema={"type": "object"},
                    )
                )
                return "sent"
            except Blocked as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == ["run_fence_lost", "sent"]
    assert len(sent) == 1
    assert restart_worker_process(rt)[0]["status"] == "blocked"
    assert not rt.provider.calls


def test_legacy_completion_id_collision_survives_real_process_restart(runtime):
    rt = runtime
    event, artifact, _ = seed_request(rt)
    # Emulate a pre-upgrade admitted task before the pending NATS delivery.
    rt.client.write(
        [
            PutSystemRecord(
                "extraction_run",
                event.event_id,
                {
                    "command": Command(kind="reevaluate", target_id=artifact).model_dump(
                        mode="json"
                    ),
                    "status": "queued",
                    "attempts": 0,
                    "scheduled": False,
                    "root_feedback_id": event.root_feedback_id,
                    "depth": event.depth,
                    "created_at": "2026-11-05T10:00:00Z",
                    "event_id": event.event_id,
                    "model_calls": [],
                },
            ),
            MarkProcessed(CONSUMER, event.event_id),
        ],
        Context.of(event),
    ).check()
    assert restart_worker_process(rt) == [
        {
            "status": "blocked",
            "error": "legacy_completion_id_collision",
        }
    ]
    rt.worker.handle_event(event)
    legacy = rt.client.get_system_record(event.event_id)
    assert legacy is not None and legacy.data_type == "extraction_run"
    assert "request_event_id" not in legacy.payload and "gateway_calls" not in legacy.payload
    assert rt.client.get_system_record(reevaluation_run_id(event.event_id)) is None
    assert rt.client.get_reevaluation_result(event.event_id) is None
    assert not rt.client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert not rt.provider.calls


def test_concurrent_input_change_prevents_finish_and_does_not_recompute(runtime):
    rt = runtime
    event, artifact, attr = seed_request(rt)
    run = admit(rt, event)

    def advance_input():
        current = rt.store.client("attribution").get(attr)
        assert isinstance(current, SemanticRecord)
        rt.store.client("attribution").write(
            [Update("attribution", attr, current.version, {**current.payload, "later": True})],
            Context.system(),
        ).check()

    rt.provider.before_return = advance_input
    before = len(rt.store.outbox())
    rt.worker.drain()
    final = rt.client.get_system_record(run.id)
    assert final is not None and final.payload["error"] == "gateway_result_not_committed"
    assert len(rt.store.outbox()) == before + 1  # Only the independent input change.
    assert rt.client.get_reevaluation_result(event.event_id) is None
    assert not rt.client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert len(rt.client.history(artifact)) == 1 and rt.client.stale_dependencies([attr])
    assert restart_worker_process(rt)[0]["status"] == "blocked"
    assert len(rt.provider.calls) == 1
