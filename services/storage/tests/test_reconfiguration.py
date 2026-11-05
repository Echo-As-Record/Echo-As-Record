"""Reconfiguration acceptance against isolated PostgreSQL and JetStream."""

from __future__ import annotations

import asyncio

import pytest
from ear_contracts import new_id
from ear_storage.relay import RelayStore, run_relay
from ear_storage_client import Context, Create
from ear_storage_client.consumer_wire import ConsumerRequest, DeliveryCommand, RegisterConsumer

from .test_management import command
from .test_management_integration import enable, execute
from .test_management_integration import env as env
from .test_management_integration import setup as setup

pytestmark = [pytest.mark.postgres, pytest.mark.nats]


def test_growth_delivers_future_type_preserves_old_backlog_for_two_consumers(setup):
    dsn, backend, cfg, service, _, _, processor = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    assert (
        consumers.register(
            plugin.token, RegisterConsumer(consumer="sample.sdk", event_types=["data_changed"])
        )["status"]
        == "ready"
    )
    old_person, activity, new_person = new_id(), new_id(), new_id()
    backend.client("attribution").write(
        [Create("person", old_person, {})], Context.system()
    ).check()
    backend.client("extraction").write([Create("activity", activity, {})], Context.system()).check()
    # Deliberately unrelayed: DB commit boundary is NOT a broker stream sequence.
    accepted, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person"),
            subscribe_types=("activity", "person"),
        ),
    )
    assert result["status"] == "succeeded", result
    assert processor.process(accepted["feedback_id"]) == result
    backend.client("attribution").write(
        [Create("person", new_person, {})], Context.system()
    ).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    for name in ("sample.managed", "sample.sdk"):
        seen = []
        for _ in range(12):
            response = consumers.fetch(plugin.token, ConsumerRequest(consumer=name))
            delivery = response["delivery"]
            if delivery:
                seen.append(delivery["event"]["data_id"])
                consumers.finish(
                    plugin.token,
                    DeliveryCommand(consumer=name, delivery_token=delivery["delivery_token"]),
                    nack=False,
                )
            if response["status"] == "idle":
                break
        assert seen == [activity, new_person]


def test_shrink_closes_auth_gate_even_without_broker_configuration(setup):
    from ear_storage.auth import AuthenticationError
    from ear_storage.management import ManagementProcessor, submit
    from ear_storage.schema import connection

    dsn, backend, _, service, _, admin, _ = setup
    plugin = enable(setup)
    item = new_id()
    backend.client("extraction").write([Create("activity", item, {})], Context.system()).check()
    accepted = submit(
        service.client(admin.token),
        command("set_permissions", read_types=("sample_item",), subscribe_types=()),
    )
    processor = ManagementProcessor(backend)
    result = processor.process(accepted["feedback_id"])
    assert result["status"] == "pending", result
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT data_type FROM storage.type_grants WHERE principal_id=%s ORDER BY 1",
            (plugin.principal_id,),
        ).fetchall() == [("sample_item",)]
        assert conn.execute(
            "SELECT recovery_pending FROM storage.module_lifecycle WHERE module='sample'"
        ).fetchone() == (True,)
    with pytest.raises(AuthenticationError):
        service.client(plugin.token).get(item)
    resumed = setup[-1].process(accepted["feedback_id"])
    assert resumed["status"] == "succeeded", resumed
    assert resumed["need_recovery_decision"]


def growth():
    return command(
        "set_permissions",
        read_types=("sample_item", "activity", "person"),
        subscribe_types=("activity", "person"),
    )


def drain(consumers, token, name):
    seen = []
    for _ in range(30):
        response = consumers.fetch(token, ConsumerRequest(consumer=name))
        if response["delivery"]:
            delivery = response["delivery"]
            seen.append(delivery["event"]["data_id"])
            consumers.finish(
                token,
                DeliveryCommand(consumer=name, delivery_token=delivery["delivery_token"]),
                nack=False,
            )
        if response["status"] == "idle":
            return seen
    pytest.fail("consumer failed to become idle")


