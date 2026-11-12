"""Explicit scheduler grants: real isolated PG, HTTP/SDK and optional real NATS.

Fixture registration/credentials never target a running deployment. Trusted
backend writes seed synthetic data; all assertions exercise authenticated ACLs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataTypeSpec,
    ReevaluationResult,
    Registry,
    TracedCause,
    VersionRef,
    new_id,
    utcnow,
)
from ear_storage import AuthOperator, PostgresStorage, StorageService, bootstrap, migrate
from ear_storage.consumers import ConsumerPolicy
from ear_storage.http import create_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection, writer
from ear_storage_client import (
    Context,
    Create,
    FinishReevaluation,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    RegisterDependencies,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, SnapshotUnavailable
from fastapi.testclient import TestClient

from .test_http import TestTransport
from .test_http_postgres import live_server

pytestmark = pytest.mark.postgres
SYS = Context.system()


def registration():
    types = dict(DEFAULT_REGISTRY.data_types)
    for name in ("attribution", "artifact"):
        types[name] = replace(types[name], supports_reevaluation=True, requires_dependencies=True)
    return (
        Registry(types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types)
        .extend_core(
            DataTypeSpec("attribution_state", "system_record", "attribution"),
            DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
            DataTypeSpec(
                "rule_selection",
                "semantic",
                "extraction",
                unique_fields=("activity_id",),
                requires_dependencies=True,
                supports_reevaluation=True,
            ),
        )
        .extend(
            DataTypeSpec("sample_item", "semantic", "sample", supports_reevaluation=True),
            DataTypeSpec("sample_completion", "system_record", "sample", reevaluation_result=True),
        )
    )


def explicit_grants(registry):
    events = {
        n
        for n, s in registry.data_types.items()
        if s.emits_events and n != "plugin_management_feedback"
    }
    reads = (
        events
        | {n for n, s in registry.data_types.items() if s.reevaluation_result}
        | {"propagation_record", "attribution_feedback_result"}
    )
    return reads, events


@pytest.fixture
def access(pg_dsn):
    registry = registration()
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry)
    operator = AuthOperator(pg_dsn)
    reads, subscriptions = explicit_grants(registry)
    dep = operator.issue_principal("dependency", read_types=reads, subscribe_types=subscriptions)
    owner = operator.issue_principal(
        "sample",
        read_types=["sample_item", "sample_completion", "person", "rule"],
        subscribe_types=["sample_item"],
    )
    store = PostgresStorage(pg_dsn)
    source, target, secondary = new_id(), new_id(), new_id()
    store.client("attribution").write(
        [Create("person", source, {"name": "synthetic"})], SYS
    ).check()
    store.client("extraction").write([Create("rule", secondary, {})], SYS).check()
    store.client("sample").write(
        [
            Create("sample_item", target, {}),
            RegisterDependencies(
                "sample_item",
                target,
                [
                    VersionRef(data_type="person", id=source, version=1),
                ],
            ),
        ],
        SYS,
    ).check()
    store.client("attribution").write(
        [Update("person", source, 1, {"name": "changed"})], SYS
    ).check()
    return dict(
        dsn=pg_dsn,
        registry=registry,
        operator=operator,
        dep=dep,
        owner=owner,
        store=store,
        service=StorageService(pg_dsn),
        reads=reads,
        subs=subscriptions,
        source=source,
        target=target,
        secondary=secondary,
    )


def request_op(a, *, multi=False):
    extra = (
        (
            TracedCause(
                cause=Cause(
                    data_type="rule", data_id=a["secondary"], old_version=None, new_version=1
                ),
                root_feedback_id="system",
                depth=1,
            ),
        )
        if multi
        else ()
    )
    return PublishReevaluation(
        "sample_item",
        a["target"],
        Cause(data_type="person", data_id=a["source"], old_version=1, new_version=2),
        "synthetic change",
        new_id(),
        additional_causes=extra,
    )


def sdk(http, issued):
    return RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http))


def test_complete_live_sdk_shape_metadata_and_no_implicit_sensitive_grants(access):
    a = access
    with live_server(a["service"]) as url, RemoteStorageClient(a["dep"].token, url) as client:
        state = client.scheduler_state()
        assert set(state) == {"codec", "coverage_complete", "coverage_revision", "owners"}
        assert state["codec"] == 1 and state["coverage_complete"] is True
        assert len(state["coverage_revision"]) == 64
        assert state == client.scheduler_state()
        assert state["owners"]["sample"] == dict(
            enabled=True, can_receive=True, revision=None, online=None
        )
        # Core registration is not proof that any active executor identity exists.
        assert state["owners"]["extraction"]["can_receive"] is False
        assert all(v["online"] is None for v in state["owners"].values())
        assert len(client.dependents_of(a["source"])) == 1
        assert len(client.stale_dependencies([a["source"]])) == 1
        with client.snapshot() as snapshot:
            assert len(snapshot.dependents_of(a["source"])) == 1
        assert (
            not {"plugin_management_feedback", "plugin_management_result", "llm_call"}
            & client.registry.data_types.keys()
        )
        with pytest.raises(PermissionDenied):
            client.list_plugin_directory()
    with connection(a["dsn"]) as conn:
        grants = {
            r[0]
            for r in conn.execute(
                "SELECT data_type FROM storage.type_grants WHERE principal_id=%s",
                (a["dep"].principal_id,),
            )
        }
    assert grants == a["reads"]


@pytest.mark.parametrize("role", ["api", "admin", "sample", "extraction", "forged"])
def test_state_rejects_roles_and_self_reported_identity(access, role):
    a = access
    module = "api" if role in {"admin", "forged"} else role
    issued = a["operator"].issue_principal(
        module, read_types=a["reads"], subscribe_types=a["subs"], admin=role == "admin"
    )
    with TestClient(create_app(a["service"])) as http:
        response = http.get(
            "/v1/scheduler/state",
            headers={
                "Authorization": "Bearer " + issued.token,
                "X-Module": "dependency",
                "X-Admin": "true",
            },
        )
        assert response.status_code == 403
        assert response.json() == dict(code="forbidden", message="access denied", details={})
        forged = http.request(
            "GET",
            "/v1/scheduler/state?module=dependency&admin=true",
            json={"module": "dependency", "admin": True},
            headers={"Authorization": "Bearer " + issued.token},
        )
        assert forged.status_code == 403
        with sdk(http, issued) as client:
            with pytest.raises(PermissionDenied):
                client.scheduler_state()
            if role == "admin":
                assert client.list_plugin_directory()["items"] == []
            else:
                with pytest.raises(PermissionDenied):
                    client.list_plugin_directory()
        if role == "forged":
            with pytest.raises(PermissionDenied):
                RemoteStorageClient(
                    issued.token, "http://test", module="dependency", transport=TestTransport(http)
                )


@pytest.mark.parametrize(
    "missing,subscription",
    [
        ("sample_item", False),
        ("sample_item", True),
        ("sample_completion", False),
        ("propagation_record", False),
        ("attribution_feedback_result", False),
        ("voiceprint", False),
    ],
)
def test_withdrawal_blocks_all_graph_paths_and_snapshot_without_leaking(
    access, missing, subscription
):
    a = access
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        sid = client.create_snapshot()["snapshot_id"]
        snapshot = client.read_snapshot(sid)
        assert snapshot.dependents_of(a["source"])
        reads = a["reads"] if subscription else a["reads"] - {missing}
        subs = a["subs"] - {missing}
        a["operator"].set_grants(a["dep"].principal_id, read_types=reads, subscribe_types=subs)
        actions = [
            client.scheduler_state,
            lambda: client.registry,
            lambda: client.dependents_of(a["source"]),
            lambda: client.stale_dependencies([a["source"]]),
            lambda: client.dependencies_of(a["target"]),
            lambda: client.dependency_set(a["target"]),
            client.create_snapshot,
            lambda: snapshot.get(a["target"]),
            lambda: snapshot.dependents_of(a["source"]),
            lambda: client.write([request_op(a)], SYS.deeper()),
            client.list_history_reevaluations,
        ]
        for action in actions:
            with pytest.raises(PermissionDenied):
                action()
        response = http.get(
            "/v1/scheduler/state", headers={"Authorization": "Bearer " + a["dep"].token}
        )
        assert response.status_code == 403
        assert missing not in response.text and a["target"] not in response.text
        a["operator"].set_grants(
            a["dep"].principal_id, read_types=a["reads"], subscribe_types=a["subs"]
        )
        assert client.scheduler_state()["coverage_complete"] is True
        client.release_snapshot(sid)


def test_new_type_invalidates_coverage_then_old_snapshot_even_after_explicit_grant(access):
    a = access
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        revision = client.scheduler_state()["coverage_revision"]
        sid = client.create_snapshot()["snapshot_id"]
        snapshot = client.read_snapshot(sid)
        expanded = a["registry"].extend(DataTypeSpec("sample_new", "semantic", "sample"))
        bootstrap(a["dsn"], expanded)  # isolated test only, no grants implicitly added
        new = new_id()
        a["store"].client("sample").write(
            [
                Create("sample_new", new, {}),
                RegisterDependencies(
                    "sample_new", new, [VersionRef(data_type="person", id=a["source"], version=2)]
                ),
            ],
            SYS,
        ).check()
        for action in (
            client.scheduler_state,
            lambda: client.registry,
            lambda: client.dependents_of(a["source"]),
            lambda: client.stale_dependencies([a["source"]]),
            lambda: snapshot.dependents_of(a["source"]),
            lambda: client.write([request_op(a)], SYS.deeper()),
        ):
            with pytest.raises(PermissionDenied):
                action()
        reads, subs = explicit_grants(expanded)
        a["operator"].set_grants(a["dep"].principal_id, read_types=reads, subscribe_types=subs)
        assert client.scheduler_state()["coverage_revision"] != revision
        assert len(client.dependents_of(a["source"])) == 2
        for action in (
            lambda: snapshot.get(a["source"]),
            lambda: snapshot.dependents_of(a["source"]),
        ):
            with pytest.raises(SnapshotUnavailable):
                action()
        with client.snapshot() as fresh:
            assert len(fresh.dependents_of(a["source"])) == 2


def test_grant_fingerprint_and_legacy_snapshot_are_fail_closed(access):
    a = access
    client = a["service"].client(a["dep"].token)
    before = client.scheduler_state()["coverage_revision"]
    sid = client.create_snapshot().snapshot_id
    a["operator"].set_grants(
        a["dep"].principal_id, read_types=a["reads"] | {"extraction_run"}, subscribe_types=a["subs"]
    )
    assert client.scheduler_state()["coverage_revision"] != before
    with pytest.raises(PermissionDenied):
        client.read_snapshot(sid)
    legacy = client.create_snapshot().snapshot_id
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute(
            "DELETE FROM storage.snapshot_items WHERE snapshot_id=%s AND kind='scheduler_coverage'",
            (legacy,),
        )
    with pytest.raises(PermissionDenied):
        client.read_snapshot(legacy)


def test_revoked_credentials_and_absent_required_registration(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    operator = AuthOperator(pg_dsn)
    reads = set(DEFAULT_REGISTRY.data_types)
    issued = operator.issue_principal("dependency", read_types=reads, subscribe_types=reads)
    with TestClient(create_app(StorageService(pg_dsn))) as http, sdk(http, issued) as client:
        with pytest.raises(PermissionDenied):
            client.scheduler_state()  # baseline lacks attribution_feedback_result
        operator.revoke(issued.principal_id)
        with pytest.raises(AuthenticationError):
            client.scheduler_state()


def test_owner_causes_publish_replay_and_real_unchanged_completion(access):
    a = access
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        op = request_op(a, multi=True)
        owner_reads = ["sample_item", "sample_completion", "person"]
        a["operator"].set_grants(
            a["owner"].principal_id, read_types=owner_reads, subscribe_types=["sample_item"]
        )
        with pytest.raises(PermissionDenied):
            client.write([op], SYS.deeper())
        with connection(a["dsn"]) as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM storage.outbox WHERE event_id=%s", (op.event_id,)
                ).fetchone()
                is None
            )
        a["operator"].set_grants(
            a["owner"].principal_id,
            read_types=[*owner_reads, "rule"],
            subscribe_types=["sample_item"],
        )
        receipt_id = new_id()
        event = client.write([op], SYS.deeper(), request_id=receipt_id).check().events[0]
        a["operator"].reserve_consumer_identity("sample.scheduler_test", a["owner"].principal_id)
        result = ReevaluationResult(
            request_event_id=event.event_id,
            target_type="sample_item",
            target_id=a["target"],
            outcome="unchanged",
            before_version=1,
            after_version=1,
            root_feedback_id="system",
            depth=1,
            additional_causes=event.additional_causes,
            completed_at=utcnow(),
        )
        with sdk(http, a["owner"]) as owner:
            owner.write(
                [
                    RegisterDependencies(
                        "sample_item",
                        a["target"],
                        [VersionRef(data_type="person", id=a["source"], version=2)],
                    ),
                    FinishReevaluation(result),
                    MarkProcessed("sample.scheduler_test", event.event_id),
                ],
                Context.of(event),
            ).check()
        assert client.get_reevaluation_result(event.event_id) == result
        a["operator"].revoke(a["owner"].principal_id)
        assert client.scheduler_state()["owners"]["sample"]["can_receive"] is False
        with pytest.raises(PermissionDenied):
            client.write([op], SYS.deeper(), request_id=receipt_id)


def test_managed_owner_lifecycle_and_effective_grants(access):
    a = access
    client = a["service"].client(a["dep"].token)
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute(
            "INSERT INTO storage.module_lifecycle(module,enabled,resources_ready,revision) "
            "VALUES ('sample',true,true,7)"
        )
        for dtype in ("sample_item", "sample_completion", "person", "rule"):
            conn.execute(
                "INSERT INTO storage.module_permissions VALUES (%s,%s,%s)",
                ("sample", dtype, dtype == "sample_item"),
            )
    assert client.scheduler_state()["owners"]["sample"] == dict(
        enabled=True, can_receive=True, revision=7, online=None
    )
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute(
            "DELETE FROM storage.module_permissions WHERE module='sample' AND data_type='person'"
        )
    with pytest.raises(PermissionDenied):
        client.write([request_op(a)], SYS.deeper())
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute(
            "UPDATE storage.module_lifecycle SET enabled=false,revision=8 WHERE module='sample'"
        )
    assert client.scheduler_state()["owners"]["sample"] == dict(
        enabled=False, can_receive=False, revision=8, online=None
    )


@pytest.mark.nats
def test_real_nats_queued_delivery_rechecks_all_causes_and_scheduler_revocation(
    access, nats_server
):
    a = access
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="SCHED_" + suffix,
        prefix="sched_" + suffix,
        timeout=0.2,
        lease_seconds=5,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    service = StorageService(
        a["dsn"],
        relay_config=cfg,
        consumer_policy=ConsumerPolicy(poll_timeout=0.05, delivery_lease=0.5, ack_wait=1),
    )
    with (
        live_server(service) as url,
        RemoteStorageClient(a["dep"].token, url) as dep,
        RemoteStorageClient(a["owner"].token, url) as owner,
    ):
        owner.subscribe(
            "sample.scheduler_delivery", lambda event: None, event_types=["reevaluation_requested"]
        )
        seen = []
        dep.subscribe("dependency.scheduler_delivery", seen.append, event_types=["data_changed"])
        event = dep.write([request_op(a, multi=True)], SYS.deeper()).check().events[0]
        a["operator"].set_grants(
            a["owner"].principal_id,
            read_types=["sample_item", "sample_completion", "person"],
            subscribe_types=["sample_item"],
        )
        asyncio.run(run_relay(RelayStore(a["dsn"], cfg), once=True))
        with pytest.raises(PermissionDenied):
            owner.poll_once()
        a["operator"].set_grants(
            a["owner"].principal_id,
            read_types=["sample_item", "sample_completion", "person", "rule"],
            subscribe_types=["sample_item"],
        )
        # A denied broker fetch is not ACKed; wait for real JetStream redelivery.
        delivered = 0
        for _ in range(30):
            delivered += owner.poll_once()
            if delivered:
                break
            time.sleep(0.05)
        assert delivered == 1
        assert dep.scheduler_state()["coverage_complete"] is True
        a["operator"].set_grants(
            a["dep"].principal_id,
            read_types=a["reads"] - {"sample_item"},
            subscribe_types=a["subs"] - {"sample_item"},
        )
        with pytest.raises(PermissionDenied):
            dep.poll_once()
        assert seen == []
        with connection(a["dsn"]) as conn:
            row = conn.execute(
                "SELECT event_text FROM storage.outbox WHERE event_id=%s", (event.event_id,)
            ).fetchone()
            assert row and json.loads(row[0])["additional_causes"]


def test_ordinary_partial_graph_acl_is_unchanged(access):
    a = access
    issued = a["operator"].issue_principal("api", read_types=["person"])
    with TestClient(create_app(a["service"])) as http, sdk(http, issued) as client:
        assert client.dependents_of(a["source"]) == []
        assert client.stale_dependencies([a["source"]]) == []
        assert set(client.registry.data_types) == {
            "person",
            "feedback",
            "plugin_management_feedback",
        }
        with client.snapshot() as snapshot:
            assert snapshot.dependents_of(a["source"]) == []
        with pytest.raises(PermissionDenied):
            client.scheduler_state()


def test_registered_module_name_without_core_provenance_is_not_scheduler(access):
    a = access
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute(
            "UPDATE storage.registry SET is_core=false "
            "WHERE spec_text::jsonb->>'writer'='dependency'"
        )
    client = a["service"].client(a["dep"].token)
    with pytest.raises(PermissionDenied):
        client.scheduler_state()
    with pytest.raises(PermissionDenied):
        client.dependents_of(a["source"])


def test_owner_permissions_are_not_unioned_across_identities(access):
    a = access
    a["operator"].set_grants(
        a["owner"].principal_id,
        read_types=["sample_item", "person"],
        subscribe_types=["sample_item"],
    )
    a["operator"].issue_principal(
        "sample", read_types=["sample_item", "rule"], subscribe_types=["sample_item"]
    )
    with pytest.raises(PermissionDenied):
        a["service"].client(a["dep"].token).write([request_op(a, multi=True)], SYS.deeper())


def test_snapshot_context_cleanup_does_not_mask_primary_failure_after_withdrawal(access):
    a = access
    with (
        TestClient(create_app(a["service"])) as http,
        sdk(http, a["dep"]) as client,
        pytest.raises(RuntimeError, match="primary computation failure"),
        client.snapshot(),
    ):
        a["operator"].set_grants(a["dep"].principal_id)
        raise RuntimeError("primary computation failure")


@pytest.mark.nats
def test_new_grants_do_not_silently_expand_old_scheduler_consumer(access, nats_server):
    a = access
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="SCHED_" + suffix,
        prefix="sched_" + suffix,
        timeout=2,
        lease_seconds=5,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    service = StorageService(a["dsn"], relay_config=cfg)
    with live_server(service) as url, RemoteStorageClient(a["dep"].token, url) as client:
        name = "dependency.fixed_scope"
        client.subscribe(name, lambda event: None, event_types=["data_changed"])
        with connection(a["dsn"]) as conn:
            old = conn.execute(
                "SELECT generation,config_text FROM storage.consumers WHERE name=%s", (name,)
            ).fetchone()
        expanded = a["registry"].extend(DataTypeSpec("sample_future", "semantic", "sample"))
        bootstrap(a["dsn"], expanded)
        reads, subs = explicit_grants(expanded)
        a["operator"].set_grants(a["dep"].principal_id, read_types=reads, subscribe_types=subs)
        # Grant coverage is not a claim that old broker filters were reconfigured.
        assert client.scheduler_state()["coverage_complete"] is True
        with pytest.raises(PermissionDenied):
            client.poll_once()
        with (
            RemoteStorageClient(a["dep"].token, url) as restarted,
            pytest.raises(PermissionDenied),
        ):
            restarted.subscribe(name, lambda event: None, event_types=["data_changed"])
        with connection(a["dsn"]) as conn:
            assert (
                conn.execute(
                    "SELECT generation,config_text FROM storage.consumers WHERE name=%s", (name,)
                ).fetchone()
                == old
            )
