"""Isolated authenticated HTTP/PostgreSQL/NATS attribution; synthetic inputs only.

Explicit fixture registry, no migration of existing deployments. Real broker for
new input/Finish; fault tests discard batch responses on the real HTTP boundary.
Worker and private journal reopen in-process, not an OS crash or full deployment.
"""

import asyncio
from contextlib import ExitStack, suppress
from dataclasses import replace

import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, Ingested, Runner
from ear_adapter.testing import FakeLocator, FakeSource, evidence_json
from ear_attribution import AttributionService, CommitJournal, MatchConfig
from ear_attribution.feedback import feedback_result_id
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataTypeSpec,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    new_id,
)
from ear_storage import bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.http import create_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.service import StorageService
from ear_storage_client import AppendRaw, Context, PermissionDenied, PublishReevaluation, Query
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.testing import make_raw
from nats.js.errors import NotFoundError
from starlette.responses import Response

from .test_api_gateway_integration import Harness, loopback
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
CONFIG = MatchConfig(cosine_threshold=0.9, gps_threshold_m=100)
RAW_TYPES = {
    "audio_ref",
    "transcript_segment",
    "local_speaker",
    "voiceprint",
    "ambient_sound",
    "gps_point",
    "phone_info",
}
ATTR_TYPES = RAW_TYPES | {
    "person",
    "place",
    "attribution",
    "feedback",
    "attribution_state",
    "attribution_feedback_result",
    "attribution_completion",
}


@pytest.fixture
def attribution_runtime(pg_dsn, nats_server, tmp_path):
    types = dict(DEFAULT_REGISTRY.data_types)
    types["attribution"] = replace(
        types["attribution"],
        supports_reevaluation=True,
        requires_dependencies=True,
    )
    registry = Registry(
        types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
    ).extend_core(
        DataTypeSpec("attribution_state", "system_record", "attribution"),
        DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
    )
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
    operator = AuthOperator(pg_dsn)
    principal = operator.issue_principal(
        "attribution",
        read_types=ATTR_TYPES,
        subscribe_types=["voiceprint", "gps_point", "feedback", "attribution"],
    )
    operator.reserve_consumer_identity("attribution", principal.principal_id)
    adapter = operator.issue_principal("adapter", read_types=RAW_TYPES)
    api = operator.issue_principal("api", read_types=ATTR_TYPES)
    dependency = operator.issue_principal("dependency", read_types=ATTR_TYPES)
    peer = operator.issue_principal("attribution", read_types=ATTR_TYPES)
    app = create_app(StorageService(pg_dsn, relay_config=cfg))
    fault = Harness(armed=False, committed=False, bodies=[])

    @app.middleware("http")
    async def lose_commit(request, call_next):
        body = await request.json() if request.url.path == "/v1/batches" else None
        match = body is not None and any(
            op["op"] == "MarkProcessed" and op["body"]["consumer"] == "attribution"
            for op in body["ops"]
        )
        if match:
            fault.bodies.append(body)
        lose = match and fault.armed
        if lose:
            fault.armed = False
            if not fault.committed:
                return Response("synthetic unavailable", status_code=503)
        response = await call_next(request)
        if lose:
            assert response.status_code == 200
            return Response("synthetic dropped receipt", status_code=200)
        return response

    try:
        with ExitStack() as stack:
            url = stack.enter_context(loopback(app))
            clients = {
                name: stack.enter_context(RemoteStorageClient(p.token, url))
                for name, p in (
                    ("owner", principal),
                    ("adapter", adapter),
                    ("api", api),
                    ("dependency", dependency),
                    ("peer", peer),
                )
            }
            yield Harness(
                clients=clients,
                operator=operator,
                peer=peer,
                dsn=pg_dsn,
                cfg=cfg,
                fault=fault,
                directory=tmp_path / "private-commits",
                caller=principal,
                url=url,
            )
    finally:

        async def cleanup():
            async with reader(cfg) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(cfg.stream)

        asyncio.run(cleanup())


def relay(rt):
    return asyncio.run(run_relay(RelayStore(rt.dsn, rt.cfg), once=True))


def worker(rt):
    journal = CommitJournal(rt.directory, namespace="synthetic-attribution-runtime")
    return AttributionService.business(rt.clients["owner"], config=CONFIG, journal=journal), journal