@pytest.mark.parametrize("failure", ["first", "second", "lost_response"])
def test_partial_broker_create_retry_stable_all_consumers_and_ack_loss(setup, monkeypatch, failure):
    import json

    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.schema import connection, one
    from ear_storage_client import MarkProcessed

    dsn, backend, cfg, service, _, admin, processor = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    consumers.register(
        plugin.token, RegisterConsumer(consumer="sample.sdk", event_types=["data_changed"])
    )
    first, backlog = new_id(), new_id()
    events = (
        backend.client("extraction")
        .write([Create("activity", first, {}), Create("activity", backlog, {})], Context.system())
        .check()
        .events
    )
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    # Markers, ownership and names survive the replacement durable.
    service.client(plugin.token).write(
        [MarkProcessed("sample.managed", events[0].event_id)], Context.system()
    ).check()
    original = consumers.broker.call
    calls = []
    failed = False

    def flaky(operation, **kwargs):
        nonlocal failed
        if operation == "create":
            calls.append(kwargs["durable"])
            if not failed and len(calls) == (2 if failure == "second" else 1):
                failed = True
                if failure == "lost_response":
                    original(operation, **kwargs)
                raise ConsumerUnavailable("synthetic create failure")
        return original(operation, **kwargs)

    monkeypatch.setattr(consumers.broker, "call", flaky)
    from ear_storage.management import submit

    body = growth()
    accepted = submit(service.client(admin.token), body)
    pending = processor.process(accepted["feedback_id"])
    assert pending["status"] == "pending" and pending["error_code"] is not None
    with connection(dsn) as conn:
        plan = json.loads(
            one(
                conn,
                "SELECT plan_text FROM storage.consumer_reconfigurations WHERE feedback_id=%s",
                (accepted["feedback_id"],),
            )[0]
        )
        assert conn.execute("SELECT count(*) FROM storage.consumer_generations").fetchone() == (4,)
        assert conn.execute(
            "SELECT count(*) FROM storage.consumers WHERE module='sample' AND ready"
        ).fetchone() == (0,)
    assert service.client(plugin.token).get(first) is not None
    final = processor.process(accepted["feedback_id"])
    assert final["status"] == "succeeded", final
    assert processor.process(accepted["feedback_id"]) == final
    assert submit(service.client(admin.token), body) == accepted
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.consumer_generations").fetchone() == (4,)
        assert conn.execute(
            "SELECT name,principal_id,generation FROM storage.consumers ORDER BY name"
        ).fetchall() == [
            (r["name"], plugin.principal_id, r["generation"]) for r in plan["resources"]
        ]
    assert set(calls) == {r["generation"] for r in plan["resources"]}
    new_person = new_id()
    backend.client("attribution").write(
        [Create("person", new_person, {})], Context.system()
    ).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    lost_ack = False

    def ack_lost(operation, **kwargs):
        nonlocal lost_ack
        result = original(operation, **kwargs)
        if operation == "ack" and not lost_ack:
            lost_ack = True
            raise ConsumerUnavailable("synthetic ack response loss")
        return result

    monkeypatch.setattr(consumers.broker, "call", ack_lost)
    # Start replay excludes the already processed event. A lost broker ack
    # after DB completion must not invoke the backlog handler a second time.
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery and delivery["event"]["data_id"] == backlog
    receipt = DeliveryCommand(consumer="sample.managed", delivery_token=delivery["delivery_token"])
    with pytest.raises(ConsumerUnavailable):
        consumers.finish(plugin.token, receipt, nack=False)
    assert consumers.finish(plugin.token, receipt, nack=False)["status"] == "acked"
    assert drain(consumers, plugin.token, "sample.managed") == [new_person]
    assert drain(consumers, plugin.token, "sample.sdk") == [first, backlog, new_person]


