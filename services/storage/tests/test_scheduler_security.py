"""Bounded regressions for the second review: synthetic, isolated PG + HTTP/SDK.

Propagation payloads are untrusted even when their module or schema tag matches.
The service admission gate, not caller fields, must authorize scheduling writes.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataTypeSpec,
    Registry,
    TracedCause,
    VersionRef,
    new_id,
)
from ear_dependency import CommitJournal, RemoteDependencyService
from ear_dependency.authority import SchedulerBlocked
from ear_dependency.domain import Step, Trigger
from ear_storage import AuthOperator, PostgresStorage, StorageService, bootstrap, migrate
from ear_storage.core_deployment import core_identity_policies, core_mvp_registry
from ear_storage.http import create_app
from ear_storage.scheduler_access import PROFILE_TYPES, strict_profile
from ear_storage.schema import connection, writer
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

from .test_scheduler_access import sdk

pytestmark = pytest.mark.postgres
SYS = Context.system()


def seed(dsn, registry):
    migrate(dsn)
    bootstrap(dsn, registry)
    store = PostgresStorage(dsn)
    source, target = new_id(), new_id()
    store.client("attribution").write([Create("person", source, {"name": "before"})], SYS).check()
    store.client("extraction").write(
        [
            Create("artifact", target, {"text": "synthetic"}),
            RegisterDependencies(
                "artifact", target, [VersionRef(data_type="person", id=source, version=1)]
            ),
        ],
        SYS,
    ).check()
    event = (
        store.client("attribution")
        .write([Update("person", source, 1, {"name": "after"})], SYS)
        .check()
        .events[0]
    )
    cause = Cause(data_type="person", data_id=source, old_version=1, new_version=2)
    return source, target, cause, event


def step_for(target, cause):
    return Step(
        target_id=target,
        target_type="artifact",
        target_version=1,
        causes=[TracedCause(cause=cause, root_feedback_id="system", depth=1)],
    )


def outbox_count(dsn):
    with connection(dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM storage.outbox "
            "WHERE event_text::jsonb->>'event_type'='reevaluation_requested'"
        ).fetchone()
        assert row is not None
        return row[0]


@pytest.fixture
def security(pg_dsn):
    registry = core_mvp_registry()
    source, target, cause, event = seed(pg_dsn, registry)
    operator = AuthOperator(pg_dsn)
    policy = core_identity_policies()["dependency"]
    full = operator.issue_principal(
        "dependency", read_types=policy.reads, subscribe_types=policy.subscriptions
    )
    executor = core_identity_policies()["extraction"]
    operator.issue_principal(
        "extraction", read_types=executor.reads, subscribe_types=executor.subscriptions
    )
    return dict(
        dsn=pg_dsn,
        source=source,
        target=target,
        cause=cause,
        event=event,
        operator=operator,
        full=full,
        policy=policy,
        service=StorageService(pg_dsn),
    )


def test_original_peer_probe_cannot_inject_pending_step(security, tmp_path):
    a = security
    peer = a["operator"].issue_principal("dependency", read_types=["propagation_record"])
    step = step_for(a["target"], a["cause"])
    with TestClient(create_app(a["service"])) as http, sdk(http, peer) as limited:
        with pytest.raises(PermissionDenied):
            limited.scheduler_state()
        with pytest.raises(PermissionDenied):
            limited.write(
                [PublishReevaluation("artifact", a["target"], a["cause"], "synthetic", new_id())],
                SYS.deeper(),
            )
        assert limited.get(a["source"]) is None and limited.get(a["target"]) is None
        with pytest.raises(PermissionDenied, match="access denied"):
            limited.write(
                [PutSystemRecord("propagation_record", step.id, step.model_dump(mode="json"))], SYS
            )
        with (
            sdk(http, a["full"]) as full,
            closing(CommitJournal(tmp_path / "journal", namespace="probe")) as journal,
        ):
            RemoteDependencyService(full, journal=journal).tick()
            assert full.get_system_record(step.id) is None
    assert outbox_count(a["dsn"]) == 0


@pytest.mark.parametrize(
    "missing",
    [
        "all",
        "subscriptions",
        "person",
        "artifact",
        "attribution_completion",
        "propagation_record",
        "attribution_feedback_result",
        "voiceprint",
    ],
)
def test_every_partial_identity_denied_queue_control_and_overwrite(security, missing):
    a = security
    reads, subs = a["policy"].reads, a["policy"].subscriptions
    if missing == "all":
        reads, subs = frozenset(), frozenset()
    elif missing == "subscriptions":
        subs = frozenset()
    else:
        reads, subs = reads - {missing}, subs - {missing}
    peer = a["operator"].issue_principal("dependency", read_types=reads, subscribe_types=subs)
    full = a["service"].client(a["full"].token)
    step = step_for(a["target"], a["cause"])
    original = PutSystemRecord("propagation_record", step.id, step.model_dump(mode="json"))
    full.write([original], SYS).check()
    before = full.get_system_record(step.id)
    payloads = [
        step.model_dump(mode="json"),
        step.model_copy(
            update={
                "state": "intent",
                "owner": "extraction",
                "authority_revision": full.scheduler_state()["coverage_revision"],
            }
        ).model_dump(mode="json"),
        Trigger(event=a["event"]).model_dump(mode="json"),
        {
            "schema_tag": "dependency_runtime_v1",
            "consumer": "dependency.changes",
            "status": "blocked",
            "reason": "coverage_denied",
            "pending_transport": False,
        },
        {
            "schema_tag": "dependency_runtime_v1",
            "record_kind": "step",
            "state": "pending",
            "caller_module": "dependency",
            "principal_id": a["full"].principal_id,
            "scheduler_authority": True,
        },
    ]
    with TestClient(create_app(a["service"])) as http, sdk(http, peer) as limited:
        for payload in payloads:
            for rid, revision in ((new_id(), None), (step.id, before.revision)):
                with pytest.raises(PermissionDenied, match="access denied"):
                    limited.write(
                        [
                            PutSystemRecord(
                                "propagation_record", rid, payload, expected_revision=revision
                            )
                        ],
                        SYS,
                    )
    assert full.get_system_record(step.id) == before
    assert len(full.system_history(step.id)) == 1
    assert outbox_count(a["dsn"]) == 0


def test_full_writer_replay_restore_and_revoked_diagnostic_fail_closed(security, tmp_path):
    a = security
    step = step_for(a["target"], a["cause"])
    op = PutSystemRecord("propagation_record", step.id, step.model_dump(mode="json"))
    request_id = new_id()
    with TestClient(create_app(a["service"])) as http, sdk(http, a["full"]) as full:
        receipt = full.write([op], SYS, request_id=request_id).check()
        assert full.write([op], SYS, request_id=request_id).check() == receipt
        sid = full.create_snapshot()["snapshot_id"]
        assert full.read_snapshot(sid).get_system_record(step.id) is not None
        a["operator"].set_grants(a["full"].principal_id, read_types=["propagation_record"])
        with pytest.raises(PermissionDenied):
            full.write([op], SYS, request_id=request_id)
        with pytest.raises(PermissionDenied):
            full.read_snapshot(sid).get_system_record(step.id)
        with closing(CommitJournal(tmp_path / "denied", namespace="diagnostic")) as journal:
            worker = RemoteDependencyService(full, journal=journal)
            # Explicit local failure: blocked diagnostics cannot be persisted either.
            with pytest.raises(SchedulerBlocked, match="write_denied"):
                worker.tick()
            assert journal.pending("runtime") is not None
            assert full.get_system_record(worker.status_id) is None
        assert outbox_count(a["dsn"]) == 0
        a["operator"].set_grants(
            a["full"].principal_id,
            read_types=a["policy"].reads,
            subscribe_types=a["policy"].subscriptions,
        )
        # Restore a legitimate persisted step with a new service/worker, not a new admission.
        with (
            TestClient(create_app(StorageService(a["dsn"]))) as restarted_http,
            sdk(restarted_http, a["full"]) as restarted,
            closing(CommitJournal(tmp_path / "restored", namespace="restore")) as journal,
        ):
            RemoteDependencyService(restarted, journal=journal).tick()
            saved = restarted.get_system_record(step.id)
            assert saved is not None
            assert saved.payload["state"] == "requested"
            assert saved.payload["authority_revision"]
            captured = restarted.read_snapshot(sid).get_system_record(step.id)
            assert captured is not None and captured.payload["state"] == "pending"
        assert outbox_count(a["dsn"]) == 1


def test_plugin_with_complete_reads_cannot_write_propagation(security):
    a = security
    registry = core_mvp_registry().extend(DataTypeSpec("sample_item", "system_record", "sample"))
    bootstrap(a["dsn"], registry)
    plugin = a["operator"].issue_principal(
        "sample", read_types=registry.data_types, subscribe_types=registry.data_types
    )
    client = a["service"].client(plugin.token)
    with pytest.raises(PermissionDenied):
        client.write([PutSystemRecord("propagation_record", new_id(), {"status": "blocked"})], SYS)
    client.write([PutSystemRecord("sample_item", new_id(), {"synthetic": True})], SYS).check()


@pytest.mark.parametrize("missing", [s.name for s in PROFILE_TYPES] + ["all_profile"])
def test_original_mixed_provenance_probe_fails_closed(pg_dsn, missing):
    full = core_mvp_registry()
    removed = {s.name for s in PROFILE_TYPES} if missing == "all_profile" else {missing}
    mixed = Registry(dict(full.data_types), dict(full.event_publishers), full.core_types - removed)
    source, target, cause, _ = seed(pg_dsn, mixed)
    assert mixed.to_json() == full.to_json()
    with pytest.raises(PermissionDenied, match="access denied"):
        strict_profile(mixed)
    operator = AuthOperator(pg_dsn)
    partial = operator.issue_principal(
        "dependency", read_types=["person", "artifact", "propagation_record"]
    )
    complete = operator.issue_principal(
        "dependency", read_types=full.data_types, subscribe_types=full.data_types
    )
    for issued in (partial, complete):
        with TestClient(create_app(StorageService(pg_dsn))) as http, sdk(http, issued) as client:
            # Ordinary ACL payload reads remain usable for diagnosis, never a proof.
            assert client.get(source) is not None
            actions = [
                client.scheduler_state,
                lambda: client.registry,
                lambda: client.dependents_of(source),
                lambda: client.dependency_set(target),
                lambda: client.stale_dependencies([source]),
                client.create_snapshot,
                lambda: client.write(
                    [PublishReevaluation("artifact", target, cause, "synthetic", new_id())],
                    SYS.deeper(),
                ),
                lambda: client.write(
                    [
                        PutSystemRecord(
                            "propagation_record",
                            new_id(),
                            step_for(target, cause).model_dump(mode="json"),
                        )
                    ],
                    SYS,
                ),
            ]
            for action in actions:
                with pytest.raises(PermissionDenied, match="access denied"):
                    action()
    assert outbox_count(pg_dsn) == 0


@pytest.mark.parametrize("missing", [s.name for s in PROFILE_TYPES])
def test_existing_snapshot_cannot_restore_after_mixed_provenance(security, missing):
    a = security
    client = a["service"].client(a["full"].token)
    sid = client.create_snapshot().snapshot_id
    snapshot = client.read_snapshot(sid)
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute("UPDATE storage.registry SET is_core=false WHERE name=%s", (missing,))
    for action in (
        lambda: client.read_snapshot(sid),
        lambda: snapshot.get(a["source"]),
        lambda: snapshot.dependents_of(a["source"]),
    ):
        with pytest.raises(PermissionDenied):
            action()


def test_strict_snapshot_proof_cannot_be_restored_as_legacy(security):
    a = security
    client = a["service"].client(a["full"].token)
    sid = client.create_snapshot().snapshot_id
    # Deployment-only synthetic corruption, not a remote configuration capability.
    with connection(a["dsn"]) as conn, writer(conn):
        conn.execute("DELETE FROM storage.type_grants WHERE data_type='rule_selection'")
        conn.execute("DELETE FROM storage.type_revisions WHERE data_type='rule_selection'")
        conn.execute("DELETE FROM storage.registry WHERE name='rule_selection'")
    with pytest.raises(PermissionDenied):
        client.read_snapshot(sid)


@pytest.mark.parametrize("profile", ["default", "attribution", "extraction", "both_caps"])
def test_plugin_name_collision_does_not_select_strict_or_break_legacy(pg_dsn, profile):
    full = core_mvp_registry()
    types, core = dict(DEFAULT_REGISTRY.data_types), DEFAULT_REGISTRY.core_types
    additions = []
    if profile == "both_caps":
        types["attribution"] = full.get("attribution")
        types["artifact"] = full.get("artifact")
        additions = list(PROFILE_TYPES[:2])
    elif profile == "attribution":
        types["attribution"] = full.get("attribution")
        additions = list(PROFILE_TYPES[:2])
    elif profile == "extraction":
        types["artifact"] = full.get("artifact")
        additions = [PROFILE_TYPES[2]]
    registry = Registry(types, DEFAULT_REGISTRY.event_publishers, core).extend_core(*additions)
    # A plugin occupying every missing profile name still cannot opt this database in.
    registry = registry.extend(
        *(replace(s, writer="sample") for s in PROFILE_TYPES if s.name not in registry.data_types)
    )
    assert strict_profile(registry) is None
    source, target, cause, _ = seed(pg_dsn, registry)
    issued = AuthOperator(pg_dsn).issue_principal(
        "dependency", read_types=["person", "artifact", "propagation_record"]
    )
    client = StorageService(pg_dsn).client(issued.token)
    with client.snapshot() as snapshot:
        assert client.dependents_of(source) and snapshot.dependents_of(source)
    client.write([PutSystemRecord("propagation_record", new_id(), {"legacy": True})], SYS).check()
    client.write(
        [PublishReevaluation("artifact", target, cause, "legacy", new_id())], SYS.deeper()
    ).check()
    with pytest.raises(PermissionDenied):
        client.scheduler_state()


@pytest.mark.parametrize("defect", ["owner", "kind", "capability", "completion", "publisher"])
def test_mixed_provenance_with_bad_specs_is_not_legacy(defect):
    full = core_mvp_registry()
    types, publishers = dict(full.data_types), dict(full.event_publishers)
    if defect == "owner":
        types["attribution_state"] = replace(types["attribution_state"], writer="sample")
    elif defect == "kind":
        types["attribution_state"] = replace(types["attribution_state"], kind="semantic")
    elif defect == "capability":
        types["artifact"] = replace(
            types["artifact"], requires_dependencies=False, supports_reevaluation=False
        )
    elif defect == "completion":
        types["attribution_completion"] = replace(
            types["attribution_completion"], reevaluation_result=False
        )
    else:
        publishers["reevaluation_requested"] = ("dependency", "sample")
    mixed = Registry(types, publishers, full.core_types - {"rule_selection"})
    with pytest.raises(PermissionDenied):
        strict_profile(mixed)
