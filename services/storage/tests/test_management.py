"""P3 feedback authority/atomicity tests on disposable PostgreSQL, no real inputs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from ear_contracts import DEFAULT_REGISTRY, NewRawRecord, Source, new_id, utcnow
from ear_storage.auth import AuthenticationError, AuthOperator
from ear_storage.backend import PostgresStorage
from ear_storage.http import create_app
from ear_storage.management import CONSUMER, FEEDBACK, RESULT, ManagementProcessor, submit
from ear_storage.management_input import ManagementInput
from ear_storage.schema import bootstrap, connection, migrate
from ear_storage.service import StorageService
from ear_storage_client import AppendRaw, Context, Create, InvalidRequest, PermissionDenied, Query
from fastapi.testclient import TestClient
from pydantic import ValidationError

SYS = Context.system()


def declaration(module="sample", **changes):
    return ManagementInput.model_validate(
        dict(
            request_id=new_id(),
            action="register_plugin",
            module=module,
            types=[{"name": module + "_item", "kind": "semantic"}],
            read_types=[module + "_item", "activity"],
            subscribe_types=["activity"],
            **changes,
        )
    )


def command(action, module="sample", **kwargs):
    return ManagementInput(request_id=new_id(), action=action, module=module, **kwargs)


@pytest.fixture
def mgmt(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    backend = PostgresStorage(pg_dsn)
    service = StorageService(pg_dsn)
    operator = AuthOperator(pg_dsn)
    admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
    processor = ManagementProcessor(backend)
    return pg_dsn, backend, service, operator, admin, processor


def run(mgmt, body):
    _, _, service, _, admin, processor = mgmt
    accepted = submit(service.client(admin.token), body)
    return accepted, processor.process(accepted["feedback_id"])


@pytest.mark.parametrize(
    "mutation",
    [
        {"admin": True},
        {"module": "core", "types": [{"name": "activity", "kind": "semantic"}]},
        {"types": [{"name": "sample_item", "kind": "semantic", "writer": "storage"}]},
        {"types": [{"name": "sample_item", "kind": "semantic", "is_core": True}]},
        {"types": [{"name": "sample_item", "kind": "oops"}]},
        {"types": [{"name": "sample_item", "kind": "raw_record", "requires_dependencies": True}]},
        {"types": [{"name": "sample_item", "kind": "semantic", "supports_reevaluation": True}]},
        {"subscribe_types": ["person"]},
        {"module": "bad; DROP SCHEMA storage"},
        {"action": "rotate_credentials"},
        {"recovery_choice": "skip"},
    ],
)
def test_closed_input(mutation):
    body = declaration().model_dump(mode="json")
    body.update(mutation)
    with pytest.raises(ValidationError):
        ManagementInput.model_validate(body)


@pytest.mark.postgres
def test_accepted_then_atomic_registered_current_database_and_result_only(mgmt):
    dsn, backend, service, _, admin, processor = mgmt
    body = declaration()
    accepted = submit(service.client(admin.token), body)
    assert accepted["status"] == "accepted"
    assert "sample_item" not in backend.registry
    result = service.client(admin.token).get_system_record(accepted["result_id"])
    assert result and result.payload["status"] == "accepted"
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.module_lifecycle").fetchone() == (0,)
    final = processor.process(accepted["feedback_id"])
    assert final["status"] == "succeeded" and final["enabled"] is False
    assert final["online"] is None and final["resources_ready"] is True
    assert "sample_item" in backend.registry and "sample_item" not in DEFAULT_REGISTRY
    assert not backend.registry.is_core("sample_item")
    assert backend.registry.writer_of("sample_item") == "sample"
    assert processor.process(accepted["feedback_id"]) == final
    assert submit(service.client(admin.token), body) == accepted
    events = [e for _, group in backend.outbox() for e in group]
    assert len(events) == 1 and events[0].data_type == FEEDBACK
    assert backend.is_processed(CONSUMER, events[0].event_id)
    assert set(backend.registry.event_publishers) == {"data_changed", "reevaluation_requested"}
    with connection(dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ear_m_sample') IS NOT NULL").fetchone() == (
            True,
        )
        assert conn.execute("SELECT count(*) FROM storage.management_audit").fetchone() == (1,)


@pytest.mark.postgres
def test_admin_must_be_trusted_and_result_reads_need_grants(mgmt):
    _, backend, service, operator, admin, _ = mgmt
    plain = operator.issue_principal("api")
    body = declaration()
    with pytest.raises(PermissionDenied):
        submit(service.client(plain.token), body)
    accepted, _ = run(mgmt, body)
    assert service.client(plain.token).get_system_record(accepted["result_id"]) is None
    with pytest.raises(PermissionDenied):
        service.client(plain.token).query(Query(RESULT))
    changed = body.model_copy(update={"module": "different"})
    with pytest.raises(InvalidRequest):
        submit(service.client(admin.token), changed)
    assert len(backend.outbox()) == 1
    with TestClient(create_app(service)) as http:
        headers = {"Authorization": "Bearer " + admin.token}
        response = http.post(
            "/v1/management/requests", headers=headers, json=body.model_dump(mode="json")
        )
        assert response.status_code == 202 and response.json() == accepted
        payload = body.model_dump(mode="json") | {"admin": True}
        assert (
            http.post("/v1/management/requests", headers=headers, json=payload).status_code == 422
        )


@pytest.mark.postgres
@pytest.mark.parametrize("revoke", ["role", "principal"])
def test_queued_authority_revoked(mgmt, revoke):
    _, backend, service, operator, admin, processor = mgmt
    accepted = submit(service.client(admin.token), declaration())
    if revoke == "role":
        operator.set_admin(admin.principal_id, admin=False)
    else:
        operator.revoke(admin.principal_id)
    result = processor.process(accepted["feedback_id"])
    assert result["status"] == "failed" and result["error_code"] == "authority_revoked"
    assert "sample_item" not in backend.registry
    assert len([e for _, group in backend.outbox() for e in group]) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("alias", ["id", "dedup"])
def test_raw_dedup_cannot_steal_submit_authority(mgmt, alias):
    dsn, backend, service, operator, admin, _ = mgmt
    body = declaration()
    accepted = submit(service.client(admin.token), body)
    old = backend.get(accepted["feedback_id"])
    assert old is not None
    other = operator.issue_principal("api", admin=True)
    now = utcnow()
    record = NewRawRecord(
        id=old.id if alias == "id" else new_id(),
        data_type=FEEDBACK,
        event_start=now,
        event_end=now,
        source=Source(device="synthetic", channel="test", user="default"),
        adapter_version="management-v1",
        dedup_key=admin.principal_id + ":" + body.request_id if alias == "dedup" else "new",
        payload=body.model_dump(mode="json"),
    )
    with pytest.raises(PermissionDenied):
        service.client(other.token).write([AppendRaw(record)], SYS)
    with connection(dsn) as conn:
        assert conn.execute("SELECT principal_id FROM storage.management_authority").fetchall() == [
            (admin.principal_id,)
        ]


@pytest.mark.postgres
def test_register_rollback_unknown_grant_and_namespace_parallel(mgmt):
    dsn, backend, service, _, admin, processor = mgmt
    body = declaration().model_copy(update={"read_types": ("missing",), "subscribe_types": ()})
    _, failed = run(mgmt, body)
    assert failed["status"] == "failed" and "sample_item" not in backend.registry
    with connection(dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ear_m_sample')").fetchone() == (None,)
    requests = [submit(service.client(admin.token), declaration()) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(processor.process, [r["feedback_id"] for r in requests]))
    assert sorted(r["status"] for r in results) == ["failed", "succeeded"]
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.module_lifecycle").fetchone() == (1,)


@pytest.mark.postgres
def test_concurrent_duplicate_request_and_processor(mgmt):
    dsn, _, service, _, admin, processor = mgmt
    body = declaration()
    with ThreadPoolExecutor(3) as pool:
        receipts = list(pool.map(lambda _: submit(service.client(admin.token), body), range(3)))
    assert receipts[0] == receipts[1] == receipts[2]
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(processor.process, [r["feedback_id"] for r in receipts]))
    assert results[0] == results[1] == results[2]
    with connection(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.management_tasks").fetchone() == (1,)


@pytest.mark.postgres
def test_disabled_identity_core_unaffected_and_register_completion(mgmt):
    _, backend, service, operator, _, _ = mgmt
    body = declaration().model_copy(
        update={
            "types": ManagementInput.model_validate(
                {
                    **declaration().model_dump(mode="json"),
                    "types": [
                        {
                            "name": "sample_item",
                            "kind": "semantic",
                            "requires_dependencies": True,
                            "supports_reevaluation": True,
                        },
                        {"name": "sample_completion", "kind": "system_record"},
                    ],
                }
            ).types
        }
    )
    run(mgmt, body)
    plugin = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    with pytest.raises(AuthenticationError):
        service.client(plugin.token).write([Create("sample_item", new_id(), {})], SYS)
    assert backend.registry.writer_of("sample_completion") == "sample"
    core = operator.issue_principal("extraction")
    service.client(core.token).write([Create("activity", new_id(), {})], SYS).check()
    _, result = run(mgmt, command("enable"))
    assert result["enabled"] is False
    assert result["error_code"] == "broker_not_configured"


@pytest.mark.postgres
@pytest.mark.parametrize("module", ["core", "storage", "api", "extraction", "llm_gateway"])
def test_cannot_register_reserved_module(mgmt, module):
    _, backend, _, _, _, _ = mgmt
    before = backend.registry
    _, result = run(mgmt, declaration(module))
    assert result["status"] == "failed" and result["error_code"] == "namespace_unavailable"
    assert backend.registry == before


@pytest.mark.postgres
def test_result_write_failure_rolls_back_ddl_audit_and_mark(mgmt, monkeypatch):
    dsn, backend, service, _, admin, processor = mgmt
    accepted = submit(service.client(admin.token), declaration())
    original = processor._record

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic transaction abort")

    monkeypatch.setattr(processor, "_record", fail)
    with pytest.raises(RuntimeError):
        processor.process(accepted["feedback_id"])
    assert "sample_item" not in backend.registry
    with connection(dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ear_m_sample')").fetchone() == (None,)
        assert conn.execute("SELECT count(*) FROM storage.management_audit").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM storage.processed").fetchone() == (0,)
        assert conn.execute("SELECT status FROM storage.management_tasks").fetchone() == (
            "accepted",
        )
    monkeypatch.setattr(processor, "_record", original)
    assert processor.process(accepted["feedback_id"])["status"] == "succeeded"


def test_retry_operation_is_closed_and_old_payload_compatible():
    original = declaration()
    old = original.model_dump(mode="json")
    old.pop("target_feedback_id")
    # Reconstruct the legacy declaration before the additive completion capability.
    for item in old["types"]:
        item.pop("reevaluation_result")
    assert original.payload() == old
    assert ManagementInput.model_validate(old).payload() == old
    assert old["types"][0]["time_from"] is None
    target = new_id()
    retry = command("retry_task", target_feedback_id=target)
    assert retry.payload()["target_feedback_id"] == target
    for body in [
        {**old, "target_feedback_id": target},
        {**retry.payload(), "target_feedback_id": None},
        {**retry.payload(), "read_types": ["activity"]},
        {**retry.payload(), "consumer": "another.worker"},
        {**retry.payload(), "admin": True},
    ]:
        with pytest.raises(ValidationError):
            ManagementInput.model_validate(body)
