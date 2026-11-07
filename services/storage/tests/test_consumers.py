"""P2b real PG + NATS + HTTP/SDK, synthetic data and isolated test containers only."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, new_id
from ear_storage.auth import AuthOperator
from ear_storage.consumer_broker import ConsumerUnavailable
from ear_storage.consumers import ConsumerPolicy
from ear_storage.http import create_app
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import bootstrap, connection
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ServiceUnavailable
from fastapi.testclient import TestClient

from .test_http import TestTransport
from .test_http_postgres import live_server
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()


@pytest.fixture
def setup(env):
    dsn, store, cfg = env
    policy = ConsumerPolicy(max_attempts=2, retry_base=0.01, retry_max=0.02)
    service = StorageService(dsn, relay_config=cfg, consumer_policy=policy)
    operator = AuthOperator(dsn)
    issued = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    return dsn, store, cfg, policy, service, operator, issued


def publish(setup, count=1):
    dsn, store, cfg, *_ = setup
    events = (
        store.client("extraction")
        .write([Create("activity", new_id(), {"private": "synthetic"}) for _ in range(count)], SYS)
        .check()
        .events
    )
    assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == count
    return events


def command(http, issued, operation, consumer="extraction.worker", **kwargs):
    return http.post(
        "/v1/consumers/" + operation,
        json={"codec": 1, "consumer": consumer, **kwargs},
        headers={"Authorization": "Bearer " + issued.token},
    )


def expire(dsn):
    with connection(dsn) as conn:
        conn.execute(
            "UPDATE storage.consumer_deliveries SET lease_until='-infinity', "
            "available_at='-infinity'"
        )


def test_two_consumers_independent_live_sdk_and_creation_cursor(setup):
    dsn, _, _, _, service, _, issued = setup
    publish(setup)  # Must not be delivered, even with broker start-at-all.
    a, b = [], []
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as sdk:
        sdk.subscribe("extraction.a", a.append)
        sdk.subscribe("extraction.b", b.append)
        events = publish(setup, 2)
        assert a == b == []  # subscribe never invokes handlers.
        assert sdk.run_until_idle(max_deliveries=1) == 1
        assert sdk.run_until_idle() == 3
        assert [e.event_id for e in a] == [e.event_id for e in events]
        assert a == b
        assert sdk.run_until_idle() == 0
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(DISTINCT generation) FROM storage.consumers"
        ).fetchone() == (2,)


def test_pending_creation_retry_stable_generation(setup, monkeypatch):
    dsn, _, _, _, service, _, issued = setup
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call
    with TestClient(create_app(service)) as http:
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise ConsumerUnavailable("private credentials")

            patch.setattr(consumers.broker, "call", fail)
            response = command(http, issued, "register")
            assert response.json()["status"] == "pending"
            with connection(dsn) as conn:
                generation = conn.execute("SELECT generation FROM storage.consumers").fetchone()
            assert command(http, issued, "fetch").json()["status"] == "pending"
        assert consumers.broker.call == original
        assert command(http, issued, "register").json()["status"] == "ready"
        with connection(dsn) as conn:
            assert conn.execute("SELECT generation FROM storage.consumers").fetchone() == generation
        assert "private" not in response.text


def test_ownership_namespace_and_no_broker_configuration_leaks(setup):
    _, _, _, _, service, operator, issued = setup
    other = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    with TestClient(create_app(service)) as http:
        assert command(http, issued, "register").json()["status"] == "ready"
        for op in ["register", "fetch", "deadletters"]:
            assert command(http, other, op).status_code == 403
        assert command(http, other, "register", "extraction.other").status_code == 200
        assert command(http, issued, "register", "api.fake").status_code == 403
        for extra in [
            {"scope": "public"},
            {"module": "api"},
            {"filter_subjects": [">"]},
            {"codec": True},
        ]:
            assert command(http, issued, "register", **extra).status_code == 422
        result = service.client(other.token)
        with pytest.raises(PermissionDenied):
            result.write([MarkProcessed("extraction.worker", new_id())], SYS)


def test_revocation_queued_inflight_ack_and_deadletter_redaction(setup):
    dsn, _, _, _, service, operator, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        operator.set_grants(issued.principal_id, read_types=[])
        response = command(http, issued, "fetch")
        assert response.status_code == 403 and "event_id" not in response.text
        operator.set_grants(
            issued.principal_id, read_types=["activity"], subscribe_types=["activity"]
        )
        # Broker ingress was not acknowledged; let its AckWait elapse without sleeping.
        # Restarting an ingress fetch before DB persistence is covered separately.
        with connection(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM storage.consumer_deliveries").fetchone() == (
                0,
            )
    # Another consumer demonstrates in-flight checks without waiting broker AckWait.
    with TestClient(create_app(service)) as http:
        command(http, issued, "register", "extraction.inflight")
        publish(setup)
        delivery = command(http, issued, "fetch", "extraction.inflight").json()["delivery"]
        assert delivery
        operator.set_grants(issued.principal_id, read_types=[])
        for operation in ["ack", "nack"]:
            assert (
                command(
                    http,
                    issued,
                    operation,
                    "extraction.inflight",
                    delivery_token=delivery["delivery_token"],
                ).status_code
                == 403
            )
        assert command(http, issued, "fetch", "extraction.inflight").status_code == 403
        assert (
            command(http, issued, "deadletters", "extraction.inflight").json()["deadletters"] == []
        )


def test_retry_deadletter_replay_current_state_and_restart(setup):
    dsn, store, cfg, policy, service, operator, issued = setup
    calls = []

    def broken(event):
        calls.append(event.event_id)
        raise ValueError("SECRET payload cause")

    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):
        sdk.subscribe("extraction.worker", broken)
        event = publish(setup)[0]
        assert sdk.poll_once() == 1
        expire(dsn)
        assert sdk.poll_once() == 1
        letters = sdk.deadletters("extraction.worker")
        assert len(letters) == 1 and letters[0]["attempts"] == 2
        assert "SECRET" not in json.dumps(letters) and "change_description" not in json.dumps(
            letters
        )
        operator.set_grants(issued.principal_id, read_types=[])
        assert sdk.deadletters("extraction.worker") == []
        with pytest.raises(PermissionDenied):
            sdk.replay_deadletter("extraction.worker", letters[0]["delivery_id"])
        operator.set_grants(
            issued.principal_id, read_types=["activity"], subscribe_types=["activity"]
        )
        assert sdk.replay_deadletter("extraction.worker", letters[0]["delivery_id"])
    # New service and SDK share neither connections nor handler state.
    restarted = StorageService(dsn, relay_config=cfg, consumer_policy=policy)
    store.client("extraction").write(
        [Update("activity", event.data_id, 1, {"current": True})], SYS
    ).check()
    seen = []
    with (
        TestClient(create_app(restarted)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):

        def handler(e):
            current = sdk.get(e.data_id)
            assert current is not None and current.version == 2
            sdk.write([MarkProcessed("extraction.worker", e.event_id)], SYS).check()
            seen.append(e.event_id)

        sdk.subscribe("extraction.worker", handler)
        assert sdk.run_until_idle() == 1
        assert seen == [event.event_id] and calls == [event.event_id] * 2
        assert len(sdk.deadletters("extraction.worker")) == 1  # historical DLQ retained


def test_lost_fetch_response_and_duplicate_processed(setup):
    dsn, _, cfg, policy, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        event = publish(setup)[0]
        lost = command(http, issued, "fetch").json()["delivery"]
        assert lost
        assert command(http, issued, "fetch").json()["status"] == "busy"
        expire(dsn)
    restarted = StorageService(dsn, relay_config=cfg, consumer_policy=policy)
    with TestClient(create_app(restarted)) as http:
        delivery = command(http, issued, "fetch").json()["delivery"]
        assert delivery["event"]["event_id"] == event.event_id
        assert delivery["delivery_token"] != lost["delivery_token"] and delivery["attempt"] == 2
        assert (
            command(http, issued, "ack", delivery_token=lost["delivery_token"]).status_code == 409
        )
        service.client(issued.token).write(
            [MarkProcessed("extraction.worker", event.event_id)], SYS
        ).check()
        # Lose ack HTTP response: repeating the same token is idempotent.
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
        assert command(http, issued, "fetch").json()["status"] == "idle"


def test_concurrent_poll_one_active_and_no_writer_gate_during_broker(setup, monkeypatch):
    _, _, _, _, service, _, issued = setup
    consumers = service.consumers
    assert consumers is not None
    entered, release = threading.Event(), threading.Event()
    original = consumers.broker.call

    def block(operation, **kwargs):
        if operation == "fetch":
            entered.set()
            assert release.wait(4)
        return original(operation, **kwargs)

    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup, 2)
        with monkeypatch.context() as patch, ThreadPoolExecutor(2) as pool:
            patch.setattr(consumers.broker, "call", block)
            first = pool.submit(command, http, issued, "fetch")
            try:
                assert entered.wait(2)
                second = command(http, issued, "fetch")
                assert second.json()["status"] == "busy"
                # A different writer commits while the broker operation is blocked.
                service.client(issued.token).write([Create("activity", new_id(), {})], SYS).check()
            finally:
                release.set()
            assert first.result().json()["delivery"] is not None
        assert command(http, issued, "fetch").json()["status"] == "busy"


def test_ack_broker_failure_persisted_then_retry(setup, monkeypatch):
    dsn, _, _, _, service, _, issued = setup
    consumers = service.consumers
    assert consumers is not None
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        delivery = command(http, issued, "fetch").json()["delivery"]
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise ConsumerUnavailable("secret")

            patch.setattr(consumers.broker, "call", fail)
            assert (
                command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
                == 503
            )
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT state,ack_pending FROM storage.consumer_deliveries"
            ).fetchone() == ("acked", True)
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
        assert command(http, issued, "fetch").json()["status"] == "idle"


def test_history_default_denied_and_explicit_core_allow(setup):
    dsn, _, cfg, policy, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        assert command(http, issued, "register", from_beginning=True).status_code == 403
    event = publish(setup)[0]
    service = StorageService(
        dsn,
        relay_config=cfg,
        consumer_policy=replace(policy, history_modules=frozenset(["extraction"])),
    )
    with TestClient(create_app(service)) as http:
        assert command(http, issued, "register", from_beginning=True).json()["status"] == "ready"
        delivery = command(http, issued, "fetch").json()["delivery"]
        assert delivery["event"]["event_id"] == event.event_id


def test_reevaluation_trusted_custom_capability_owner_filter(setup):
    dsn, store, cfg, _, service, operator, _ = setup
    registry = DEFAULT_REGISTRY.extend(
        DataTypeSpec("synthetic_result", "semantic", "extraction", supports_reevaluation=True)
    )
    bootstrap(dsn, registry)
    # Backend loads current registry per write.
    owner = operator.issue_principal(
        "extraction",
        read_types=["synthetic_result", "activity"],
        subscribe_types=["synthetic_result"],
    )
    other = operator.issue_principal(
        "api", read_types=["synthetic_result"], subscribe_types=["synthetic_result"]
    )
    with TestClient(create_app(service)) as http:
        assert (
            command(http, owner, "register", event_types=["reevaluation_requested"]).json()[
                "status"
            ]
            == "ready"
        )
        assert (
            command(
                http, other, "register", "api.worker", event_types=["reevaluation_requested"]
            ).status_code
            == 403
        )
        event = (
            store.client("extraction")
            .write([Create("synthetic_result", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        cause = (
            store.client("extraction")
            .write([Create("activity", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        request = (
            store.client("dependency")
            .write(
                [
                    PublishReevaluation(
                        "synthetic_result",
                        event.data_id,
                        Cause(
                            data_type="activity",
                            data_id=cause.data_id,
                            old_version=None,
                            new_version=1,
                        ),
                        "synthetic",
                    )
                ],
                SYS.deeper(),
            )
            .check()
            .events[0]
        )
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 3
        delivery = command(http, owner, "fetch").json()["delivery"]
        assert delivery["event"]["event_id"] == request.event_id
        assert (
            command(http, owner, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
        assert command(http, owner, "fetch").json()["status"] == "idle"


def test_committed_marker_survives_lost_ack_at_attempt_limit(setup):
    dsn, _, _, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        event = publish(setup)[0]
        assert command(http, issued, "fetch").json()["delivery"]
        expire(dsn)
        delivery = command(http, issued, "fetch").json()["delivery"]
        assert delivery["attempt"] == 2
        service.client(issued.token).write(
            [MarkProcessed("extraction.worker", event.event_id)], SYS
        ).check()
        expire(dsn)
        assert command(http, issued, "fetch").json()["status"] == "progress"
        assert command(http, issued, "fetch").json()["status"] == "idle"
        assert command(http, issued, "deadletters").json()["deadletters"] == []


def test_sdk_conflict_check_nacks_and_already_processed_checks_marker(setup):
    dsn, _, _, _, service, _, issued = setup
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):
        calls = []

        def handler(event):
            calls.append(event.event_id)
            if len(calls) == 1:
                sdk.write([Update("activity", event.data_id, 99, {})], SYS).check()
            sdk.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()
            sdk.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()

        sdk.subscribe("extraction.worker", handler)
        event = publish(setup)[0]
        assert sdk.poll_once() == 1
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT state,error_code FROM storage.consumer_deliveries"
            ).fetchone() == ("retry", "handler_failed")
        expire(dsn)
        assert sdk.poll_once() == 1
        assert sdk.run_until_idle() == 0 and calls == [event.event_id] * 2
        assert sdk.deadletters("extraction.worker") == []


def test_sdk_loss_fetch_and_ack_http_responses_live(setup):
    import httpx

    dsn, _, _, _, service, _, issued = setup

    class LoseResponses(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)
            self.lost = set()

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            response.read()
            operation = request.url.path.rsplit("/", 1)[-1]
            if (
                operation in {"fetch", "ack"}
                and operation not in self.lost
                and response.status_code == 200
            ):
                self.lost.add(operation)
                response.close()
                raise httpx.ReadError("simulated dropped response", request=request)
            return response

        def close(self):
            self.inner.close()

    seen = []
    with (
        live_server(service) as url,
        RemoteStorageClient(issued.token, url, transport=LoseResponses()) as sdk,
    ):

        def handler(event):
            sdk.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()
            seen.append(event.event_id)

        sdk.subscribe("extraction.worker", handler)
        event = publish(setup)[0]
        with pytest.raises(ServiceUnavailable):
            sdk.poll_once()
        assert seen == []
        expire(dsn)
        with pytest.raises(ServiceUnavailable):
            sdk.poll_once()
        assert seen == [event.event_id]
        assert sdk.run_until_idle() == 0
        assert seen == [event.event_id]


def test_fetch_broker_down_bounded_writer_independent(setup, nats_server):
    import time

    from .conftest import docker

    _, _, _, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        docker("pause", nats_server.name)
        try:
            with ThreadPoolExecutor(1) as pool:
                started = time.monotonic()
                pending = pool.submit(command, http, issued, "fetch")
                service.client(issued.token).write([Create("activity", new_id(), {})], SYS).check()
                response = pending.result(timeout=5)
                assert response.status_code == 503 and time.monotonic() - started < 5
        finally:
            docker("unpause", nats_server.name)
        assert command(http, issued, "fetch").json()["delivery"] is not None


def test_default_cursor_excludes_committed_but_unrelayed(setup):
    dsn, store, cfg, _, service, _, issued = setup
    old = (
        store.client("extraction").write([Create("activity", new_id(), {})], SYS).check().events[0]
    )
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):
        seen = []
        sdk.subscribe("extraction.worker", seen.append)
        new = (
            store.client("extraction")
            .write([Create("activity", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 2
        assert sdk.run_until_idle() == 1
        assert [e.event_id for e in seen] == [new.event_id] and new.event_id != old.event_id


def test_unverified_broker_payload_cannot_enter_delivery(setup):
    import nats

    dsn, _, cfg, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")

        async def inject():
            nc = await nats.connect(servers=list(cfg.servers), token=cfg.token)
            try:
                await nc.jetstream().publish(
                    cfg.prefix + ".data_changed.activity.extraction", b'{"secret": "unverified"}'
                )
            finally:
                await nc.close()

        asyncio.run(inject())
        response = command(http, issued, "fetch")
        assert response.status_code == 503 and "unverified" not in response.text
        with connection(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM storage.consumer_deliveries").fetchone() == (
                0,
            )


def test_plugin_history_denied_even_if_deployment_module_allowlisted(setup):
    dsn, _, cfg, policy, _, operator, _ = setup
    registry = DEFAULT_REGISTRY.extend(
        DataTypeSpec("synthetic_plugin", "semantic", "plugin_test", supports_reevaluation=True)
    )
    bootstrap(dsn, registry)
    issued = operator.issue_principal(
        "plugin_test", read_types=["synthetic_plugin"], subscribe_types=["synthetic_plugin"]
    )
    service = StorageService(
        dsn,
        relay_config=cfg,
        consumer_policy=replace(policy, history_modules=frozenset(["plugin_test"])),
    )
    with TestClient(create_app(service)) as http:
        assert (
            command(http, issued, "register", "plugin_test.worker", from_beginning=True).status_code
            == 403
        )
        assert command(http, issued, "register", "plugin_test.worker").json()["status"] == "ready"


def test_no_nats_clear_503_and_lifecycle_hook(setup):
    dsn, _, _, _, service, _, issued = setup
    with TestClient(create_app(StorageService(dsn))) as http:
        assert command(http, issued, "register").status_code == 503
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        delivery = command(http, issued, "fetch").json()["delivery"]
        with connection(dsn) as conn:
            conn.execute("UPDATE storage.consumers SET enabled=false")
        assert command(http, issued, "fetch").status_code == 403
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 403
        )
        assert command(http, issued, "deadletters").status_code == 403
        with pytest.raises(PermissionDenied):
            service.client(issued.token).write(
                [MarkProcessed("extraction.worker", delivery["event"]["event_id"])], SYS
            )


def test_same_module_principal_cannot_read_owner_progress_through_snapshot(setup):
    _, _, _, _, service, operator, issued = setup
    other = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        event = publish(setup)[0]
        owner, foreign = service.client(issued.token), service.client(other.token)
        owner.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()
        assert owner.is_processed("extraction.worker", event.event_id)
        assert not foreign.is_processed("extraction.worker", event.event_id)
        with owner.snapshot() as mine, foreign.snapshot() as theirs:
            assert mine.is_processed("extraction.worker", event.event_id)
            assert not theirs.is_processed("extraction.worker", event.event_id)


def test_registered_marker_write_rechecks_subscription_revocation(setup):
    _, _, _, _, service, operator, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        event = publish(setup)[0]
        operator.set_grants(issued.principal_id, read_types=["activity"])
        with pytest.raises(PermissionDenied):
            service.client(issued.token).write(
                [MarkProcessed("extraction.worker", event.event_id)], SYS
            )


def test_replay_does_not_publish_to_other_consumer(setup):
    dsn, _, _, _, service, _, issued = setup
    seen = []
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):

        def broken(event):
            raise RuntimeError("not retained")

        sdk.subscribe("extraction.worker", broken)
        sdk.subscribe("extraction.observer", seen.append)
        event = publish(setup)[0]
        sdk.poll_once()
        expire(dsn)
        sdk.poll_once()
        letters = sdk.deadletters("extraction.worker")
        assert len(letters) == 1
        sdk.replay_deadletter("extraction.worker", letters[0]["delivery_id"])
        sdk.poll_once()
        assert [e.event_id for e in seen] == [event.event_id]
        with connection(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM storage.outbox").fetchone() == (1,)


def test_create_response_loss_reuses_actual_durable(setup, monkeypatch):
    dsn, _, _, _, service, _, issued = setup
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call

    def lose(operation, **kwargs):
        result = original(operation, **kwargs)
        if operation == "create":
            raise ConsumerUnavailable("lost create response")
        return result

    with TestClient(create_app(service)) as http:
        with monkeypatch.context() as patch:
            patch.setattr(consumers.broker, "call", lose)
            assert command(http, issued, "register").json()["status"] == "pending"
        event = publish(setup)[0]
        assert command(http, issued, "register").json()["status"] == "ready"
        assert (
            command(http, issued, "fetch").json()["delivery"]["event"]["event_id"] == event.event_id
        )
        with connection(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM storage.consumers").fetchone() == (1,)


def test_real_broker_ack_response_lost_is_recoverable(setup, monkeypatch):
    dsn, _, _, _, service, _, issued = setup
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call

    def lose(operation, **kwargs):
        result = original(operation, **kwargs)
        if operation == "ack":
            raise ConsumerUnavailable("lost broker ack receipt")
        return result

    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        delivery = command(http, issued, "fetch").json()["delivery"]
        with monkeypatch.context() as patch:
            patch.setattr(consumers.broker, "call", lose)
            assert (
                command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
                == 503
            )
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT state,ack_pending FROM storage.consumer_deliveries"
            ).fetchone() == ("acked", True)
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
        assert command(http, issued, "fetch").json()["status"] == "idle"


def test_delivery_token_bound_to_consumer(setup):
    _, _, _, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register", "extraction.a")
        command(http, issued, "register", "extraction.b")
        publish(setup)
        a = command(http, issued, "fetch", "extraction.a").json()["delivery"]
        b = command(http, issued, "fetch", "extraction.b").json()["delivery"]
        assert a["event"] == b["event"]
        assert (
            command(
                http, issued, "ack", "extraction.b", delivery_token=a["delivery_token"]
            ).status_code
            == 403
        )
        assert (
            command(
                http, issued, "ack", "extraction.b", delivery_token=b["delivery_token"]
            ).status_code
            == 200
        )


def test_data_changed_type_filters_no_other_types(setup):
    dsn, store, cfg, _, service, _, issued = setup
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):
        seen = []
        sdk.subscribe("extraction.worker", seen.append, event_types=["data_changed"])
        store.client("attribution").write([Create("person", new_id(), {})], SYS).check()
        wanted = (
            store.client("extraction")
            .write([Create("activity", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 2
        assert sdk.run_until_idle() == 1
        assert [e.event_id for e in seen] == [wanted.event_id]


def test_legacy_marker_name_not_silently_adopted(setup):
    _, store, _, _, service, _, issued = setup
    store.client("extraction").write([MarkProcessed("extraction.legacy", new_id())], SYS).check()
    with TestClient(create_app(service)) as http:
        assert command(http, issued, "register", "extraction.legacy").status_code == 422


def test_reevaluation_cause_read_revoked_while_queued(setup):
    dsn, store, cfg, _, service, operator, _ = setup
    bootstrap(
        dsn,
        DEFAULT_REGISTRY.extend(
            DataTypeSpec("synthetic_result", "semantic", "extraction", supports_reevaluation=True)
        ),
    )
    owner = operator.issue_principal(
        "extraction",
        read_types=["synthetic_result", "activity"],
        subscribe_types=["synthetic_result"],
    )
    with TestClient(create_app(service)) as http:
        command(http, owner, "register", event_types=["reevaluation_requested"])
        target = (
            store.client("extraction")
            .write([Create("synthetic_result", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        cause = (
            store.client("extraction")
            .write([Create("activity", new_id(), {})], SYS)
            .check()
            .events[0]
        )
        store.client("dependency").write(
            [
                PublishReevaluation(
                    "synthetic_result",
                    target.data_id,
                    Cause(
                        data_type="activity", data_id=cause.data_id, old_version=None, new_version=1
                    ),
                    "SECRET_DESCRIPTION",
                )
            ],
            SYS.deeper(),
        ).check()
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 3
        operator.set_grants(
            owner.principal_id,
            read_types=["synthetic_result"],
            subscribe_types=["synthetic_result"],
        )
        response = command(http, owner, "fetch")
        assert response.status_code == 403
        assert "SECRET" not in response.text and cause.data_id not in response.text
