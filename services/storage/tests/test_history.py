"""P5 authorization, exclusions and finite history planning. Synthetic data only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from ear_contracts import Cause, ReevaluationResult, VersionRef, new_id, utcnow
from ear_storage import schema
from ear_storage.consumer_broker import ConsumerUnavailable
from ear_storage.history import job_status
from ear_storage.http import create_app
from ear_storage.management import submit
from ear_storage.management_input import ManagementInput
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import connection, one
from ear_storage_client import (
    Context,
    Create,
    FinishReevaluation,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.consumer_wire import ConsumerRequest, DeliveryCommand, ReplayCommand
from ear_storage_client.remote_errors import DeliveryExpired
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .test_management import command, declaration
from .test_management_integration import enable, execute
from .test_management_integration import setup as setup
from .test_reconfiguration import drain
from .test_relay import env as env

SYS = Context.system()


def test_old_payload_fingerprint_and_explicit_history_fields():
    assert command("enable").payload().keys() == {
        "request_id",
        "action",
        "module",
        "types",
        "read_types",
        "subscribe_types",
    }
    old = declaration().payload()
    assert ManagementInput.model_validate(old).payload() == old
    for changes in [
        {},
        {"rangeStart": "2026-01-01T00:00:00Z"},
        {"rangeStart": "2026-01-02T00:00:00Z", "rangeEnd": "2026-01-01T00:00:00Z"},
        {"rangeStart": "2026-01-01T00:00:00+00:00", "rangeEnd": "2026-01-02T00:00:00Z"},
    ]:
        with pytest.raises(ValidationError):
            command("history_backfill", input_types=("activity",), **changes)
    for changes in [
        {"resume_choice": "skip"},
        {"expected_revision": 1},
        {"resume_choice": "skip", "expected_revision": True},
    ]:
        with pytest.raises(ValidationError):
            command("enable", **changes)


def relay(setup):
    dsn, _, cfg, *_ = setup
    asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))


def recover(setup, choice, **extra):
    _, state = execute(setup, command("status"))
    return execute(
        setup, command("enable", resume_choice=choice, expected_revision=state["revision"], **extra)
    )


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("old_state", ["inflight", "retry", "dead", "unfetched", "unrelayed"])
def test_skip_fences_old_batch_preserves_business_and_delivers_new(setup, old_state):
    dsn, backend, _, service, _, _, processor = setup
    plugin = enable(setup, dependencies=True)
    client = service.client(plugin.token)
    consumers = service.consumers
    assert consumers is not None
    source, target = new_id(), new_id()
    event = (
        backend.client("extraction").write([Create("activity", source, {})], SYS).check().events[0]
    )
    client.write(
        [
            Create("sample_item", target, {"old": True}),
            RegisterDependencies(
                "sample_item", target, [VersionRef(data_type="activity", id=source, version=1)]
            ),
        ],
        SYS,
    ).check()
    before = (backend.get(target), backend.dependency_set(target))
    delivery = None
    if old_state != "unrelayed":
        relay(setup)
    if old_state in {"inflight", "retry", "dead"}:
        delivery = consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))[
            "delivery"
        ]
        assert delivery
        if old_state in {"retry", "dead"}:
            with connection(dsn) as conn:
                conn.execute(
                    "UPDATE storage.consumer_deliveries SET state=%s WHERE delivery_id=%s",
                    (old_state, delivery["delivery_id"]),
                )
    execute(setup, command("disable"))
    accepted, final = recover(setup, "skip")
    assert (
        final["status"] == "succeeded" and final["enabled"] and not final["need_recovery_decision"]
    )
    assert processor.process(accepted["feedback_id"]) == final
    if delivery:
        with pytest.raises(DeliveryExpired):
            consumers.finish(
                plugin.token,
                DeliveryCommand(
                    consumer="sample.managed", delivery_token=delivery["delivery_token"]
                ),
                nack=False,
            )
        with pytest.raises(PermissionDenied):
            consumers.replay(
                plugin.token,
                ReplayCommand(consumer="sample.managed", delivery_id=delivery["delivery_id"]),
            )
    with pytest.raises(PermissionDenied, match="event_excluded"):
        client.write(
            [
                Update("sample_item", target, 1, {"old": False}),
                RegisterDependencies("sample_item", target, []),
                MarkProcessed("sample.managed", event.event_id),
            ],
            SYS,
        )
    assert (backend.get(target), backend.dependency_set(target)) == before
    assert not backend.is_processed("sample.managed", event.event_id)
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 0
    new = new_id()
    backend.client("extraction").write([Create("activity", new, {})], SYS).check()
    relay(setup)
    assert drain(consumers, plugin.token, "sample.managed") == [new]
    # Event-free direct writes retain normal permissions, not a sandbox claim.
    client.write(
        [
            Update("sample_item", target, 1, {"direct": True}),
            RegisterDependencies("sample_item", target, []),
        ],
        SYS,
    ).check()


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("limit", [None, "items", "bytes"])
def test_catchup_bounded_deduplicated_current_plan(setup, monkeypatch, limit):
    dsn, backend, _, service, _, admin, processor = setup
    enable(setup)
    first, terminal = new_id(), new_id()
    backend.client("extraction").write(
        [Create("activity", first, {}), Create("activity", terminal, {})], SYS
    ).check()
    # Both precede disable and have never been fetched or relayed.
    execute(setup, command("disable"))
    backend.client("extraction").write(
        [Update("activity", first, 1, {"v": 2}), Supersede("activity", terminal, 1)], SYS
    ).check()
    if limit:
        monkeypatch.setattr(
            "ear_storage.history.MAX_ITEMS"
            if limit == "items"
            else "ear_storage.history.MAX_BYTES",
            1,
        )
    accepted, result = recover(setup, "catch_up")
    if limit:
        assert result["error_code"] == "history_input_limit"
        with connection(dsn) as conn:
            assert one(conn, "SELECT count(*) FROM storage.history_jobs")[0] == 0
            assert one(conn, "SELECT count(*) FROM storage.recovery_decisions")[0] == 0
        return
    assert result["status"] == "succeeded" and result["enabled"]
    job = result["history_job_id"]
    assert job_status(service.client(admin.token), job)["state"] == "planned"
    assert processor.process(accepted["feedback_id"])["history_job_id"] == job
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_jobs")[0] == 1
        assert conn.execute(
            "SELECT object_id,item_kind,state FROM storage.history_items ORDER BY object_id"
        ).fetchall() == sorted(
            [(first, "current_state", "candidate"), (terminal, "current_state", "unavailable")]
        )
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 0


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("failure", ["verify", "create"])
def test_skip_partial_resources_revision_and_idempotence(setup, monkeypatch, failure):
    dsn, backend, _, service, _, _, processor = setup
    plugin = enable(setup)
    assert service.consumers is not None
    execute(setup, command("disable"))
    old = new_id()
    backend.client("extraction").write([Create("activity", old, {})], SYS).check()
    original = service.consumers.broker.call

    def fail(operation, **kwargs):
        if operation == failure:
            raise ConsumerUnavailable("synthetic")
        return original(operation, **kwargs)

    monkeypatch.setattr(service.consumers.broker, "call", fail)
    accepted, pending = recover(setup, "skip")
    assert pending["status"] == "pending" and not pending["enabled"]
    future = new_id()
    backend.client("extraction").write([Create("activity", future, {})], SYS).check()
    monkeypatch.setattr(service.consumers.broker, "call", original)
    final = processor.process(accepted["feedback_id"])
    assert final["status"] == "succeeded" and final["enabled"]
    relay(setup)
    assert drain(service.consumers, plugin.token, "sample.managed") == [future]
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.recovery_decisions")[0] == 1
    _, disabled = execute(setup, command("disable"))
    _, rejected = execute(
        setup, command("enable", resume_choice="skip", expected_revision=disabled["revision"] - 1)
    )
    assert rejected["error_code"] == "stale_recovery_revision"


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("choice", ["skip", "catch_up"])
def test_permission_restoration_failure_retry_and_selected_types(setup, monkeypatch, choice):
    _dsn, backend, _, service, _, _, processor = setup
    plugin = enable(setup)
    assert service.consumers is not None
    original = service.consumers.broker.call

    def fail(operation, **kwargs):
        if operation == "create":
            raise ConsumerUnavailable("synthetic")
        return original(operation, **kwargs)

    execute(setup, command("set_permissions", read_types=("sample_item",), subscribe_types=()))
    old = new_id()
    backend.client("extraction").write([Create("activity", old, {})], SYS).check()
    _, state = execute(setup, command("status"))
    monkeypatch.setattr(service.consumers.broker, "call", fail)
    body = command(
        "set_permissions",
        read_types=("sample_item", "activity", "person"),
        subscribe_types=("activity", "person"),
        resume_choice=choice,
        expected_revision=state["revision"],
    )
    accepted, pending = execute(setup, body)
    assert pending["status"] == "pending" and pending["need_recovery_decision"]
    future, old_newtype = new_id(), new_id()
    backend.client("extraction").write([Create("activity", future, {})], SYS).check()
    backend.client("attribution").write([Create("person", old_newtype, {})], SYS).check()
    monkeypatch.setattr(service.consumers.broker, "call", original)
    final = processor.process(accepted["feedback_id"])
    if choice == "catch_up":
        assert (
            final["status"] == "succeeded" and final["history_job_id"] == pending["history_job_id"]
        )
        with pytest.raises(PermissionDenied):
            service.consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
        return
    assert final["status"] == "succeeded" and not final["need_recovery_decision"]
    newtype = new_id()
    backend.client("attribution").write([Create("person", newtype, {})], SYS).check()
    relay(setup)
    assert drain(service.consumers, plugin.token, "sample.managed") == [future, newtype]


@pytest.mark.postgres
@pytest.mark.nats
def test_history_explicit_range_authority_and_job_is_not_a_result(setup):
    dsn, backend, _, service, operator, admin, _ = setup
    plugin = enable(setup)
    day = datetime(2026, 1, 1, tzinfo=UTC)
    inside, at_end, terminal = new_id(), new_id(), new_id()
    backend.client("extraction").write(
        [
            Create("activity", inside, {}, event_start=day, event_end=day),
            Create(
                "activity",
                at_end,
                {},
                event_start=day + timedelta(days=1),
                event_end=day + timedelta(days=1),
            ),
            Create("activity", terminal, {}, event_start=day, event_end=day + timedelta(hours=1)),
        ],
        SYS,
    ).check()
    backend.client("extraction").write([Supersede("activity", terminal, 1)], SYS).check()
    client = service.client(plugin.token)
    assert client.history(inside)
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_jobs")[0] == 0
    body = command(
        "history_backfill",
        input_types=("activity",),
        rangeStart=day,
        rangeEnd=day + timedelta(days=1),
    )
    plain = operator.issue_principal("api")
    for token in (plain.token, plugin.token):
        with pytest.raises(PermissionDenied):
            submit(service.client(token), body)
    accepted, result = execute(setup, body)
    assert result["status"] == "succeeded" and result["history_execution_status"] == "planned"
    job = result["history_job_id"]
    status = job_status(client, job)
    assert status["owner"] == "sample" and status["state"] == "planned"
    assert status["items"] == {"candidate": 1, "unavailable": 1} and status["execution_available"]
    other = operator.issue_principal("extraction", read_types=["activity"])
    with TestClient(create_app(service)) as http:
        for token, code in [
            (admin.token, 200),
            (plugin.token, 200),
            (plain.token, 403),
            (other.token, 403),
        ]:
            response = http.get(
                "/v1/history/jobs/" + job, headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == code, response.text
    assert client.get_system_record(job) is None
    with connection(dsn) as conn:
        assert (
            one(conn, "SELECT feedback_id FROM storage.history_jobs")[0] == accepted["feedback_id"]
        )
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 0


@pytest.mark.postgres
@pytest.mark.nats
def test_first_enable_filters_delayed_old_relay(setup):
    _, backend, _, service, operator, _, _ = setup
    body = declaration()
    execute(setup, body)
    old, new = new_id(), new_id()
    backend.client("extraction").write([Create("activity", old, {})], SYS).check()
    plugin = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    _, result = execute(setup, command("enable"))
    assert result["enabled"]
    backend.client("extraction").write([Create("activity", new, {})], SYS).check()
    relay(setup)
    assert service.consumers is not None
    assert drain(service.consumers, plugin.token, "sample.managed") == [new]


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("choice", ["catch_up", "skip", "completed"])
def test_request_enqueue_ack_is_not_completion_and_context_submission_is_fenced(setup, choice):
    dsn, backend, _, service, operator, _, _ = setup
    body = ManagementInput.model_validate(
        dict(
            declaration().payload(),
            types=[
                {"name": "sample_item", "kind": "semantic", "supports_reevaluation": True},
                {"name": "sample_completion", "kind": "system_record"},
            ],
            read_types=["sample_item", "sample_completion", "activity"],
            subscribe_types=["sample_item"],
        )
    )
    execute(setup, body)
    plugin = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    execute(setup, command("enable"))
    source, target = new_id(), new_id()
    backend.client("extraction").write([Create("activity", source, {})], SYS).check()
    client = service.client(plugin.token)
    client.write([Create("sample_item", target, {})], SYS).check()
    request = new_id()
    backend.client("dependency").write(
        [
            PublishReevaluation(
                "sample_item",
                target,
                Cause(data_type="activity", data_id=source, old_version=None, new_version=1),
                "synthetic",
                request,
            )
        ],
        SYS.deeper(),
    ).check()
    relay(setup)
    if choice == "completed":
        result = ReevaluationResult(
            request_event_id=request,
            target_type="sample_item",
            target_id=target,
            outcome="unchanged",
            before_version=1,
            after_version=1,
            root_feedback_id="system",
            depth=1,
            completed_at=utcnow(),
        )
        client.write(
            [
                FinishReevaluation(result),
                RegisterDependencies("sample_item", target, []),
                MarkProcessed("sample.managed", request),
            ],
            SYS.deeper(),
        ).check()
    else:
        client.write([MarkProcessed("sample.managed", request)], SYS).check()
    # Simulate transport acknowledgement only, as distinct from business Finish.
    assert service.consumers is not None
    for _ in range(5):
        delivery = service.consumers.fetch(
            plugin.token, ConsumerRequest(consumer="sample.managed")
        )["delivery"]
        if delivery:
            service.consumers.finish(
                plugin.token,
                DeliveryCommand(
                    consumer="sample.managed", delivery_token=delivery["delivery_token"]
                ),
                nack=False,
            )
    execute(setup, command("disable"))
    _, result = recover(setup, "catch_up" if choice == "completed" else choice)
    if choice == "completed":
        assert result["status"] == "succeeded"
        with connection(dsn) as conn:
            assert one(conn, "SELECT count(*) FROM storage.history_items")[0] == 0
        return
    if choice == "catch_up":
        assert result["status"] == "succeeded"
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT object_id,item_kind FROM storage.history_items"
            ).fetchall() == [(target, "reevaluation_needed")]
            assert one(conn, "SELECT request_id FROM storage.history_item_requests")[0] == request
        from ear_storage.history_execution import claim

        assert job_status(client, result["history_job_id"])["requires_scheduler"]
        blocked = claim(client, result["history_job_id"])
        assert blocked["status"] == "blocked"
        assert blocked["error_code"] == "scheduler_unavailable"
    else:
        assert result["enabled"]
        with pytest.raises(PermissionDenied):
            client.write(
                [Update("sample_item", target, 1, {"oldworker": True})],
                Context("system", 1, request_event_id=request),
            )
    assert backend.get(target).version == 1
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 0


@pytest.mark.postgres
@pytest.mark.parametrize("revision", [10, 11, 12, 13])
def test_explicit_upgrade_preserves_business_outbox_and_idempotence(pg_dsn, monkeypatch, revision):
    from ear_storage.backend import PostgresStorage

    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", revision)
        schema.migrate(pg_dsn)
        schema.bootstrap(pg_dsn)
        backend = PostgresStorage(pg_dsn)
        rid = new_id()
        backend.client("extraction").write([Create("activity", rid, {"old": True})], SYS).check()
        with connection(pg_dsn) as conn:
            old = conn.execute("SELECT event_id,event_text FROM storage.outbox").fetchall()
    schema.migrate(pg_dsn)
    schema.migrate(pg_dsn)
    with connection(pg_dsn) as conn:
        assert one(conn, "SELECT schema_revision FROM storage.meta")[0] == schema.SCHEMA_VERSION
        assert conn.execute("SELECT event_id,event_text FROM storage.outbox").fetchall() == old
    record = PostgresStorage(pg_dsn).get(rid)
    assert record is not None and record.payload == {"old": True}


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("choice", ["skip", "catch_up"])
def test_readonly_origin_recovery_preserves_unrelated_target_cursor(setup, choice):
    dsn, backend, _, service, operator, _, _ = setup
    body = ManagementInput.model_validate(
        dict(
            declaration().payload(),
            types=[
                {"name": "sample_item", "kind": "semantic", "supports_reevaluation": True},
                {"name": "sample_completion", "kind": "system_record"},
            ],
            read_types=["sample_item", "activity"],
            subscribe_types=["sample_item"],
        )
    )
    execute(setup, body)
    plugin = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    execute(setup, command("enable"))
    source, target = new_id(), new_id()
    backend.client("extraction").write([Create("activity", source, {})], SYS).check()
    service.client(plugin.token).write([Create("sample_item", target, {})], SYS).check()
    request = new_id()
    backend.client("dependency").write(
        [
            PublishReevaluation(
                "sample_item",
                target,
                Cause(data_type="activity", data_id=source, old_version=None, new_version=1),
                "synthetic",
                request,
            )
        ],
        SYS.deeper(),
    ).check()
    _, state = execute(
        setup,
        command("set_permissions", read_types=("sample_item",), subscribe_types=("sample_item",)),
    )
    _, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity"),
            subscribe_types=("sample_item",),
            resume_choice=choice,
            expected_revision=state["revision"],
        ),
    )
    with connection(dsn) as conn:
        if choice == "catch_up":
            assert result["status"] == "succeeded"
            assert one(conn, "SELECT request_id FROM storage.history_item_requests")[0] == request
            return
        assert result["status"] == "succeeded" and not result["need_recovery_decision"]
        assert one(conn, "SELECT event_id FROM storage.recovery_excluded_events")[0] == request
    with pytest.raises(PermissionDenied):
        service.client(plugin.token).write(
            [Update("sample_item", target, 1, {"stale": True})],
            Context("system", 1, request_event_id=request),
        )
    # Ordinary older input with the same target type is NOT skipped by origin recovery.
    relay(setup)
    assert service.consumers is not None
    assert drain(service.consumers, plugin.token, "sample.managed") == [target]
