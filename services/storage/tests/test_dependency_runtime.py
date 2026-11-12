"""Actual ⑤ + authenticated HTTP/PG/NATS; isolated synthetic fixture only.

Both gates required. No .env, deployed endpoints, ear-dev, model calls or real
inputs. Explicit complete grants (including synthetic completion types), real
owner Finish, real child-process restart. Not button/P5/full-MVP acceptance.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import ExitStack, suppress

import pytest
from ear_contracts import (
    DataChanged,
    DataTypeSpec,
    ReevaluationRequested,
    ReevaluationResult,
    SemanticRecord,
    VersionRef,
    new_id,
    utcnow,
)
from ear_dependency import CommitJournal, RemoteDependencyService
from ear_dependency.authority import SchedulerBlocked
from ear_dependency.domain import Step, Trigger
from ear_storage import bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.core_deployment import core_mvp_registry
from ear_storage.http import create_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    FinishReevaluation,
    MarkProcessed,
    PermissionDenied,
    PutSystemRecord,
    RegisterDependencies,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.wire import decode_request
from nats.js.errors import NotFoundError
from starlette.responses import Response

from .test_api_gateway_integration import Harness, loopback
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
KINDS = {"a": "alpha_first", "b": "beta_first", "join": "alpha_join", "tail": "beta_tail"}
INPUTS = {"a": ["source"], "b": ["source"], "join": ["a", "b", "source"], "tail": ["join"]}


def registration():
    # Use the production strict profile; synthetic plugins extend it, never
    # replace its trusted provenance or bypass scheduler coverage checks.
    return core_mvp_registry().extend(
        *(
            DataTypeSpec(
                t,
                "semantic",
                t.split("_")[0],
                supports_reevaluation=True,
                requires_dependencies=True,
            )
            for t in KINDS.values()
        ),
        *(
            DataTypeSpec(m + "_completion", "system_record", m, reevaluation_result=True)
            for m in ("alpha", "beta")
        ),
    )


@pytest.fixture
def dependency_runtime(pg_dsn, nats_server, tmp_path):
    registry = registration()
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry)
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
    events = {
        n
        for n, s in registry.data_types.items()
        if s.emits_events and n != "plugin_management_feedback"
    }
    reads = (
        events
        | {n for n, s in registry.data_types.items() if s.reevaluation_result}
        | {
            "propagation_record",
            "attribution_feedback_result",
        }
    )
    operator = AuthOperator(pg_dsn)
    principals = {}
    for module in ("dependency", "alpha", "beta", "attribution"):
        subs = (
            events
            if module == "dependency"
            else {t for t in KINDS.values() if t.startswith(module + "_")}
        )
        principals[module] = operator.issue_principal(
            module, read_types=reads, subscribe_types=subs
        )
        operator.reserve_consumer_identity(
            "dependency.changes" if module == "dependency" else module + ".fixture",
            principals[module].principal_id,
        )
    app = create_app(StorageService(pg_dsn, relay_config=cfg))
    fault = Harness(phase=None, committed=False, bodies=[], fired=False)

    @app.middleware("http")
    async def lose_response(request, call_next):
        lose = False
        if request.url.path == "/v1/batches":
            body = await request.json()
            ops = body["ops"]
            phase = None
            if any(op["op"] == "PublishReevaluation" for op in ops):
                phase = "publish"
            elif any(
                op["op"] == "MarkProcessed" and op["body"]["consumer"] == "dependency.changes"
                for op in ops
            ):
                phase = "admit"
            else:
                for op in ops:
                    if op["op"] == "PutSystemRecord":
                        payload = op["body"]["payload"]
                        if payload.get("state") == "intent":
                            phase = "intent"
                        elif payload.get("record_kind") == "step" and payload.get("result"):
                            phase = "finish"
                        elif (
                            payload.get("record_kind") == "trigger"
                            and payload.get("status") == "expanded"
                        ):
                            phase = "expand"
            if fault.phase == phase and phase is not None:
                fault.bodies.append(body)
                if not fault.fired:
                    lose, fault.fired = True, True
                    if not fault.committed:
                        return Response("synthetic unavailable before commit", status_code=503)
        response = await call_next(request)
        if lose:
            assert response.status_code == 200
            return Response("synthetic lost receipt after commit", status_code=200)
        return response

    try:
        with ExitStack() as stack:
            url = stack.enter_context(loopback(app))
            clients = {
                m: stack.enter_context(RemoteStorageClient(p.token, url))
                for m, p in principals.items()
            }
            rt = Harness(
                clients=clients,
                operator=operator,
                principals=principals,
                cfg=cfg,
                dsn=pg_dsn,
                url=url,
                reads=reads,
                subscriptions=events,
                fault=fault,
                directory=tmp_path / "dependency-journal",
                ids={},
                requests=[],
            )
            for owner in ("alpha", "beta"):
                clients[owner].subscribe(
                    owner + ".fixture", rt.requests.append, event_types=["reevaluation_requested"]
                )
            source = new_id()
            clients["attribution"].write(
                [Create("person", source, {"name": "synthetic"})], Context.system()
            ).check()
            rt.ids["source"] = source
            for name, kind in KINDS.items():
                target = new_id()
                refs = [
                    VersionRef(
                        data_type="person" if n == "source" else KINDS[n], id=rt.ids[n], version=1
                    )
                    for n in INPUTS[name]
                ]
                clients[kind.split("_")[0]].write(
                    [Create(kind, target, {"value": 0}), RegisterDependencies(kind, target, refs)],
                    Context.system(),
                ).check()
                rt.ids[name] = target
            yield rt
    finally:

        async def cleanup():
            async with reader(cfg) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(cfg.stream)

        asyncio.run(cleanup())


def worker(rt):
    journal = CommitJournal(rt.directory, namespace="synthetic-dependency-runtime")
    service = RemoteDependencyService(rt.clients["dependency"], journal=journal)
    return service, journal


def relay(rt):
    result = asyncio.run(run_relay(RelayStore(rt.dsn, rt.cfg), once=True))
    for name in ("alpha", "beta"):
        rt.clients[name].run_until_idle()
    return result


def change(rt):
    event = (
        rt.clients["attribution"]
        .write(
            [Update("person", rt.ids["source"], 1, {"name": "changed synthetic"})], Context.system()
        )
        .check()
        .events[0]
    )
    assert isinstance(event, DataChanged)
    return event


def finish(rt, name, outcome):
    request = next(e for e in rt.requests if e.target_id == rt.ids[name])
    assert isinstance(request, ReevaluationRequested)
    client = rt.clients[KINDS[name].split("_")[0]]
    current = client.get(request.target_id)
    assert isinstance(current, SemanticRecord)
    after = current.version + (outcome == "changed")
    ops = []
    if outcome == "changed":
        ops.append(Update(current.data_type, current.id, current.version, {"value": 1}))
    if outcome != "failed":
        inputs = [client.get(rt.ids[n]) for n in INPUTS[name]]
        assert all(inputs)
        ops.append(
            RegisterDependencies(
                current.data_type,
                current.id,
                [
                    VersionRef(data_type=i.data_type, id=i.id, version=i.version)
                    for i in inputs
                    if i is not None
                ],
            )
        )
    ops.extend(
        [
            FinishReevaluation(
                ReevaluationResult(
                    request_event_id=request.event_id,
                    target_type=current.data_type,
                    target_id=current.id,
                    outcome=outcome,
                    before_version=current.version,
                    after_version=after,
                    root_feedback_id=request.root_feedback_id,
                    depth=request.depth,
                    additional_causes=request.additional_causes,
                    completed_at=utcnow(),
                    error_code="synthetic_failure" if outcome == "failed" else None,
                )
            ),
            MarkProcessed(KINDS[name].split("_")[0] + ".fixture", request.event_id),
        ]
    )
    client.write(ops, Context.of(request)).check()


def ticks(service, count=3):
    for _ in range(count):
        service.tick()


# Credentials travel only in child stdin, never argv/files/environment/log output.
RESTART = """
import json, sys
from pathlib import Path
from ear_dependency import CommitJournal, RemoteDependencyService
from ear_storage_client.remote import RemoteStorageClient
p = json.load(sys.stdin)
with RemoteStorageClient(p['token'], p['url']) as client:
    journal = CommitJournal(Path(p['directory']), namespace='synthetic-dependency-runtime')
    try:
        service = RemoteDependencyService(client, journal=journal)
        service.subscribe()
        for _ in range(3):
            service.tick()
        assert not journal.pending_ids()
        print('fresh-worker-recovered')
    finally:
        journal.close()