@pytest.mark.parametrize("when", ["during_create", "after_switch"])
def test_existing_inflight_receipt_remains_ackable_and_backlog_not_skipped(
    setup, monkeypatch, when
):
    from concurrent.futures import ThreadPoolExecutor

    dsn, backend, cfg, service, _, _, _ = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    first, backlog = new_id(), new_id()
    backend.client("extraction").write(
        [Create("activity", first, {}), Create("activity", backlog, {})], Context.system()
    ).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery and delivery["event"]["data_id"] == first
    receipt = DeliveryCommand(consumer="sample.managed", delivery_token=delivery["delivery_token"])
    original = consumers.broker.call
    acknowledged = False
    during_person, during_activity = new_id(), new_id()

    def create(operation, **kwargs):
        nonlocal acknowledged
        if operation == "create" and not acknowledged:
            acknowledged = True
            # Both an ack and a writer must complete while broker I/O is in progress.
            with ThreadPoolExecutor(1) as pool:
                if when == "during_create":
                    assert (
                        pool.submit(consumers.finish, plugin.token, receipt, nack=False).result(3)[
                            "status"
                        ]
                        == "acked"
                    )
                pool.submit(
                    lambda: (
                        backend.client("attribution")
                        .write([Create("person", during_person, {})], Context.system())
                        .check()
                    )
                ).result(3)
                pool.submit(
                    lambda: (
                        backend.client("extraction")
                        .write([Create("activity", during_activity, {})], Context.system())
                        .check()
                    )
                ).result(3)
        return original(operation, **kwargs)

    monkeypatch.setattr(consumers.broker, "call", create)
    _, result = execute(setup, growth())
    assert result["status"] == "succeeded", result
    if when == "after_switch":
        assert consumers.finish(plugin.token, receipt, nack=False)["status"] == "acked"
    future = new_id()
    backend.client("attribution").write([Create("person", future, {})], Context.system()).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    assert drain(consumers, plugin.token, "sample.managed") == [backlog, during_activity, future]


@pytest.mark.parametrize("interruption", ["admin", "identity", "disable", "grants"])
def test_concurrent_interrupt_cannot_activate_or_leave_terminal_intent(
    setup, monkeypatch, interruption
):
    from ear_storage.schema import connection

    dsn, _, _, service, operator, admin, _ = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call
    interrupted = False

    def interrupt(operation, **kwargs):
        nonlocal interrupted
        result = original(operation, **kwargs)
        if operation == "create" and not interrupted:
            interrupted = True
            if interruption == "admin":
                operator.set_admin(admin.principal_id, admin=False)
            elif interruption == "identity":
                operator.revoke(plugin.principal_id)
            elif interruption == "grants":
                operator.set_grants(plugin.principal_id, read_types=["sample_item"])
            else:
                _, disabled = execute(setup, command("disable"))
                assert not disabled["enabled"]
        return result

    monkeypatch.setattr(consumers.broker, "call", interrupt)
    _, result = execute(setup, growth())
    assert result["status"] == "failed", result
    with connection(dsn) as conn:
        assert conn.execute("SELECT state FROM storage.consumer_reconfigurations").fetchall() == [
            ("cancelled",)
        ]
        assert not conn.execute(
            "SELECT 1 FROM storage.module_permissions WHERE module='sample' AND data_type='person'"
        ).fetchone()
        assert conn.execute(
            "SELECT count(*) FROM storage.consumer_generations WHERE state='prepared'"
        ).fetchone() == (0,)
        if interruption == "disable":
            assert conn.execute(
                "SELECT enabled FROM storage.module_lifecycle WHERE module='sample'"
            ).fetchone() == (False,)


def test_expired_old_inflight_retries_same_event_then_accepts_new_type(setup):
    from ear_storage.schema import connection
    from ear_storage_client.remote_errors import DeliveryExpired

    dsn, backend, cfg, service, _, _, _ = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    first, backlog, future = new_id(), new_id(), new_id()
    backend.client("extraction").write(
        [Create("activity", first, {}), Create("activity", backlog, {})], Context.system()
    ).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery
    receipt = DeliveryCommand(consumer="sample.managed", delivery_token=delivery["delivery_token"])
    _, result = execute(setup, growth())
    assert result["status"] == "succeeded"
    with connection(dsn) as conn:
        conn.execute(
            "UPDATE storage.consumer_deliveries SET lease_until='-infinity' "
            "WHERE consumer='sample.managed' AND state='inflight'"
        )
    with pytest.raises(DeliveryExpired):
        consumers.finish(plugin.token, receipt, nack=False)
    retried = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert retried and retried["event"]["data_id"] == first
    assert retried["delivery_id"] == delivery["delivery_id"] and retried["attempt"] == 2
    with pytest.raises(DeliveryExpired):
        consumers.finish(plugin.token, receipt, nack=False)
    consumers.finish(
        plugin.token,
        DeliveryCommand(consumer="sample.managed", delivery_token=retried["delivery_token"]),
        nack=False,
    )
    backend.client("attribution").write([Create("person", future, {})], Context.system()).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    assert drain(consumers, plugin.token, "sample.managed") == [backlog, future]


