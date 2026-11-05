"""Actual P3 PostgreSQL + JetStream, dynamic registration without service restart."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from ear_contracts import VersionRef, new_id
from ear_storage.auth import AuthenticationError, AuthOperator
from ear_storage.consumer_broker import ConsumerUnavailable
from ear_storage.consumers import ConsumerPolicy
from ear_storage.management import CONSUMER, FEEDBACK, RESULT, ManagementProcessor, submit
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    PermissionDenied,
    RegisterDependencies,
    Update,
)
from ear_storage_client.consumer_wire import ConsumerRequest, DeliveryCommand

from .test_management import command, declaration
from .test_relay import env as env
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()


@pytest.fixture
def setup(env):
    dsn, backend, cfg = env
    service = StorageService(
        dsn,
        relay_config=cfg,
        consumer_policy=ConsumerPolicy(max_attempts=2, retry_base=0.01, retry_max=0.02),
    )
    operator = AuthOperator(dsn)
    admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
    processor = ManagementProcessor(backend, service.consumers)

    # Only relay owns stream bootstrap; management verifies it, never assumes readiness.
    async def provision():
        async with reader(cfg) as js:
            await js.add_stream(config=cfg.stream_config())

    asyncio.run(provision())
    return dsn, backend, cfg, service, operator, admin, processor


def execute(setup, body):
    _, _, _, service, _, admin, processor = setup
    accepted = submit(service.client(admin.token), body)
    return accepted, processor.process(accepted["feedback_id"])


def enable(setup, *, dependencies=False):
    _, _, _, _, operator, _, _ = setup
    body = declaration()
    if dependencies:
        body = body.model_copy(
            update={"types": (body.types[0].model_copy(update={"requires_dependencies": True}),)}
        )
    _, registered = execute(setup, body)
    assert registered["status"] == "succeeded"
    plugin = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    _, result = execute(setup, command("enable"))
    assert result["status"] == "succeeded" and result["enabled"]
    return plugin


def test_live_register_graph_field_dependency_disable_preserves_data(setup):
    _, backend, _, service, _, _, _ = setup
    plugin = enable(setup, dependencies=True)
    client = service.client(plugin.token)
    assert client.registry.writer_of("sample_item") == "sample"
    a, b = new_id(), new_id()
    backend.client("extraction").write([Create("activity", a, {"name": "first"})], SYS).check()
    ref = VersionRef(data_type="activity", id=a, version=1)
    with pytest.raises(InvalidRequest):
        client.write([Create("sample_item", b, {})], SYS)
    client.write(
        [
            Create("sample_item", b, {}),
            RegisterDependencies("sample_item", b, [DependencyInput(ref, ("name",))]),
        ],
        SYS,
    ).check()
    backend.client("extraction").write([Update("activity", a, 1, {"name": "second"})], SYS).check()
    assert client.stale_dependencies([a])
    loop = client.write(
        [
            Update("sample_item", b, 1, {"changed": True}),
            RegisterDependencies(
                "sample_item", b, [VersionRef(data_type="sample_item", id=b, version=2)]
            ),
        ],
        SYS,
    )
    assert loop.conflict and loop.conflict.reason == "dependency_cycle"
    _, disabled = execute(setup, command("disable"))
    assert disabled["enabled"] is False
    for operation in [
        lambda: client.get(b),
        lambda: client.write([Create("sample_item", new_id(), {})], SYS),
        lambda: client.registry,
    ]:
        with pytest.raises(AuthenticationError):
            operation()
    assert backend.get(b) is not None


def test_queued_disable_and_recovery_requires_choice(setup):
    _, backend, cfg, service, _, _, _ = setup
    plugin = enable(setup)
    consumers = service.consumers
    assert consumers is not None
    eid = (
        backend.client("extraction")
        .write([Create("activity", new_id(), {})], SYS)
        .check()
        .events[0]
        .event_id
    )
    asyncio.run(run_relay(RelayStore(backend._dsn, cfg), once=True))
    delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))["delivery"]
    assert delivery and delivery["event"]["event_id"] == eid
    execute(setup, command("disable"))
    with pytest.raises(AuthenticationError):
        consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    with pytest.raises(AuthenticationError):
        consumers.finish(
            plugin.token,
            DeliveryCommand(consumer="sample.managed", delivery_token=delivery["delivery_token"]),
            nack=False,
        )
    _, result = execute(setup, command("enable"))
    assert result["enabled"] is False and result["error_code"] == "need_recovery_decision"


def test_permission_shrink_read_snapshot_queue_and_regrant_blocked(setup):
    _, backend, cfg, service, _, _, _ = setup
    plugin = enable(setup)
    client = service.client(plugin.token)
    rid = new_id()
    backend.client("extraction").write([Create("activity", rid, {})], SYS).check()
    lease = client.create_snapshot()
    assert client.read_snapshot(lease.snapshot_id).get(rid)
    execute(setup, command("set_permissions", read_types=("sample_item",), subscribe_types=()))
    assert client.get(rid) is None
    with pytest.raises(PermissionDenied):
        client.read_snapshot(lease.snapshot_id).get(rid)
    backend.client("extraction").write([Update("activity", rid, 1, {"new": True})], SYS).check()
    asyncio.run(run_relay(RelayStore(backend._dsn, cfg), once=True))
    assert service.consumers is not None
    with pytest.raises(PermissionDenied):
        service.consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    _, result = execute(
        setup,
        command(
            "set_permissions", read_types=("sample_item", "activity"), subscribe_types=("activity",)
        ),
    )
    assert result["error_code"] == "need_recovery_decision"
    assert client.get(rid) is None


def test_resource_retry_without_false_enabled_and_no_network_under_gate(setup, monkeypatch):
    dsn, backend, _, service, operator, _, processor = setup
    body = declaration()
    execute(setup, body)
    operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    assert service.consumers is not None
    broker = service.consumers.broker
    original = broker.call

    def unavailable(*args, **kwargs):
        # A concurrent writer must finish during broker I/O, proving gate release.
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(
                lambda: (
                    backend.client("extraction")
                    .write([Create("activity", new_id(), {})], SYS)
                    .check()
                )
            )
            future.result(timeout=3)
        raise ConsumerUnavailable("private credential must not leak")

    monkeypatch.setattr(broker, "call", unavailable)
    accepted, pending = execute(setup, command("enable"))
    assert pending["status"] == "pending" and pending["enabled"] is False
    assert pending["error_code"] == "broker_unavailable"
    with connection(dsn) as conn:
        generation = conn.execute(
            "SELECT generation FROM storage.consumers WHERE module='sample'"
        ).fetchone()
    monkeypatch.setattr(broker, "call", original)
    final = processor.process(accepted["feedback_id"])
    assert final["status"] == "succeeded" and final["enabled"]
    with connection(dsn) as conn:
        assert (
            conn.execute(
                "SELECT generation FROM storage.consumers WHERE module='sample'"
            ).fetchone()
            == generation
        )


def test_worker_handles_feedback_accepted_before_first_start_no_third_event(setup):
    dsn, backend, cfg, service, operator, admin, processor = setup
    worker = operator.issue_principal("storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK])
    accepted = submit(service.client(admin.token), declaration())
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    assert processor.poll_once(worker.token) == 1
    assert processor.poll_once(worker.token) == 0
    assert "sample_item" in backend.registry
    result = service.client(admin.token).get_system_record(accepted["result_id"])
    assert result and result.payload["status"] == "succeeded"
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.outbox").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM storage.processed WHERE consumer=%s", (CONSUMER,)
        ).fetchone() == (1,)


def test_enable_rechecks_admin_after_broker_io(setup, monkeypatch):
    _, _, _, service, operator, admin, _ = setup
    body = declaration()
    execute(setup, body)
    operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    assert service.consumers is not None
    original = service.consumers.broker.call

    def revoke(operation, **kwargs):
        result = original(operation, **kwargs)
        operator.set_admin(admin.principal_id, admin=False)
        return result

    monkeypatch.setattr(service.consumers.broker, "call", revoke)
    _, result = execute(setup, command("enable"))
    assert result["error_code"] == "authority_revoked" and result["enabled"] is False


def test_disable_with_unfetched_outbox_does_not_silently_skip(setup):
    _, backend, _, _, _, _, _ = setup
    enable(setup)
    backend.client("extraction").write([Create("activity", new_id(), {})], SYS).check()
    execute(setup, command("disable"))
    _, result = execute(setup, command("enable"))
    assert result["error_code"] == "need_recovery_decision" and not result["enabled"]


def test_disable_enable_without_missed_changes_and_disconnect_stays_enabled(setup):
    _, _, _, service, _, _, _ = setup
    plugin = enable(setup)
    assert service.client(plugin.token).module == "sample"
    _, status = execute(setup, command("status"))
    assert status["enabled"] and status["online"] is None
    execute(setup, command("disable"))
    _, result = execute(setup, command("enable"))
    assert result["status"] == "succeeded" and result["enabled"]
    assert service.client(plugin.token).module == "sample"


def management_state(dsn):
    with connection(dsn) as conn:
        return (
            conn.execute("SELECT * FROM storage.module_lifecycle ORDER BY module").fetchall(),
            conn.execute("SELECT * FROM storage.module_permissions ORDER BY 1,2").fetchall(),
            conn.execute(
                "SELECT * FROM storage.type_grants WHERE principal_id IN "
                "(SELECT id FROM storage.principals WHERE module='sample') ORDER BY 1,2"
            ).fetchall(),
            conn.execute(
                "SELECT name,principal_id,module,generation,config_text,destination,"
                "creation_cursor,ready,enabled,fence,lease_until::text "
                "FROM storage.consumers ORDER BY name"
            ).fetchall(),
        )


def test_subscription_growth_converges_and_read_only_growth_keeps_consumers(setup):
    dsn, _, _, service, _, _, _ = setup
    plugin = enable(setup)
    _, grown = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person"),
            subscribe_types=("activity", "person"),
        ),
    )
    assert grown["status"] == "succeeded"
    before = management_state(dsn)
    _, allowed = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person", "place"),
            subscribe_types=("activity", "person"),
        ),
    )
    assert allowed["status"] == "succeeded"
    assert allowed["revision"] == grown["revision"] + 1
    assert "person" in service.client(plugin.token).registry
    assert management_state(dsn)[3] == before[3]


@pytest.mark.parametrize("broken", ["ready", "config", "broker", "identity"])
def test_enabled_enable_never_masks_broken_configuration(setup, monkeypatch, broken):
    dsn, _, _, service, operator, _, _ = setup
    plugin = enable(setup)
    assert service.consumers is not None
    if broken == "broker":

        def unavailable(*args, **kwargs):
            raise ConsumerUnavailable("synthetic")

        monkeypatch.setattr(service.consumers.broker, "call", unavailable)
    elif broken == "identity":
        operator.set_grants(plugin.principal_id, read_types=["sample_item"])
    else:
        with connection(dsn) as conn:
            if broken == "ready":
                conn.execute("UPDATE storage.consumers SET ready=false WHERE module='sample'")
            else:
                conn.execute(
                    "UPDATE storage.consumers SET config_text="
                    "jsonb_set(config_text::jsonb,'{filters}','[]')::text WHERE module='sample'"
                )
    before = management_state(dsn)
    _, result = execute(setup, command("enable"))
    assert result["status"] != "succeeded" and result["error_code"]
    assert management_state(dsn) == before


def pending_worker(setup, monkeypatch, failure="verify"):
    dsn, _, cfg, service, operator, admin, processor = setup
    body = declaration()
    worker = operator.issue_principal("storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK])
    operator.reserve_consumer_identity(CONSUMER, worker.principal_id)
    execute(setup, body)
    operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    enable_body = command("enable")
    accepted = submit(service.client(admin.token), enable_body)
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    assert service.consumers is not None
    original = service.consumers.broker.call

    def fail(operation, **kwargs):
        if operation == failure:
            if operation == "create":
                original(operation, **kwargs)  # durable created, confirmation lost
            raise ConsumerUnavailable("synthetic resource failure")
        return original(operation, **kwargs)

    monkeypatch.setattr(service.consumers.broker, "call", fail)
    # First event was processed by registration; then the actual enable delivery.
    assert processor.poll_once(worker.token) == 0
    assert processor.poll_once(worker.token) == 1
    result = service.client(admin.token).get_system_record(accepted["result_id"])
    assert result and result.payload["status"] == "pending"
    with connection(dsn) as conn:
        conn.execute("UPDATE storage.consumer_deliveries SET available_at='-infinity'")
    return worker, accepted, enable_body, original


def await_result(setup, worker, accepted, status):
    _, _, _, service, _, admin, processor = setup
    for _ in range(20):
        processor.poll_once(worker.token)
        result = service.client(admin.token).get_system_record(accepted["result_id"])
        assert result
        if result.payload["status"] == status:
            return result.payload
    raise AssertionError("management result did not reach " + status)


def retry_feedback(setup, accepted, token=None):
    from ear_storage.http import create_app
    from fastapi.testclient import TestClient

    dsn, _, cfg, service, _, admin, _ = setup
    body = command("retry_task", target_feedback_id=accepted["feedback_id"])
    with TestClient(create_app(service)) as http:
        response = http.post(
            "/v1/management/requests",
            json=body.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + (token or admin.token)},
        )
    assert response.status_code == 202
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
    return body, response.json()


@pytest.mark.parametrize("failure", ["verify", "create"])
def test_dead_task_approved_retry_preserves_identity_and_no_false_processed(
    setup, monkeypatch, failure
):
    dsn, backend, _, service, operator, admin, processor = setup
    worker, accepted, body, original = pending_worker(setup, monkeypatch, failure)
    before = management_state(dsn)
    assert processor.poll_once(worker.token) == 1
    # Poll itself records terminal exhaustion, without relying on a status read.
    failed = backend.get_system_record(accepted["result_id"])
    assert failed and failed.payload["status"] == "failed"
    assert failed.payload["stage"] == "recovery_required"
    assert not failed.payload["enabled"]
    with connection(dsn) as conn:
        event_row = conn.execute(
            "SELECT event_id FROM storage.management_tasks WHERE feedback_id=%s",
            (accepted["feedback_id"],),
        ).fetchone()
        assert event_row is not None
        event_id = event_row[0]
    assert not backend.is_processed(CONSUMER, event_id)
    assert submit(service.client(admin.token), body) == accepted
    consumers = service.consumers
    assert consumers is not None
    letters = consumers.deadletters(worker.token, ConsumerRequest(consumer=CONSUMER))["deadletters"]
    assert len(letters) == 1
    other = operator.issue_principal("storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK])
    from ear_storage_client.consumer_wire import ReplayCommand

    for foreign in [other, admin]:
        with pytest.raises(PermissionDenied):
            consumers.deadletters(foreign.token, ConsumerRequest(consumer=CONSUMER))
        with pytest.raises(PermissionDenied):
            consumers.replay(
                foreign.token,
                ReplayCommand(consumer=CONSUMER, delivery_id=letters[0]["delivery_id"]),
            )
    with pytest.raises(InvalidRequest, match="management_feedback_required"):
        consumers.replay(
            worker.token, ReplayCommand(consumer=CONSUMER, delivery_id=letters[0]["delivery_id"])
        )
    monkeypatch.setattr(consumers.broker, "call", original)
    retry_body, retry = retry_feedback(setup, accepted)
    approved = await_result(setup, worker, retry, "succeeded")
    assert approved["target_feedback_id"] == accepted["feedback_id"]
    # Lost submit/worker responses cannot create additional recovery deliveries.
    assert submit(service.client(admin.token), retry_body) == retry
    for _ in range(3):
        assert processor.process(retry["feedback_id"], worker_token=worker.token) == approved
    final = await_result(setup, worker, accepted, "succeeded")
    assert final["request_id"] == body.request_id
    assert final["feedback_id"] == accepted["feedback_id"]
    assert final["recovery_feedback_id"] == retry["feedback_id"]
    assert final["enabled"] and backend.is_processed(CONSUMER, event_id)
    after = management_state(dsn)
    assert after[1:3] == before[1:3]  # retry never grants anything
    assert after[3][0][3] == before[3][0][3]  # original durable generation retained
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.consumer_deliveries WHERE replay_of IS NOT NULL"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM storage.module_lifecycle").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM storage.outbox").fetchone() == (3,)
    _, again = retry_feedback(setup, accepted)
    rejected = await_result(setup, worker, again, "failed")
    assert rejected["error_code"] == "task_already_completed"


@pytest.mark.parametrize("crash", ["nack", "ack_flush", "lease", "accepted_lease"])
def test_status_reconciles_worker_crash_window(setup, monkeypatch, crash):
    dsn, backend, _, service, _, admin, processor = setup
    worker, accepted, _, _ = pending_worker(setup, monkeypatch)
    consumers = service.consumers
    assert consumers is not None
    delivery = consumers.fetch(worker.token, ConsumerRequest(consumer=CONSUMER))["delivery"]
    assert delivery and delivery["attempt"] == 2
    if crash == "ack_flush":
        call = consumers.broker.call

        def fail_ack(operation, **kwargs):
            if operation == "ack":
                raise ConsumerUnavailable("lost ack")
            return call(operation, **kwargs)

        monkeypatch.setattr(consumers.broker, "call", fail_ack)
        with pytest.raises(ConsumerUnavailable):
            consumers.finish(
                worker.token,
                DeliveryCommand(consumer=CONSUMER, delivery_token=delivery["delivery_token"]),
                nack=True,
            )
    elif crash == "nack":
        consumers.finish(
            worker.token,
            DeliveryCommand(consumer=CONSUMER, delivery_token=delivery["delivery_token"]),
            nack=True,
        )
    else:
        with connection(dsn) as conn:
            conn.execute("UPDATE storage.consumer_deliveries SET lease_until='-infinity'")
            if crash == "accepted_lease":
                conn.execute(
                    "UPDATE storage.management_tasks SET status='accepted' WHERE feedback_id=%s",
                    (accepted["feedback_id"],),
                )
    # Simulate process death: no processor/poll reconcile after finish/lease.
    stale = backend.get_system_record(accepted["result_id"])
    assert stale and stale.payload["status"] == "pending"
    if crash == "lease":
        from ear_storage_client import Query

        service.client(admin.token).count(Query(RESULT))  # listing/status reconciles too
        current = backend.get_system_record(accepted["result_id"])
        assert current and current.payload["status"] == "failed"
    result = service.client(admin.token).get_system_record(accepted["result_id"])
    assert result and result.payload["status"] == "failed"
    assert result.payload["stage"] == "recovery_required"
    assert result.payload["error_code"] == (
        "delivery_dead" if crash in {"nack", "ack_flush"} else "delivery_lease_exhausted"
    )
    assert service.client(admin.token).get_system_record(accepted["result_id"]) == result
    assert processor.process(accepted["feedback_id"])["status"] == "failed"
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.processed WHERE event_id="
            "(SELECT event_id FROM storage.management_tasks WHERE feedback_id=%s)",
            (accepted["feedback_id"],),
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM storage.outbox").fetchone() == (2,)


@pytest.mark.parametrize(
    "revoked", ["original_before", "retry_before", "retry_after", "original_after"]
)
def test_recovery_checks_both_admins_and_nonadmin_cannot_submit(setup, monkeypatch, revoked):
    dsn, _, _, service, operator, admin, processor = setup
    worker, accepted, _, original = pending_worker(setup, monkeypatch)
    assert processor.poll_once(worker.token) == 1
    assert service.consumers is not None
    monkeypatch.setattr(service.consumers.broker, "call", original)
    plain = operator.issue_principal("api")
    body = command("retry_task", target_feedback_id=accepted["feedback_id"])
    with pytest.raises(PermissionDenied):
        submit(service.client(plain.token), body)
    approver = operator.issue_principal("api", admin=True)
    _, retry = retry_feedback(setup, accepted, approver.token)
    if revoked == "original_before":
        operator.set_admin(admin.principal_id, admin=False)
    if revoked == "retry_before":
        operator.set_admin(approver.principal_id, admin=False)
    if revoked.endswith("before"):
        result = await_result(setup, worker, retry, "failed")
        assert result["error_code"] == (
            "original_authority_revoked" if revoked.startswith("original") else "authority_revoked"
        )
    else:
        await_result(setup, worker, retry, "succeeded")
        operator.set_admin(
            admin.principal_id if revoked.startswith("original") else approver.principal_id,
            admin=False,
        )
        result = await_result(setup, worker, accepted, "failed")
        assert result["error_code"] == "authority_revoked"
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT enabled FROM storage.module_lifecycle WHERE module='sample'"
        ).fetchone() == (False,)


def test_repeated_exhaustion_needs_new_approval_never_new_resources_or_grants(setup, monkeypatch):
    dsn, _, _, service, _, admin, processor = setup
    worker, accepted, _, original = pending_worker(setup, monkeypatch)
    assert processor.poll_once(worker.token) == 1
    before = management_state(dsn)
    retry_body, retry = retry_feedback(setup, accepted)
    await_result(setup, worker, retry, "succeeded")
    assert processor.poll_once(worker.token) == 1  # first replay attempt fails
    with connection(dsn) as conn:
        conn.execute("UPDATE storage.consumer_deliveries SET available_at='-infinity'")
    assert processor.poll_once(worker.token) == 1  # replay itself exhausted
    failed = service.client(admin.token).get_system_record(accepted["result_id"])
    assert failed and failed.payload["stage"] == "recovery_required"
    assert submit(service.client(admin.token), retry_body) == retry
    assert (
        processor.process(retry["feedback_id"], worker_token=worker.token)["status"] == "succeeded"
    )
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.consumer_deliveries WHERE replay_of IS NOT NULL"
        ).fetchone() == (1,)
    assert service.consumers is not None
    monkeypatch.setattr(service.consumers.broker, "call", original)
    _, second = retry_feedback(setup, accepted)
    await_result(setup, worker, second, "succeeded")
    final = await_result(setup, worker, accepted, "succeeded")
    assert final["recovery_feedback_id"] == second["feedback_id"]
    after = management_state(dsn)
    assert after[1:3] == before[1:3]
    assert after[3][0][3] == before[3][0][3]
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.consumer_deliveries WHERE replay_of IS NOT NULL"
        ).fetchone() == (2,)