"""


def new_process(rt):
    result = subprocess.run(
        [sys.executable, "-c", RESTART],
        input=json.dumps(
            {
                "token": rt.principals["dependency"].token,
                "url": rt.url,
                "directory": str(rt.directory),
            }
        ),
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fresh-worker-recovered"


def test_real_broker_diamond_changed_unchanged_new_worker(dependency_runtime):
    rt = dependency_runtime
    service, journal = worker(rt)
    try:
        proof = rt.clients["dependency"].scheduler_state()
        assert proof["coverage_complete"] and proof["owners"]["alpha"]["can_receive"]
        service.subscribe()
        change(rt)
        assert relay(rt).delivered > 0
        rt.clients["dependency"].run_until_idle()
        ticks(service)
        relay(rt)
        assert {e.target_id for e in rt.requests} == {rt.ids["a"], rt.ids["b"]}
        finish(rt, "a", "changed")
        ticks(service)
        relay(rt)
        assert len(rt.requests) == 2  # join still waits for b
        finish(rt, "b", "unchanged")
    finally:
        journal.close()
    new_process(rt)
    relay(rt)
    service, journal = worker(rt)
    try:
        assert {e.target_id for e in rt.requests} == {rt.ids[n] for n in ("a", "b", "join")}
        finish(rt, "join", "changed")
        ticks(service)
        relay(rt)
        assert len(rt.requests) == 4
        finish(rt, "tail", "unchanged")
        ticks(service)
        steps = [r for r in service.records() if isinstance(r, Step)]
        assert len(steps) == 4 and all(s.state == "done" for s in steps)
        assert {s.result.outcome for s in steps if s.result is not None} == {"changed", "unchanged"}
        assert all(t.status == "closed" for t in service.records() if isinstance(t, Trigger))
        ticks(service)
        assert not journal.pending_ids()
        assert len(rt.requests) == 4
    finally:
        journal.close()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("phase", ["admit", "expand", "intent", "publish", "finish"])
def test_real_identical_receipt_replay_with_new_process(dependency_runtime, phase, committed):
    rt = dependency_runtime
    service, journal = worker(rt)
    event = change(rt)
    try:
        if phase != "admit":
            service.handle(event)
        if phase == "finish":
            ticks(service)
            relay(rt)
            finish(rt, "a", "unchanged")
        rt.fault.phase, rt.fault.committed = phase, committed
        with pytest.raises(CommitUnknown):
            service.handle(event) if phase == "admit" else service.tick()
        assert journal.pending("operation") is not None
    finally:
        journal.close()
    new_process(rt)
    assert len(rt.fault.bodies) >= 2
    assert rt.fault.bodies[0] == rt.fault.bodies[1]  # includes request_id and original context
    service, journal = worker(rt)
    try:
        relay(rt)
        assert len(rt.requests) == 2
        service.handle(event)
        ticks(service)
        assert len([r for r in service.records() if isinstance(r, Trigger)]) == 1
        assert not journal.pending_ids()
    finally:
        journal.close()


@pytest.mark.parametrize("mode", ["coverage", "owner", "failed"])
def test_real_blocking_never_claims_empty_graph_success(dependency_runtime, mode):
    rt = dependency_runtime
    service, journal = worker(rt)
    try:
        service.handle(change(rt))
        if mode == "coverage":
            before = rt.clients["dependency"].get_system_record(service.status_id)
            assert before is not None
            rt.operator.set_grants(
                rt.principals["dependency"].principal_id,
                read_types=rt.reads - {"beta_tail"},
                subscribe_types=rt.subscriptions - {"beta_tail"},
            )
            # Strict queue protection also denies remote diagnostic writes.
            # Keep the original coverage failure durably LOCAL, not via a
            # payload-based exception that a low-privilege peer could forge.
            with pytest.raises(SchedulerBlocked, match=r"^write_denied$"):
                service.tick()
            pending = journal.pending("runtime")
            assert pending is not None
            ops, context = decode_request(pending[1])
            assert len(ops) == 1 and isinstance(ops[0], PutSystemRecord)
            assert ops[0].data_type == "propagation_record"
            assert ops[0].id == service.status_id
            assert ops[0].payload["status"] == "blocked"
            assert ops[0].payload["reason"] == "coverage_denied"
            assert context == Context.system()
            assert rt.clients["dependency"].get_system_record(service.status_id) == before
            with pytest.raises(SchedulerBlocked, match=r"^write_denied$"):
                service.tick()
            assert journal.pending("runtime") == pending
            assert not rt.requests
            with connection(rt.dsn) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM storage.outbox "
                    "WHERE event_text::jsonb->>'event_type'='reevaluation_requested'"
                ).fetchone() == (0,)
            # Coverage revocation must also invalidate snapshot reads; do not
            # bypass that boundary just to inspect the test's propagation graph.
            with pytest.raises(PermissionDenied):
                service.records()
        else:
            if mode == "owner":
                rt.operator.revoke(rt.principals["alpha"].principal_id)
            ticks(service)
            relay(rt) if mode == "failed" else None
            if mode == "failed":
                finish(rt, "a", "failed")
                ticks(service)
            steps = {s.target_id: s for s in service.records() if isinstance(s, Step)}
            assert steps[rt.ids["a"]].reason == (
                "owner_unauthorized" if mode == "owner" else "failed"
            )
            assert steps[rt.ids["join"]].reason == "upstream_blocked"
            assert not any(
                t.status == "closed" for t in service.records() if isinstance(t, Trigger)
            )
    finally:
        journal.close()


@pytest.mark.parametrize("committed", [False, True])
def test_real_abrupt_process_exit_keeps_unknown_publication(dependency_runtime, committed):
    rt = dependency_runtime
    service, journal = worker(rt)
    try:
        service.handle(change(rt))
    finally:
        journal.close()
    rt.fault.phase, rt.fault.committed = "publish", committed
    crash = """