def test_shrink_broker_failure_keeps_gate_closed_pending_data_and_recovery_choice(
    setup, monkeypatch
):
    from ear_storage.auth import AuthenticationError
    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.schema import connection
    from ear_storage_client import PermissionDenied

    dsn, backend, cfg, service, _, _, processor = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    item = new_id()
    backend.client("extraction").write([Create("activity", item, {})], Context.system()).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery
    original = consumers.broker.call

    def fail_after_create(operation, **kwargs):
        result = original(operation, **kwargs)
        if operation == "create":
            raise ConsumerUnavailable("synthetic response lost")
        return result

    monkeypatch.setattr(consumers.broker, "call", fail_after_create)
    accepted, pending = execute(
        setup, command("set_permissions", read_types=("sample_item",), subscribe_types=())
    )
    assert pending["status"] == "pending" and pending["need_recovery_decision"]
    with pytest.raises(AuthenticationError):
        service.client(plugin.token).get(item)
    monkeypatch.setattr(consumers.broker, "call", original)
    final = processor.process(accepted["feedback_id"])
    assert final["status"] == "succeeded" and final["need_recovery_decision"]
    assert backend.get(item) is not None
    with pytest.raises(PermissionDenied):
        consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT delivery_id,state FROM storage.consumer_deliveries "
            "WHERE consumer='sample.managed'"
        ).fetchall() == [(delivery["delivery_id"], "inflight")]
        assert conn.execute(
            "SELECT count(*) FROM storage.consumer_receipts WHERE token=%s",
            (delivery["delivery_token"],),
        ).fetchone() == (1,)
    _, restored = execute(
        setup,
        command(
            "set_permissions", read_types=("sample_item", "activity"), subscribe_types=("activity",)
        ),
    )
    assert restored["error_code"] == "need_recovery_decision"
    assert restored["need_recovery_decision"]


def retired_ack(setup, monkeypatch):
    """Real G1 delivery, committed completion, failed broker ack, then managed G2."""
    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.schema import connection

    dsn, backend, cfg, service, _, _, _ = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    item = new_id()
    backend.client("extraction").write([Create("activity", item, {})], Context.system()).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery
    original = consumers.broker.call

    def fail_ack(operation, **kwargs):
        if operation == "ack":
            raise ConsumerUnavailable("synthetic ack failure before broker")
        return original(operation, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(consumers.broker, "call", fail_ack)
        with pytest.raises(ConsumerUnavailable):
            consumers.finish(
                plugin.token,
                DeliveryCommand(
                    consumer="sample.managed", delivery_token=delivery["delivery_token"]
                ),
                nack=False,
            )
    _, result = execute(setup, growth())
    assert result["status"] == "succeeded"
    with connection(dsn) as conn:
        pending = conn.execute(
            "SELECT broker_generation,broker_reply FROM storage.consumer_deliveries "
            "WHERE consumer='sample.managed' AND delivery_id=%s AND ack_pending "
            "AND state='acked' AND NOT historical_filtered",
            (delivery["delivery_id"],),
        ).fetchone()
        assert pending
        assert conn.execute(
            "SELECT state FROM storage.consumer_generations WHERE generation=%s", (pending[0],)
        ).fetchone() == ("retired",)
    return plugin, delivery, pending, original


def broker_pending(cfg, generation):
    from .test_relay import reader

    async def inspect():
        async with reader(cfg) as js:
            return (await js.consumer_info(cfg.stream, generation)).num_ack_pending

    return asyncio.run(inspect())


@pytest.mark.parametrize("restriction", ["read", "subscribe", "broker"])
def test_retired_ack_does_not_block_current_authorized_delivery(setup, monkeypatch, restriction):
    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.http import create_app
    from ear_storage.schema import connection
    from fastapi.testclient import TestClient

    dsn, backend, cfg, service, operator, admin, _ = setup
    plugin, old, pending, original = retired_ack(setup, monkeypatch)
    consumers = service.consumers
    assert consumers is not None
    calls = []

    def broker(operation, **kwargs):
        if operation == "ack":
            calls.append(kwargs["reply"])
            if restriction == "broker" and kwargs["reply"] == pending[1]:
                raise ConsumerUnavailable("retired broker temporarily unavailable")
        return original(operation, **kwargs)

    monkeypatch.setattr(consumers.broker, "call", broker)
    if restriction != "broker":
        operator.set_grants(
            plugin.principal_id,
            read_types=["sample_item", "person"]
            + (["activity"] if restriction == "subscribe" else []),
            subscribe_types=["person"],
        )
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT recovery_pending FROM storage.module_lifecycle WHERE module='sample'"
        ).fetchone() == (False,)
    future = new_id()
    backend.client("attribution").write([Create("person", future, {})], Context.system()).check()
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    with TestClient(create_app(service)) as http:

        def request(operation, token=plugin.token, **extra):
            return http.post(
                "/v1/consumers/" + operation,
                headers={"Authorization": "Bearer " + token},
                json={"codec": 1, "consumer": "sample.managed", **extra},
            )

        assert request("fetch", admin.token).status_code == 403
        assert request("fetch", "x" * 43).status_code == 401
        assert calls == []
        response = request("fetch")
        assert response.status_code == 200, response.text
        delivery = response.json()["delivery"]
        assert delivery and delivery["event"]["data_id"] == future
        assert old["event"]["event_id"] not in response.text
        assert pending[0] not in response.text and pending[1] not in response.text
        assert request("ack", delivery_token=delivery["delivery_token"]).status_code == 200
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT state,ack_pending,historical_filtered FROM storage.consumer_deliveries "
                "WHERE delivery_id=%s",
                (old["delivery_id"],),
            ).fetchone() == ("acked", True, False)
            assert conn.execute(
                "SELECT ack_pending FROM storage.consumer_deliveries WHERE delivery_id=%s",
                (delivery["delivery_id"],),
            ).fetchone() == (False,)
        assert broker_pending(cfg, pending[0]) == 1
        if restriction != "broker":
            assert pending[1] not in calls
        monkeypatch.setattr(consumers.broker, "call", original)
        operator.set_grants(
            plugin.principal_id,
            read_types=["sample_item", "activity", "person"],
            subscribe_types=["activity", "person"],
        )
        for _ in range(4):
            assert request("fetch").status_code == 200
            if broker_pending(cfg, pending[0]) == 0:
                break
        assert broker_pending(cfg, pending[0]) == 0
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT ack_pending,historical_filtered FROM storage.consumer_deliveries "
                "WHERE delivery_id=%s",
                (old["delivery_id"],),
            ).fetchone() == (False, False)
        operator.revoke(plugin.principal_id)
        assert request("fetch").status_code == 401


