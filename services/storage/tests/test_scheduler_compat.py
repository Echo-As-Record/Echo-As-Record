"""Real PG compatibility: legacy filtered access is not strict graph authority.

No global fixture migration. Legacy tests use DEFAULT_REGISTRY unchanged;
strict tests explicitly bootstrap a persisted trusted core bundle. Synthetic
records only; all assertions go through authenticated service or HTTP/SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from dataclasses import replace

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, Registry, VersionRef, new_id
from ear_dependency.authority import SchedulerBlocked
from ear_dependency.journal import CommitJournal
from ear_dependency.remote import RemoteDependencyService
from ear_storage import AuthOperator, PostgresStorage, StorageService, bootstrap, migrate
from ear_storage.http import create_app
from ear_storage.management import ManagementProcessor, submit
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.scheduler_access import PROFILE, strict_profile
from ear_storage.schema import SchemaMismatch, connection
from ear_storage_client import (
    Context,
    Create,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Update,
)
from fastapi.testclient import TestClient

from .test_history_scheduler import arrange, publish
from .test_management import declaration
from .test_management_integration import setup as setup
from .test_relay import env as env
from .test_scheduler_access import access as access
from .test_scheduler_access import registration, request_op, sdk

pytestmark = pytest.mark.postgres
SYS = Context.system()


@pytest.fixture
def legacy(pg_dsn):
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec("sample_item", "semantic", "sample"))
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry)
    store = PostgresStorage(pg_dsn)
    source, target, hidden = (new_id() for _ in range(3))
    store.client("attribution").write([Create("person", source, {"v": 1})], SYS).check()
    for module, dtype, rid in (
        ("extraction", "artifact", target),
        ("sample", "sample_item", hidden),
    ):
        store.client(module).write(
            [
                Create(dtype, rid, {}),
                RegisterDependencies(
                    dtype,
                    rid,
                    [
                        VersionRef(data_type="person", id=source, version=1),
                    ],
                ),
            ],
            SYS,
        ).check()
    store.client("attribution").write([Update("person", source, 1, {"v": 2})], SYS).check()
    operator = AuthOperator(pg_dsn)
    issued = operator.issue_principal("dependency", read_types=["person", "artifact"])
    return dict(
        dsn=pg_dsn,
        registry=registry,
        store=store,
        operator=operator,
        dep=issued,
        service=StorageService(pg_dsn),
        source=source,
        target=target,
        hidden=hidden,
    )


def legacy_request(a):
    return PublishReevaluation(
        "artifact",
        a["target"],
        Cause(data_type="person", data_id=a["source"], old_version=1, new_version=2),
        "synthetic legacy request",
        new_id(),
    )


def test_legacy_partial_graph_live_and_snapshot_still_filter_without_proof(legacy):
    a = legacy
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        assert "sample_item" not in client.registry.data_types
        assert "attribution_feedback_result" not in client.registry.data_types
        with client.snapshot() as snapshot:
            for reader in (client, snapshot):
                assert [e.dependent.id for e in reader.dependents_of(a["source"])] == [a["target"]]
                assert [e.dependent.id for e in reader.stale_dependencies([a["source"]])] == [
                    a["target"]
                ]
                group = reader.dependency_set(a["target"])
                assert group is not None and len(group.dependencies) == 1
                assert reader.get(a["hidden"]) is None
            a["operator"].set_grants(a["dep"].principal_id, read_types=["artifact"])
            assert client.dependents_of(a["source"]) == []
            with pytest.raises(PermissionDenied):
                snapshot.dependents_of(a["source"])
            for reader in (client, snapshot):
                with pytest.raises(PermissionDenied):
                    reader.dependency_set(a["target"])  # Never a fake complete empty set.
        with pytest.raises(PermissionDenied):
            client.scheduler_state()


@pytest.mark.parametrize("owner_state", ["absent", "no_grants", "revoked", "disabled"])
def test_legacy_publish_and_identical_replay_do_not_require_owner_readiness(legacy, owner_state):
    a = legacy
    if owner_state != "absent":
        owner = a["operator"].issue_principal("extraction")
        if owner_state == "revoked":
            a["operator"].revoke(owner.principal_id)
        elif owner_state == "disabled":
            with connection(a["dsn"]) as conn:
                conn.execute(
                    "INSERT INTO storage.module_lifecycle(module,enabled,resources_ready) "
                    "VALUES ('extraction',false,false)"
                )
    assert not a["registry"].get("artifact").supports_reevaluation
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        op, rid = legacy_request(a), new_id()
        first = client.write([op], SYS.deeper(), request_id=rid).check()
        # New service instance reads the same persisted deployment, no process opt-in.
        with (
            TestClient(create_app(StorageService(a["dsn"]))) as restarted_http,
            sdk(restarted_http, a["dep"]) as restarted,
        ):
            assert restarted.write([op], SYS.deeper(), request_id=rid).check() == first
        a["operator"].set_grants(a["dep"].principal_id, read_types=["artifact"])
        with pytest.raises(PermissionDenied):
            client.write([op], SYS.deeper(), request_id=rid)
        with connection(a["dsn"]) as conn:
            assert conn.execute(
                "SELECT count(*) FROM storage.outbox WHERE event_id=%s", (op.event_id,)
            ).fetchone() == (1,)


def test_legacy_full_grants_and_request_flags_cannot_mint_strict_proof(legacy, tmp_path):
    a = legacy
    reads, subs = set(a["registry"].data_types), set(a["registry"].data_types)
    a["operator"].set_grants(a["dep"].principal_id, read_types=reads, subscribe_types=subs)
    with TestClient(create_app(a["service"])) as http, sdk(http, a["dep"]) as client:
        assert len(client.dependents_of(a["source"])) == 2
        response = http.request(
            "GET",
            "/v1/scheduler/state?strict=true&full_visibility=true",
            headers={"Authorization": "Bearer " + a["dep"].token, "X-Scheduler-Authority": "true"},
            json={"strict": True, "scheduler_authority": True, "profile": PROFILE},
        )
        assert response.status_code == 422  # Request flags are rejected, not honored.
        with pytest.raises(PermissionDenied):
            client.scheduler_state()
        with closing(CommitJournal(tmp_path / "journal", namespace="compat")) as journal:
            worker = RemoteDependencyService(client, journal=journal)
            with pytest.raises(SchedulerBlocked, match="coverage_denied"):
                worker.subscribe()
        with connection(a["dsn"]) as conn:
            assert conn.execute("SELECT count(*) FROM storage.consumers").fetchone() == (0,)


@pytest.mark.parametrize(
    "profile", ["feedback_only", "attribution_only", "extraction_only", "untrusted"]
)
def test_partial_or_plugin_profiles_do_not_enable_strict(pg_dsn, profile):
    full = registration()
    extras = [
        full.get(n) for n in ("attribution_state", "attribution_feedback_result", "rule_selection")
    ]
    if profile == "feedback_only":
        registry = DEFAULT_REGISTRY.extend_core(extras[1])
    elif profile == "attribution_only":
        types = dict(DEFAULT_REGISTRY.data_types)
        types["attribution"] = full.get("attribution")
        registry = Registry(
            types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
        ).extend_core(*extras[:2])
    elif profile == "extraction_only":
        types = dict(DEFAULT_REGISTRY.data_types)
        types["artifact"] = full.get("artifact")
        registry = Registry(
            types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
        ).extend_core(extras[2])
    else:
        registry = DEFAULT_REGISTRY.extend(*extras)
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry)
    operator = AuthOperator(pg_dsn)
    issued = operator.issue_principal(
        "dependency", read_types=registry.data_types, subscribe_types=registry.data_types
    )
    client = StorageService(pg_dsn).client(issued.token)
    assert client.dependents_of(new_id()) == []
    with pytest.raises(PermissionDenied):
        client.scheduler_state()


@pytest.mark.parametrize(
    "defect",
    [
        "attribution_caps",
        "artifact_caps",
        "selection_caps",
        "selection_owner",
        "completion",
        "publisher",
        "propagation_provenance",
    ],
)
def test_complete_but_invalid_core_bundle_fails_closed_not_legacy(pg_dsn, defect):
    registry = registration()
    types, publishers, core = (
        dict(registry.data_types),
        dict(registry.event_publishers),
        registry.core_types,
    )
    if defect == "attribution_caps":
        types["attribution"] = replace(types["attribution"], requires_dependencies=False)
    elif defect == "artifact_caps":
        types["artifact"] = replace(types["artifact"], supports_reevaluation=False)
    elif defect == "selection_caps":
        types["rule_selection"] = replace(types["rule_selection"], requires_dependencies=False)
    elif defect == "selection_owner":
        types["rule_selection"] = replace(types["rule_selection"], writer="sample")
    elif defect == "completion":
        types["attribution_completion"] = replace(
            types["attribution_completion"], reevaluation_result=False
        )
    elif defect == "publisher":
        publishers["reevaluation_requested"] = ("dependency", "sample")
    else:
        core = core - {"propagation_record"}
    registry = Registry(types, publishers, core)
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry)
    issued = AuthOperator(pg_dsn).issue_principal(
        "dependency", read_types=types, subscribe_types=types
    )
    client = StorageService(pg_dsn).client(issued.token)
    for action in (
        client.scheduler_state,
        lambda: client.registry,
        lambda: client.dependents_of(new_id()),
        client.create_snapshot,
    ):
        with pytest.raises(PermissionDenied):
            action()


def test_strict_partial_identity_cannot_use_legacy_side_door(access):
    a = access
    partial = a["operator"].issue_principal("dependency", read_types=["person", "sample_item"])
    client = StorageService(a["dsn"]).client(partial.token)
    # Ordinary payload reads remain ACL reads, not a graph proof.
    assert client.get(a["source"]) is not None
    for action in (
        client.scheduler_state,
        lambda: client.registry,
        lambda: client.dependents_of(a["source"]),
        lambda: client.dependency_set(a["target"]),
        lambda: client.stale_dependencies([a["source"]]),
        client.create_snapshot,
        client.list_history_reevaluations,
        lambda: client.attach_history_request(new_id(), new_id(), new_id()),
        lambda: client.write([request_op(a)], SYS.deeper()),
    ):
        with pytest.raises(PermissionDenied):
            action()
    assert a["service"].client(a["dep"].token).scheduler_state()["coverage_complete"] is True


def test_profile_is_persistent_fingerprinted_and_never_auto_grants(access, caplog):
    a = access
    caplog.set_level(logging.DEBUG, logger="ear_storage.scheduler_access")
    before = a["service"].client(a["dep"].token).scheduler_state()
    fingerprint = strict_profile(a["registry"])
    assert fingerprint is not None and len(fingerprint) == 64
    assert StorageService(a["dsn"]).client(a["dep"].token).scheduler_state() == before
    messages = [
        json.loads(r.message) for r in caplog.records if r.name == "ear_storage.scheduler_access"
    ]
    assert messages and all(
        m["profile_fingerprint"] == fingerprint and m["profile"] == PROFILE for m in messages
    )
    assert a["dep"].token not in caplog.text
    with connection(a["dsn"]) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (14,)
        grants = set(
            conn.execute(
                "SELECT data_type,can_subscribe FROM storage.type_grants WHERE principal_id=%s",
                (a["dep"].principal_id,),
            ).fetchall()
        )
    assert grants == {(n, n in a["subs"]) for n in a["reads"]}


def test_legacy_bootstrap_cannot_silently_upgrade_existing_capabilities(legacy):
    with pytest.raises(SchemaMismatch):
        bootstrap(legacy["dsn"], registration())
    with pytest.raises(PermissionDenied):
        legacy["service"].client(legacy["dep"].token).scheduler_state()
    assert legacy["service"].client(legacy["dep"].token).dependents_of(legacy["source"])


def test_ordinary_plugin_cannot_switch_mode_or_claim_core_authority(access):
    a = access
    plugin = a["operator"].issue_principal(
        "sample", read_types=a["reads"], subscribe_types=a["subs"]
    )
    before = a["service"].client(a["dep"].token).scheduler_state()
    with TestClient(create_app(a["service"])) as http, sdk(http, plugin) as client:
        response = http.request(
            "GET",
            "/v1/scheduler/state?strict=false&module=dependency",
            headers={"Authorization": "Bearer " + plugin.token, "X-Module": "dependency"},
            json={"scheduler_authority": True},
        )
        assert response.status_code == 403
        with pytest.raises(PermissionDenied):
            client.write(
                [PutSystemRecord("attribution_state", new_id(), {"scheduler_authority": True})], SYS
            )
        with pytest.raises(PermissionDenied):
            client.write([request_op(a)], SYS.deeper())
        assert client.dependents_of(a["source"])  # Ordinary plugin ACLs are unchanged.
    assert a["service"].client(a["dep"].token).scheduler_state() == before


@pytest.mark.nats
def test_legacy_history_bridge_still_accepts_scope_limited_scheduler(setup):
    _, issued, _, scheduler, source, targets, job, _, root = arrange(setup)
    assert scheduler.list_history_reevaluations(job)["items"]
    event = publish(scheduler, source, targets[0], root)
    restarted = StorageService(setup[0]).client(issued.token)
    linked = restarted.attach_history_request(job, targets[0], event.event_id)
    assert restarted.attach_history_request(job, targets[0], event.event_id) == linked
    with pytest.raises(PermissionDenied):
        restarted.scheduler_state()
    no_grants = setup[4].issue_principal("dependency")
    assert setup[3].client(no_grants.token).list_history_reevaluations()["items"] == []


@pytest.mark.nats
def test_legacy_consumer_grant_growth_does_not_require_full_graph_or_reconfigure(
    legacy, nats_server
):
    a = legacy
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="COMPAT_" + suffix,
        prefix="compat_" + suffix,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    asyncio.run(run_relay(RelayStore(a["dsn"], cfg), once=True))
    service = StorageService(a["dsn"], relay_config=cfg)
    a["operator"].set_grants(
        a["dep"].principal_id, read_types=["person"], subscribe_types=["person"]
    )
    with TestClient(create_app(service)) as http, sdk(http, a["dep"]) as client:
        seen = []
        client.subscribe("dependency.legacy", seen.append, event_types=["data_changed"])
        with connection(a["dsn"]) as conn:
            before = conn.execute(
                "SELECT generation,config_text FROM storage.consumers "
                "WHERE name='dependency.legacy'"
            ).fetchone()
        a["operator"].set_grants(
            a["dep"].principal_id,
            read_types=["person", "artifact"],
            subscribe_types=["person", "artifact"],
        )
        with sdk(http, a["dep"]) as restarted:
            restarted.subscribe("dependency.legacy", seen.append, event_types=["data_changed"])
        event = (
            a["store"]
            .client("attribution")
            .write([Update("person", a["source"], 2, {"v": 3})], SYS)
            .check()
            .events[0]
        )
        asyncio.run(run_relay(RelayStore(a["dsn"], cfg), once=True))
        assert client.poll_once() >= 1
        assert event.event_id in {e.event_id for e in seen}
        with connection(a["dsn"]) as conn:
            assert (
                conn.execute(
                    "SELECT generation,config_text FROM storage.consumers "
                    "WHERE name='dependency.legacy'"
                ).fetchone()
                == before
            )


def test_live_plugin_registration_invalidates_strict_coverage_without_auto_grants(access):
    a = access
    scheduler = a["service"].client(a["dep"].token)
    before = scheduler.scheduler_state()
    sid = scheduler.create_snapshot().snapshot_id
    admin = a["operator"].issue_principal(
        "api", admin=True, read_types=["plugin_management_feedback", "plugin_management_result"]
    )
    body = declaration("future")
    # A full business read scope does not make an ordinary plugin an administrator.
    with pytest.raises(PermissionDenied):
        submit(a["service"].client(a["owner"].token), body)
    accepted = submit(a["service"].client(admin.token), body)
    assert ManagementProcessor(a["store"]).process(accepted["feedback_id"])["status"] == "succeeded"
    assert not a["store"].registry.is_core("future_item")
    for action in (
        scheduler.scheduler_state,
        lambda: scheduler.dependents_of(a["source"]),
        lambda: scheduler.read_snapshot(sid),
        lambda: scheduler.write([request_op(a)], SYS.deeper()),
    ):
        with pytest.raises(PermissionDenied):
            action()
    # Explicit subsequent deployment authorization, never registry-driven auto-grant.
    a["operator"].set_grants(
        a["dep"].principal_id,
        read_types=a["reads"] | {"future_item"},
        subscribe_types=a["subs"] | {"future_item"},
    )
    assert scheduler.scheduler_state()["coverage_revision"] != before["coverage_revision"]
    with pytest.raises(PermissionDenied):
        scheduler.read_snapshot(sid)