import json, os, sys
from pathlib import Path
from ear_dependency import CommitJournal, RemoteDependencyService
from ear_storage_client import CommitUnknown
from ear_storage_client.remote import RemoteStorageClient
p = json.load(sys.stdin)
client = RemoteStorageClient(p['token'], p['url'])
journal = CommitJournal(Path(p['directory']), namespace='synthetic-dependency-runtime')
service = RemoteDependencyService(client, journal=journal)
try:
    service.tick()
except CommitUnknown:
    os._exit(23)  # intentional abrupt exit, no client/journal finally/atexit
os._exit(24)
"""
    outcome = subprocess.run(
        [sys.executable, "-c", crash],
        input=json.dumps(
            {
                "token": rt.principals["dependency"].token,
                "url": rt.url,
                "directory": str(rt.directory),
            }
        ),
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert outcome.returncode == 23, outcome.stderr
    new_process(rt)
    assert rt.fault.bodies[0] == rt.fault.bodies[1]
    relay(rt)
    assert len(rt.requests) == 2
    assert len({e.event_id for e in rt.requests}) == 2


def test_real_records_reject_revision_change_then_read_fresh(dependency_runtime, monkeypatch):
    rt = dependency_runtime
    service, journal = worker(rt)
    try:
        assert service.records() == []
        service.handle(change(rt))
        before = service.records()
        assert len(before) == 1 and isinstance(before[0], Trigger)
        client = rt.clients["dependency"]
        query = client.query
        calls = []

        def changed(args):
            page = query(args)
            calls.append(args)
            # A complete but different grant set is not the same read authority.
            rt.operator.set_grants(
                rt.principals["dependency"].principal_id,
                read_types=rt.reads | {"llm_call"},
                subscribe_types=rt.subscriptions,
            )
            return page

        def no_snapshot():
            pytest.fail("records must remain bounded direct reads, not a business snapshot")

        monkeypatch.setattr(client, "snapshot", no_snapshot)
        monkeypatch.setattr(client, "query", changed)
        with pytest.raises(SchedulerBlocked, match="scheduler_state_changed"):
            service.records()
        assert len(calls) == 1
        monkeypatch.setattr(client, "query", query)
        assert service.records() == before  # a new call takes new authority, no stale round
        record = before[0].model_copy(update={"reason": "synthetic_updated_record"})
        from ear_dependency.records import put

        envelope = client.get_system_record(record.id)
        assert envelope is not None
        client.write([put(record, envelope.revision)], Context.system()).check()
        assert service.records() == [record]  # no cache of records either

        def revoked(args):
            page = query(args)
            rt.operator.set_grants(
                rt.principals["dependency"].principal_id,
                read_types=rt.reads - {"beta_tail"},
                subscribe_types=rt.subscriptions - {"beta_tail"},
            )
            return page

        monkeypatch.setattr(client, "query", revoked)
        with pytest.raises(PermissionDenied):
            service.records()  # a fetched page must not escape after coverage revocation
        monkeypatch.setattr(client, "query", query)
        rt.operator.set_grants(
            rt.principals["dependency"].principal_id,
            read_types=rt.reads,
            subscribe_types=rt.subscriptions,
        )
        assert service.records() == [record]
        assert not journal.pending_ids() and not rt.requests
    finally:
        journal.close()


def test_real_records_refuse_legacy_even_with_module_and_read_grant(pg_dsn, tmp_path):
    from ear_contracts import DEFAULT_REGISTRY

    migrate(pg_dsn)
    bootstrap(pg_dsn, DEFAULT_REGISTRY)
    principal = AuthOperator(pg_dsn).issue_principal(
        "dependency", read_types={"propagation_record"}
    )
    app = create_app(StorageService(pg_dsn))
    with loopback(app) as url, RemoteStorageClient(principal.token, url) as client:
        journal = CommitJournal(tmp_path / "legacy-journal", namespace="synthetic-legacy")
        try:
            service = RemoteDependencyService(client, journal=journal)
            assert client.module == "dependency"
            # Ordinary legacy reads remain allowed, but cannot prove strict coverage.
            from ear_storage_client import Query

            assert client.query(Query("propagation_record")).items == ()
            with pytest.raises(PermissionDenied):
                service.records()
            assert not journal.pending_ids()
        finally:
            journal.close()
