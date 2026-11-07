"""Real loopback HTTP + PostgreSQL + NATS credential/consumer continuity."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from ear_contracts import VersionRef, new_id
from ear_storage.management import FEEDBACK, RESULT, submit
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage_client import Context, Create, PermissionDenied, Query, RegisterDependencies
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError

from .test_credentials import headers
from .test_http_postgres import live_server
from .test_management import command, declaration
from .test_management_integration import env as env
from .test_management_integration import execute
from .test_management_integration import setup as setup

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()


def request(http, admin, body):
    response = http.post(
        "/v1/management/requests", headers=headers(admin.token), json=body.payload()
    )
    assert response.status_code == 202
    return response.json()


def claim(http, admin, accepted):
    response = http.post(
        "/v1/management/credentials/" + accepted["feedback_id"] + "/claim",
        headers=headers(admin.token),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def consumer_state(dsn):
    with connection(dsn) as conn:
        return conn.execute(
            "SELECT name,principal_id,generation,config_text,creation_cursor "
            "FROM storage.consumers WHERE module='sample'"
        ).fetchall()


def test_live_http_rotation_receipts_snapshots_inflight_ack_and_revoke(setup, caplog):
    dsn, backend, cfg, service, operator, admin, processor = setup
    with live_server(service) as url, httpx.Client(base_url=url, trust_env=False) as http:
        registered = request(http, admin, declaration())
        assert processor.process(registered["feedback_id"])["status"] == "succeeded"
        issued_request = request(http, admin, command("rotate_credentials"))
        assert processor.process(issued_request["feedback_id"])["status"] == "succeeded"
        issued = claim(http, admin, issued_request)
        with RemoteStorageClient(issued["token"], url) as old:
            with pytest.raises(AuthenticationError):
                _ = old.module
            enabled = request(http, admin, command("enable"))
            assert processor.process(enabled["feedback_id"])["enabled"]
            assert old.module == "sample"
            aid, rid, req = new_id(), new_id(), new_id()
            backend.client("extraction").write([Create("activity", aid, {})], SYS).check()
            ops = [
                Create("sample_item", rid, {}),
                RegisterDependencies(
                    "sample_item", rid, [VersionRef(data_type="activity", id=aid, version=1)]
                ),
            ]
            receipt = old.write(ops, SYS, request_id=req).check()
            lease = old.create_snapshot()
            asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
            delivery = old._consumer("fetch", "sample.managed")["delivery"]
            assert delivery
            before = consumer_state(dsn)
            rotate = request(http, admin, command("rotate_credentials"))
            result = processor.process(rotate["feedback_id"])
            assert result["status"] == "succeeded"
            # Invalidation is at the rotate-stage commit, not delayed until claim.
            for operation in [
                lambda: old.get(rid),
                lambda: old.write(ops, SYS, request_id=req),
                lambda: old._consumer("fetch", "sample.managed"),
                lambda: old.read_snapshot(lease["snapshot_id"]).get(rid),
                lambda: old._consumer(
                    "ack", "sample.managed", delivery_token=delivery["delivery_token"]
                ),
            ]:
                with pytest.raises(AuthenticationError):
                    operation()
            current = claim(http, admin, rotate)
            assert current["principal_id"] == issued["principal_id"]
            assert consumer_state(dsn) == before
            with RemoteStorageClient(current["token"], url) as new:
                assert new.read_snapshot(lease["snapshot_id"]).get(rid)
                assert new.write(ops, SYS, request_id=req) == receipt
                ack = new._consumer(
                    "ack", "sample.managed", delivery_token=delivery["delivery_token"]
                )
                assert ack["status"] == "acked"
                with connection(dsn) as conn:
                    assert conn.execute(
                        "SELECT scope FROM storage.idempotency WHERE request_id=%s", (req,)
                    ).fetchone() == ("principal:" + issued["principal_id"],)
                # Effective grant withdrawal still gates old receipts and snapshots.
                operator.set_grants(current["principal_id"], read_types=["sample_item"])
                with pytest.raises(PermissionDenied):
                    new.write(ops, SYS, request_id=req)
                with pytest.raises(PermissionDenied):
                    new.read_snapshot(lease["snapshot_id"]).get(aid)
                revoke = request(http, admin, command("revoke_credentials"))
                assert processor.process(revoke["feedback_id"])["status"] == "succeeded"
                with pytest.raises(AuthenticationError):
                    new.get(rid)
                with pytest.raises(AuthenticationError):
                    new._consumer("fetch", "sample.managed")
            assert len(backend.history(rid)) == 1
            assert consumer_state(dsn) == before
            recovery = request(http, admin, command("rotate_credentials"))
            processor.process(recovery["feedback_id"])
            recovered = claim(http, admin, recovery)
            assert recovered["principal_id"] == issued["principal_id"]
            with RemoteStorageClient(recovered["token"], url) as new:
                assert new.get(rid)
                assert new.get(aid) is None
                with pytest.raises(PermissionDenied):
                    new.write(ops, SYS, request_id=req)
        exposed = (
            repr(backend.get_system_record(rotate["result_id"]))
            + repr(backend.outbox())
            + caplog.text
        )
        for material in (issued["token"], current["token"], recovered["token"]):
            assert material not in exposed


def test_worker_feedback_issue_and_core_worker_rotate_keep_original_consumer(setup):
    dsn, backend, cfg, service, operator, admin, processor = setup
    worker = operator.issue_principal("storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK])
    # Worker, not the HTTP endpoint, executes the submitted feedback stages.
    with live_server(service) as url, httpx.Client(base_url=url, trust_env=False) as http:
        registered = request(http, admin, declaration())
        asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
        assert processor.poll_once(worker.token) == 1
        assert backend.get_system_record(registered["result_id"])
        accepted = request(http, admin, command("rotate_credentials"))
        asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
        assert processor.poll_once(worker.token) == 1
        result = service.client(admin.token).get_system_record(accepted["result_id"])
        assert result and result.payload["status"] == "succeeded"
        with connection(dsn) as conn:
            before = conn.execute(
                "SELECT name,principal_id,generation FROM storage.consumers WHERE module='storage'"
            ).fetchall()
            assert conn.execute(
                "SELECT token_hash FROM storage.principals WHERE module='sample'"
            ).fetchone() == (None,)
        issued = claim(http, admin, accepted)
        rotated = operator.rotate(worker.principal_id)
        assert rotated.principal_id == worker.principal_id
        status = submit(service.client(admin.token), command("status"))
        asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
        from ear_storage.auth import AuthenticationError as ServerAuthenticationError

        with pytest.raises(ServerAuthenticationError):
            processor.poll_once(worker.token)
        assert processor.poll_once(rotated.token) == 1
        result = service.client(admin.token).get_system_record(status["result_id"])
        assert result and result.payload["status"] == "succeeded"
        with connection(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT name,principal_id,generation FROM storage.consumers "
                    "WHERE module='storage'"
                ).fetchall()
                == before
            )
        assert set(backend.registry.event_publishers) == {"data_changed", "reevaluation_requested"}
        assert issued["token"] not in repr(result)
        # Revocation fences unclaimed authorization even after another rotate.
        pending, _ = execute(setup, command("rotate_credentials"))
        execute(setup, command("revoke_credentials"))
        assert (
            http.post(
                "/v1/management/credentials/" + pending["feedback_id"] + "/claim",
                headers=headers(admin.token),
            ).status_code
            == 403
        )
        with pytest.raises(ServerAuthenticationError):
            service.client(issued["token"]).query(Query(RESULT))


def managed_identity(setup):
    """Only normal feedback + claim creates and restores the plugin identity."""
    from ear_storage.credentials import claim_credentials

    _, _, _, service, _, admin, _ = setup
    _, registered = execute(
        setup,
        declaration().model_copy(
            update={
                "read_types": ("sample_item", "activity", "person"),
                "subscribe_types": ("activity", "person"),
            }
        ),
    )
    assert registered["status"] == "succeeded"
    accepted, rotated = execute(setup, command("rotate_credentials"))
    assert rotated["status"] == "succeeded"
    plugin = claim_credentials(service.client(admin.token), accepted["feedback_id"])
    _, enabled = execute(setup, command("enable"))
    assert enabled["status"] == "succeeded" and enabled["enabled"]
    return plugin


def principal_state(dsn):
    with connection(dsn) as conn:
        return conn.execute(
            "SELECT id,active,credential_epoch,token_hash FROM storage.principals "
            "WHERE module='sample' ORDER BY id"
        ).fetchall()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_revoked_shrink_provisions_then_explicit_claim_keeps_only_a(
    setup, monkeypatch, failure, disabled
):
    from ear_storage.auth import AuthenticationError as ServerAuthenticationError
    from ear_storage.consumer_broker import ConsumerUnavailable
    from ear_storage.credentials import claim_credentials
    from ear_storage_client.consumer_wire import ConsumerRequest

    dsn, backend, cfg, service, _, admin, processor = setup
    plugin = managed_identity(setup)
    if disabled:
        assert execute(setup, command("disable"))[1]["status"] == "succeeded"
    pending, staged = execute(setup, command("rotate_credentials"))
    assert staged["status"] == "succeeded"
    assert execute(setup, command("revoke_credentials"))[1]["status"] == "succeeded"
    revoked = principal_state(dsn)
    assert revoked[0][1] is False
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call

    def unavailable(operation, **kwargs):
        result = original(operation, **kwargs)
        if operation == "create":
            raise ConsumerUnavailable("synthetic lost create response")
        return result

    if failure:
        monkeypatch.setattr(consumers.broker, "call", unavailable)
    accepted, result = execute(
        setup,
        command(
            "set_permissions", read_types=("sample_item", "activity"), subscribe_types=("activity",)
        ),
    )
    if failure:
        assert result["status"] == "pending" and result["error_code"] == "broker_unavailable"
        assert not result["resources_ready"]
        assert principal_state(dsn) == revoked
        monkeypatch.setattr(consumers.broker, "call", original)
        result = processor.process(accepted["feedback_id"])
    assert result["status"] == "succeeded", result
    assert result["resources_ready"] and result["enabled"] is (not disabled)
    assert principal_state(dsn) == revoked
    with pytest.raises(ServerAuthenticationError):
        service.client(plugin.token).query(Query("activity"))
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), pending["feedback_id"])
    rotate = submit(service.client(admin.token), command("rotate_credentials"))
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), rotate["feedback_id"])
    assert processor.process(rotate["feedback_id"])["status"] == "succeeded"
    assert principal_state(dsn)[0][0:2] == (plugin.principal_id, False)
    current = claim_credentials(service.client(admin.token), rotate["feedback_id"])
    assert current.principal_id == plugin.principal_id
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), pending["feedback_id"])
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT data_type,can_subscribe FROM storage.type_grants "
            "WHERE principal_id=%s ORDER BY 1",
            (current.principal_id,),
        ).fetchall() == [("activity", True), ("sample_item", False)]
    if disabled:
        with pytest.raises(ServerAuthenticationError):
            service.client(current.token).query(Query("activity"))
        with pytest.raises(ServerAuthenticationError):
            consumers.fetch(current.token, ConsumerRequest(consumer="sample.managed"))
    else:
        a, b = new_id(), new_id()
        backend.client("extraction").write([Create("activity", a, {})], SYS).check()
        backend.client("attribution").write([Create("person", b, {})], SYS).check()
        assert service.client(current.token).get(a)
        assert service.client(current.token).get(b) is None
        asyncio.run(run_relay(RelayStore(dsn, cfg), once=True))
        # Credential recovery must not bypass the independent history decision.
        with pytest.raises(PermissionDenied, match="need_recovery_decision"):
            consumers.fetch(current.token, ConsumerRequest(consumer="sample.managed"))
        import json

        filters = json.loads(consumer_state(dsn)[0][3])["filters"]
        assert any(".activity." in item for item in filters)
        assert all(".person." not in item for item in filters)


@pytest.mark.parametrize("initial_inactive", [False, True])
@pytest.mark.parametrize("interruption", ["revoke_credentials", "rotate_credentials"])
def test_reconfiguration_fences_credential_epoch_during_broker_io(
    setup, monkeypatch, initial_inactive, interruption
):
    from concurrent.futures import ThreadPoolExecutor

    from ear_storage.credentials import claim_credentials

    dsn, _, _, service, _, admin, _ = setup
    plugin = managed_identity(setup)
    old, staged = execute(setup, command("rotate_credentials"))
    assert staged["status"] == "succeeded"
    if not initial_inactive:
        plugin = claim_credentials(service.client(admin.token), old["feedback_id"])
    before = principal_state(dsn)
    consumers = service.consumers
    assert consumers is not None
    original = consumers.broker.call
    interrupted = False

    def interrupt(operation, **kwargs):
        nonlocal interrupted
        result = original(operation, **kwargs)
        if operation == "create" and not interrupted:
            interrupted = True
            with ThreadPoolExecutor(1) as pool:
                _, changed = pool.submit(execute, setup, command(interruption)).result(3)
            assert changed["status"] == "succeeded", changed
        return result

    monkeypatch.setattr(consumers.broker, "call", interrupt)
    _, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person", "place"),
            subscribe_types=("activity", "person", "place"),
        ),
    )
    assert interrupted
    assert result["status"] == "failed", result
    assert result["error_code"] == "resource_configuration_changed"
    after = principal_state(dsn)
    assert after[0][0:2] == (plugin.principal_id, False)
    assert after[0][2] == before[0][2] + 1
    with connection(dsn) as conn:
        assert not conn.execute(
            "SELECT 1 FROM storage.type_grants WHERE data_type='place'"
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM storage.module_permissions WHERE data_type='place'"
        ).fetchone()
        assert conn.execute("SELECT state FROM storage.consumer_reconfigurations").fetchall() == [
            ("cancelled",)
        ]
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), old["feedback_id"])


def test_inactive_legacy_principals_prepare_without_enabling_or_choosing_identity(setup):
    from ear_storage.credentials import claim_credentials

    dsn, _, _, service, _, admin, _ = setup
    plugin = managed_identity(setup)
    other = new_id()
    # Model a legacy ambiguous owner, not a deployment recovery shortcut.
    with connection(dsn) as conn:
        conn.execute(
            "INSERT INTO storage.principals(id,module,active) VALUES (%s,'sample',false)",
            (other,),
        )
    execute(setup, command("revoke_credentials"))
    before = principal_state(dsn)
    _, result = execute(
        setup,
        command(
            "set_permissions", read_types=("sample_item", "activity"), subscribe_types=("activity",)
        ),
    )
    assert result["status"] == "succeeded", result
    assert principal_state(dsn) == before
    rotated, result = execute(setup, command("rotate_credentials"))
    assert result["status"] == "failed"
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), rotated["feedback_id"])
    assert {row[0] for row in principal_state(dsn)} == {other, plugin.principal_id}


def test_pending_claim_rejects_principal_added_during_rotation(setup):
    from ear_storage.credentials import claim_credentials

    dsn, _, _, service, _, admin, _ = setup
    plugin = managed_identity(setup)
    rotated, result = execute(setup, command("rotate_credentials"))
    assert result["status"] == "succeeded"
    with connection(dsn) as conn:
        conn.execute(
            "INSERT INTO storage.principals(id,module,active) VALUES (%s,'sample',false)",
            (new_id(),),
        )
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), rotated["feedback_id"])
    assert all(not row[1] for row in principal_state(dsn))
    assert plugin.principal_id in {row[0] for row in principal_state(dsn)}
