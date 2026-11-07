"""Regressions for queue ordering, principal provenance, filtering and receipts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, new_id
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

from .test_consumers import command, expire, publish
from .test_consumers import setup as setup
from .test_http import TestTransport
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()
LOW = "00000000-0000-7000-8000-000000000001"


@pytest.mark.parametrize("backoff", [False, True])
def test_reversed_uuid_replay_cannot_overtake_active_multiinstance(setup, monkeypatch, backoff):
    dsn, _, cfg, policy, service, _, issued = setup
    second = StorageService(dsn, relay_config=cfg, consumer_policy=policy)
    with TestClient(create_app(service)) as http, TestClient(create_app(second)) as other:
        command(http, issued, "register")
        publish(setup, 2)
        dead = command(http, issued, "fetch").json()["delivery"]
        command(http, issued, "nack", delivery_token=dead["delivery_token"])
        expire(dsn)
        dead = command(http, issued, "fetch").json()["delivery"]
        command(http, issued, "nack", delivery_token=dead["delivery_token"])
        active = command(http, issued, "fetch").json()["delivery"]
        if backoff:
            command(http, issued, "nack", delivery_token=active["delivery_token"])
            with connection(dsn) as conn:
                conn.execute(
                    "UPDATE storage.consumer_deliveries "
                    "SET available_at=clock_timestamp()+interval '1 hour' WHERE delivery_id=%s",
                    (active["delivery_id"],),
                )
        monkeypatch.setattr("ear_storage.consumers.new_id", lambda: LOW)
        replay = command(http, issued, "replay", delivery_id=dead["delivery_id"])
        assert replay.json()["delivery_id"] == LOW < active["delivery_id"]
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda h: command(h, issued, "fetch"), [http, other]))
        assert [r.json()["status"] for r in responses] == ["busy", "busy"]
        expire(dsn)
        offered = command(other, issued, "fetch").json()["delivery"]
        assert offered["event"]["event_id"] == active["event"]["event_id"]
        assert offered["delivery_id"] == active["delivery_id"]


def test_unregistered_marker_cannot_squat_same_module_name(setup):
    _, store, _, _, service, operator, issued = setup
    attacker = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    event = publish(setup)[0]
    with pytest.raises(PermissionDenied):
        service.client(attacker.token).write(
            [MarkProcessed("extraction.worker", event.event_id)], SYS
        )
    assert not store.is_processed("extraction.worker", event.event_id)
    with TestClient(create_app(service)) as http:
        assert command(http, issued, "register").status_code == 200
        with pytest.raises(PermissionDenied):
            service.client(issued.token).write([MarkProcessed("extraction.worker", new_id())], SYS)


def test_trusted_adoption_and_reservation_preserve_owner_only(setup):
    _, store, _, _, service, operator, owner = setup
    attacker = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    old = new_id()
    store.client("extraction").write([MarkProcessed("extraction.legacy", old)], SYS).check()
    operator.adopt_consumer_markers("extraction.legacy", owner.principal_id)
    with pytest.raises(PermissionDenied):
        service.client(attacker.token).write([MarkProcessed("extraction.legacy", new_id())], SYS)
    assert service.client(owner.token).is_processed("extraction.legacy", old)
    assert not service.client(attacker.token).is_processed("extraction.legacy", old)
    with TestClient(create_app(service)) as http:
        assert command(http, attacker, "register", "extraction.legacy").status_code == 403
        assert command(http, owner, "register", "extraction.legacy").status_code == 200
        event = publish(setup)[0]
        service.client(owner.token).write(
            [MarkProcessed("extraction.legacy", event.event_id)], SYS
        ).check()
        with pytest.raises(PermissionDenied):
            service.client(owner.token).write([MarkProcessed("extraction.legacy", new_id())], SYS)


def test_historical_unreadable_cause_does_not_block_new_valid_event(setup):
    dsn, store, cfg, _, service, operator, _ = setup
    bootstrap(
        dsn,
        DEFAULT_REGISTRY.extend(
            DataTypeSpec("synthetic_result", "semantic", "extraction", supports_reevaluation=True)
        ),
    )
    owner = operator.issue_principal(
        "extraction", read_types=["synthetic_result"], subscribe_types=["synthetic_result"]
    )
    target = (
        store.client("extraction")
        .write([Create("synthetic_result", new_id(), {})], SYS)
        .check()
        .events[0]
    )
    secret = (
        store.client("extraction").write([Create("activity", new_id(), {})], SYS).check().events[0]
    )

    def request(cause):
        return (
            store.client("dependency")
            .write(
                [
                    PublishReevaluation(
                        "synthetic_result",
                        target.data_id,
                        Cause(
                            data_type=cause.data_type,
                            data_id=cause.data_id,
                            old_version=None,
                            new_version=1,
                        ),
                        "private",
                    )
                ],
                SYS.deeper(),
            )
            .check()
            .events[0]
        )

    old = request(secret)
    with TestClient(create_app(service)) as http:
        assert (
            command(http, owner, "register", event_types=["reevaluation_requested"]).status_code
            == 200
        )
        new = request(target)
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 4
        filtered = command(http, owner, "fetch")
        assert filtered.json()["status"] == "progress" and "private" not in filtered.text
        response = command(http, owner, "fetch")
        assert response.status_code == 200
        delivery = response.json()["delivery"]
        assert delivery["event"]["event_id"] == new.event_id
        assert not store.is_processed("extraction.worker", old.event_id)
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT historical_filtered,ack_pending FROM storage.consumer_deliveries "
                "WHERE event_id=%s",
                (old.event_id,),
            ).fetchone() == (True, False)
        operator.set_grants(owner.principal_id)
        assert (
            command(http, owner, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 403
        )


@pytest.mark.parametrize("operation", ["ack", "nack"])
def test_expired_receipt_typed_but_foreign_and_invalid_still_denied(setup, operation):
    dsn, _, _, _, service, operator, issued = setup
    other = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        delivery = command(http, issued, "fetch").json()["delivery"]
        expire(dsn)
        response = command(http, issued, operation, delivery_token=delivery["delivery_token"])
        assert response.status_code == 409 and response.json()["code"] == "delivery_expired"
        assert (
            command(http, other, operation, delivery_token=delivery["delivery_token"]).status_code
            == 403
        )
        assert command(http, issued, operation, delivery_token="x" * 43).status_code == 403
        current = command(http, issued, "fetch").json()["delivery"]
        assert current["event"] == delivery["event"]
        assert (
            command(http, issued, operation, delivery_token=delivery["delivery_token"]).status_code
            == 409
        )
        operator.revoke(issued.principal_id)
        assert (
            command(http, issued, operation, delivery_token=delivery["delivery_token"]).status_code
            == 401
        )


@pytest.mark.parametrize("conflict", [False, True])
def test_sdk_long_handler_expiration_reconciles_without_stuck_completion(setup, conflict):
    dsn, _, _, _, service, _, issued = setup
    seen = []
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):

        def handler(event):
            seen.append(event.event_id)
            if not conflict or len(seen) > 1:
                sdk.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()
            expire(dsn)
            if conflict and len(seen) == 1:
                sdk.write([Update("activity", event.data_id, 99, {})], SYS).check()

        sdk.subscribe("extraction.worker", handler)
        event = publish(setup)[0]
        assert sdk.poll_once() == 1
        assert not sdk._completions
        sdk.run_until_idle()
        assert seen == [event.event_id] * (2 if conflict else 1)
        assert not sdk._completions


@pytest.mark.parametrize("operation", ["ack", "nack"])
def test_sdk_unknown_completion_preserves_receipt_until_confirmed_expired(setup, operation):
    dsn, _, _, _, service, _, issued = setup
    seen, requests = [], []
    with TestClient(create_app(service)) as http:

        class DropBeforeFinish(TestTransport):
            def handle_request(self, request):
                if request.url.path.endswith("/" + operation):
                    requests.append(request.content)
                    if len(requests) == 1:
                        raise httpx.ReadError("unknown result", request=request)
                return super().handle_request(request)

        with RemoteStorageClient(
            issued.token, "http://test", transport=DropBeforeFinish(http)
        ) as sdk:

            def handler(event):
                seen.append(event.event_id)
                if operation == "nack" and len(seen) == 1:
                    raise RuntimeError("retry")
                sdk.write([MarkProcessed("extraction.worker", event.event_id)], SYS).check()

            sdk.subscribe("extraction.worker", handler)
            event = publish(setup)[0]
            with pytest.raises(ServiceUnavailable):
                sdk.poll_once()
            assert sdk._completions and seen == [event.event_id]
            expire(dsn)
            assert sdk.poll_once() == 0
            assert requests[0] == requests[1] and not sdk._completions
            sdk.run_until_idle()
            assert seen == [event.event_id] * (2 if operation == "nack" else 1)


def test_normal_ack_pending_cannot_use_historical_exemption(setup, monkeypatch):
    from ear_storage.consumer_broker import ConsumerUnavailable

    dsn, _, _, _, service, operator, issued = setup
    consumers = service.consumers
    assert consumers is not None
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        delivery = command(http, issued, "fetch").json()["delivery"]
        assert (
            command(
                http,
                issued,
                "ack",
                delivery_token=delivery["delivery_token"],
                historical_filtered=True,
            ).status_code
            == 422
        )
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise ConsumerUnavailable("lost ack")

            patch.setattr(consumers.broker, "call", fail)
            assert (
                command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
                == 503
            )
        operator.set_grants(issued.principal_id)
        assert command(http, issued, "fetch").status_code == 403
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT historical_filtered,ack_pending FROM storage.consumer_deliveries"
            ).fetchone() == (False, True)


def test_db_constraint_rejects_second_inflight_and_historical_forgery(setup):
    import psycopg

    dsn, _, _, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        events = publish(setup, 2)
        command(http, issued, "fetch")
        with pytest.raises(psycopg.errors.UniqueViolation), connection(dsn) as conn:
            conn.execute(
                "INSERT INTO storage.consumer_deliveries(consumer,delivery_id,event_id,state) "
                "VALUES ('extraction.worker',%s,%s,'inflight')",
                (new_id(), events[1].event_id),
            )
        with pytest.raises(psycopg.errors.CheckViolation), connection(dsn) as conn:
            conn.execute("UPDATE storage.consumer_deliveries SET historical_filtered=true")


def test_expired_receipt_after_automatic_deadletter_can_reconcile(setup):
    dsn, _, _, _, service, _, issued = setup
    with TestClient(create_app(service)) as http:
        command(http, issued, "register")
        publish(setup)
        command(http, issued, "fetch")
        expire(dsn)
        last = command(http, issued, "fetch").json()["delivery"]
        expire(dsn)
        assert command(http, issued, "fetch").json()["status"] == "progress"
        assert (
            command(http, issued, "ack", delivery_token=last["delivery_token"]).status_code == 409
        )