def test_import_feedback_and_finish_through_broker(attribution_runtime):
    rt = attribution_runtime
    service, journal = worker(rt)
    try:
        service.subscribe()
        source = FakeSource({"cap-1": evidence_json()})

        def importer():
            return Runner(
                source=source,
                storage=rt.clients["adapter"],
                locator=FakeLocator(),
                config=AdapterConfig(),
            )

        assert isinstance(importer().run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
        assert relay(rt).delivered > 0
        rt.clients["owner"].run_until_idle()
        attrs = rt.clients["owner"].query(Query("attribution")).items
        assert len(attrs) == 3
        assert rt.clients["owner"].count(Query("person")) == 1
        assert rt.clients["owner"].count(Query("place")) == 1
        assert isinstance(importer().run_once(capture_ids=["cap-1"]).outcomes[0], AlreadyIngested)
        current = next(a for a in attrs if a.payload["target_type"] == "person")
        feedback = make_raw(
            "feedback",
            {
                "module": "attribution",
                "command": {
                    "kind": "confirm_attribution",
                    "subject_id": current.payload["subject_id"],
                    "target_id": current.payload["target_id"],
                },
            },
            start="2026-11-05T09:00:00Z",
        )
        rt.clients["api"].write([AppendRaw(feedback)], Context.feedback(feedback.id)).check()
        relay(rt)
        rt.clients["owner"].run_until_idle()
        receipt = rt.clients["api"].get_system_record(feedback_result_id(feedback.id))
        assert receipt is not None and receipt.payload["status"] == "succeeded"
        protected = rt.clients["owner"].get(current.id)
        assert isinstance(protected, SemanticRecord) and protected.payload["user_protected"]
        target = rt.clients["owner"].get(protected.payload["target_id"])
        assert target is not None
        event = (
            rt.clients["dependency"]
            .write(
                [
                    PublishReevaluation(
                        "attribution",
                        protected.id,
                        Cause(
                            data_type="person",
                            data_id=target.id,
                            old_version=None,
                            new_version=target.version,
                        ),
                        "synthetic protected recheck",
                    ),
                ],
                Context.feedback(feedback.id).deeper(),
            )
            .check()
            .events[0]
        )
        assert isinstance(event, ReevaluationRequested)
        relay(rt)
        rt.clients["owner"].run_until_idle()
        result = rt.clients["dependency"].get_reevaluation_result(event.event_id)
        assert result is not None and result.outcome == "unchanged"
        assert result.root_feedback_id == feedback.id and result.depth == event.depth
        assert rt.clients["owner"].get(protected.id) == protected
        assert rt.clients["owner"].is_processed("attribution", event.event_id)
        deps = rt.clients["owner"].dependencies_of(protected.id)
        assert deps
        stale = rt.clients["owner"].stale_dependencies([d.depends_on.id for d in deps])
        assert not any(d.dependent.id == protected.id for d in stale)
        service.handle(event)
        assert rt.clients["dependency"].get_reevaluation_result(event.event_id) == result
        assert not rt.clients["owner"].deadletters("attribution")
        with pytest.raises(PermissionDenied):
            rt.clients["peer"].subscribe("attribution", lambda _: None)
    finally:
        journal.close()


@pytest.mark.parametrize("committed", [False, True])
def test_identical_commit_replay_after_journal_reopen(attribution_runtime, committed):
    rt = attribution_runtime
    voice = make_raw(
        "voiceprint",
        {
            "embedding": {"values": [1.0, 0.0], "shape": [2], "dtype": "float32"},
            "model_version": "synthetic/v1",
        },
        start="2026-11-05T09:00:00Z",
    )
    event = rt.clients["adapter"].write([AppendRaw(voice)], Context.system()).check().events[0]
    service, journal = worker(rt)
    rt.fault.committed, rt.fault.armed = committed, True
    try:
        with pytest.raises(CommitUnknown):
            service.handle(event)
        assert journal.pending(event.event_id) is not None
    finally:
        journal.close()
    service, journal = worker(rt)
    try:
        service.handle(event)
        assert journal.pending(event.event_id) is None
        assert len(rt.fault.bodies) == 2
        assert rt.fault.bodies[0] == rt.fault.bodies[1]  # request_id, ops and context identical
        assert rt.clients["owner"].count(Query("attribution")) == 1
        assert rt.clients["owner"].count(Query("person")) == 1
        assert rt.clients["owner"].count(Query("place")) == 1
        service.handle(event)
        assert len(rt.fault.bodies) == 2
    finally:
        journal.close()


@pytest.mark.parametrize("committed", [False, True])
def test_subscription_restart_resolves_pending_without_handler_redelivery(
    attribution_runtime, committed
):
    rt = attribution_runtime
    service, journal = worker(rt)
    service.subscribe()
    voice = make_raw(
        "voiceprint",
        {
            "embedding": {"values": [1.0, 0.0], "shape": [2], "dtype": "float32"},
            "model_version": "synthetic/v1",
        },
        start="2026-11-05T09:00:00Z",
    )
    event = rt.clients["adapter"].write([AppendRaw(voice)], Context.system()).check().events[0]
    relay(rt)
    rt.fault.committed, rt.fault.armed = committed, True
    try:
        # The SDK NACKs handler exceptions; committed markers suppress later callbacks.
        assert rt.clients["owner"].poll_once("attribution") == 1
        assert journal.pending(event.event_id) is not None
    finally:
        journal.close()
    rt.clients["owner"] = RemoteStorageClient(rt.caller.token, rt.url)
    service, journal = worker(rt)
    try:
        service.subscribe()
        # Startup must replay pending receipts independently of broker delivery.
        assert journal.pending(event.event_id) is None
        assert len(rt.fault.bodies) == 2
        assert rt.fault.bodies[0] == rt.fault.bodies[1]
        assert rt.clients["owner"].is_processed("attribution", event.event_id)
        assert rt.clients["owner"].count(Query("attribution")) == 1
        assert rt.clients["owner"].count(Query("person")) == 1
        rt.clients["owner"].run_until_idle()
        assert len(rt.fault.bodies) == 2
    finally:
        journal.close()
        rt.clients["owner"].close()