@pytest.mark.parametrize("gate", ["recovery", "auth_not_active", "foreign_generation"])
def test_retired_ack_cleanup_never_bypasses_security_gates(setup, monkeypatch, gate):
    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.schema import connection
    from ear_storage_client import PermissionDenied

    dsn, _, cfg, service, _, _, _ = setup
    plugin, old, pending, original = retired_ack(setup, monkeypatch)
    consumers = service.consumers
    assert consumers is not None
    calls = []

    def broker(operation, **kwargs):
        calls.append(operation)
        return original(operation, **kwargs)

    monkeypatch.setattr(consumers.broker, "call", broker)
    error = PermissionDenied
    if gate == "recovery":
        _, result = execute(
            setup,
            command(
                "set_permissions", read_types=("sample_item", "person"), subscribe_types=("person",)
            ),
        )
        assert result["need_recovery_decision"]
    elif gate == "auth_not_active":

        def deny(*args, **kwargs):
            raise PermissionDenied("auth_not_active")

        monkeypatch.setattr("ear_storage.consumers.authorize_event", deny)
    else:
        # A generation belonging to another consumer must not inherit current config.
        consumers.register(
            plugin.token, RegisterConsumer(consumer="sample.other", event_types=["data_changed"])
        )
        with connection(dsn) as conn:
            foreign_row = conn.execute(
                "SELECT generation FROM storage.consumers WHERE name='sample.other'"
            ).fetchone()
            assert foreign_row is not None
            foreign = foreign_row[0]
            conn.execute(
                "UPDATE storage.consumer_deliveries SET broker_generation=%s WHERE delivery_id=%s",
                (foreign, old["delivery_id"]),
            )
        error = ConsumerUnavailable
    calls.clear()
    with pytest.raises(error):
        consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    assert calls == []
    assert broker_pending(cfg, pending[0]) == 1
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT ack_pending FROM storage.consumer_deliveries WHERE delivery_id=%s",
            (old["delivery_id"],),
        ).fetchone() == (True,)
