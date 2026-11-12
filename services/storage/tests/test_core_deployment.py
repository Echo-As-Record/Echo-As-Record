"""Synthetic initialization only; PG fixture creates a NEW isolated temporary server.

No external DSN, NATS, ear-dev, real data/model, package install or deployment.
Faults model operation/response loss deterministically, not OS power-loss tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, Registry, new_id
from ear_storage import cli
from ear_storage import core_deployment as core
from ear_storage.auth import AuthOperator, authenticate, can_use_consumer, token_digest
from ear_storage.consumers import approved_filters
from ear_storage.deployment import init_identities
from ear_storage.http import create_app
from ear_storage.repository import registry_from_db
from ear_storage.scheduler_access import strict_profile
from ear_storage.schema import bootstrap, connection, migrate, writer
from ear_storage.service import StorageService
from ear_storage_client import Context, MarkProcessed, PermissionDenied, Query
from fastapi.testclient import TestClient


@pytest.fixture
def initialized(pg_dsn, tmp_path):
    migrate(pg_dsn)
    assert core.bootstrap_core(pg_dsn)
    output = tmp_path / "core-secrets" / "identities.json"
    assert core.init_core_identities(pg_dsn, output)
    return pg_dsn, output


def load(output: Path):
    return json.loads(output.read_text())["credentials"]


def snapshot(directory: Path):
    """Fingerprint test-only material without putting tokens in assertion output."""
    return {
        p.name: (p.lstat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in directory.iterdir()
        if p.is_file() and not p.is_symlink()
    }


def count_principals(dsn):
    with connection(dsn) as conn:
        row = conn.execute("SELECT count(*) FROM storage.principals").fetchone()
        assert row is not None
        return row[0]


def test_profile_is_fresh_strict_and_does_not_mutate_public_defaults():
    before = DEFAULT_REGISTRY.to_json()
    registry = core.core_mvp_registry()
    assert strict_profile(registry) is not None
    assert core.core_mvp_registry() is not registry
    assert DEFAULT_REGISTRY.to_json() == before
    assert not DEFAULT_REGISTRY.get("attribution").supports_reevaluation
    assert not DEFAULT_REGISTRY.get("artifact").requires_dependencies
    assert "rule_selection" not in DEFAULT_REGISTRY.data_types
    for name in ("attribution", "artifact", "rule_selection"):
        spec = registry.get(name)
        assert spec.supports_reevaluation and spec.requires_dependencies
    for name in ("attribution_completion", "extraction_completion"):
        assert registry.get(name).reevaluation_result
    policies = core.core_identity_policies()
    assert len(policies) == 6
    for p in policies.values():
        assert p.subscriptions <= p.reads <= registry.data_types.keys()
        assert not p.reads & {"plugin_management_feedback", "plugin_management_result"}
        assert not p.document()["admin"]
    assert policies["adapter"].reads == core.RAW_TYPES
    assert not policies["adapter"].subscriptions
    assert policies["llm_gateway"].reads - {"llm_call"} == policies["dependency"].subscriptions
    assert not policies["llm_gateway"].subscriptions


@pytest.mark.postgres
def test_migrate_is_explicit_and_bootstrap_is_idempotent(pg_dsn):
    with pytest.raises(core.CoreDeploymentError):
        core.bootstrap_core(pg_dsn)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('storage')").fetchone() == (None,)
    migrate(pg_dsn)
    assert core.bootstrap_core(pg_dsn)
    assert not core.bootstrap_core(pg_dsn)
    assert count_principals(pg_dsn) == 0
    with connection(pg_dsn) as conn:
        registry = registry_from_db(conn)
        assert registry.to_json() == core.core_mvp_registry().to_json()
        assert registry.core_types == core.core_mvp_registry().core_types
        assert strict_profile(registry) == strict_profile(core.core_mvp_registry())


@pytest.mark.postgres
@pytest.mark.parametrize(
    "variant", ["default", "partial", "untrusted", "wrong_capability", "old_schema"]
)
def test_legacy_profiles_are_not_upgraded(pg_dsn, tmp_path, variant):
    migrate(pg_dsn)
    registry = DEFAULT_REGISTRY
    if variant == "partial":
        registry = registry.extend_core(
            DataTypeSpec("attribution_state", "system_record", "attribution")
        )
    elif variant in {"untrusted", "wrong_capability"}:
        registry = core.core_mvp_registry()
        if variant == "untrusted":
            registry = Registry(
                registry.data_types, registry.event_publishers, DEFAULT_REGISTRY.core_types
            )
        else:
            types = dict(registry.data_types)
            types["artifact"] = replace(types["artifact"], requires_dependencies=False)
            registry = Registry(types, registry.event_publishers, registry.core_types)
    bootstrap(pg_dsn, registry)
    with connection(pg_dsn) as conn:
        if variant == "old_schema":
            conn.execute("UPDATE storage.meta SET schema_revision=13")
        before = conn.execute("SELECT * FROM storage.registry ORDER BY name").fetchall()
    with pytest.raises(core.CoreDeploymentError):
        core.bootstrap_core(pg_dsn)
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(pg_dsn, tmp_path / "rejected" / "identities.json")
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT * FROM storage.registry ORDER BY name").fetchall() == before
        assert count_principals(pg_dsn) == 0
        if variant == "old_schema":
            assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (13,)


@pytest.mark.postgres
def test_complete_retry_verifies_db_and_preserves_bytes_and_management(initialized, tmp_path):
    dsn, output = initialized
    # Management deliberately stays on the old independent command/path.
    deploy = tmp_path / "deploy"
    deploy.mkdir(mode=0o700)
    management = deploy / ".env"
    with pytest.raises(ValueError):
        init_identities(dsn, management)
    assert not management.exists()
    before = snapshot(output.parent)
    assert not core.init_core_identities(dsn, output)
    assert snapshot(output.parent) == before
    assert not management.exists()
    assert count_principals(dsn) == 6
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    for path in output.parent.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1
    credentials = load(output)
    with connection(dsn) as conn, writer(conn):
        assert conn.execute("SELECT count(*) FROM storage.consumers").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM storage.consumer_identities").fetchone() == (4,)
        for role, p in core.core_identity_policies().items():
            principal = authenticate(conn, token_digest(credentials[role]["token"]))
            assert principal.module == p.module and not principal.admin
            assert principal.read_types == p.reads
            assert principal.subscribe_types == p.subscriptions
            for name in p.consumers:
                assert can_use_consumer(conn, principal, name)
            assert not can_use_consumer(conn, principal, "storage.plugin_management")


@pytest.mark.postgres
def test_existing_management_first_is_preserved(pg_dsn, tmp_path):
    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    deploy = tmp_path / "deploy"
    deploy.mkdir(mode=0o700)
    management = deploy / ".env"
    init_identities(pg_dsn, management)
    before = snapshot(deploy)
    output = tmp_path / "new-core" / "identities.json"
    assert core.init_core_identities(pg_dsn, output)
    assert not core.init_core_identities(pg_dsn, output)
    assert snapshot(deploy) == before
    assert count_principals(pg_dsn) == 8


@pytest.mark.postgres
def test_strict_scheduler_status_and_least_privilege_over_http(initialized):
    dsn, output = initialized
    credentials = load(output)
    with TestClient(create_app(StorageService(dsn))) as http:

        def request(role, path):
            return http.get(path, headers={"Authorization": "Bearer " + credentials[role]["token"]})

        response = request("dependency", "/v1/scheduler/state")
        assert response.status_code == 200
        state = response.json()
        assert state["coverage_complete"] is True
        for module in ("attribution", "extraction"):
            assert state["owners"][module]["can_receive"] is True
            assert state["owners"][module]["online"] is None
        for role in ("adapter", "attribution", "extraction", "api_user", "llm_gateway"):
            assert request(role, "/v1/scheduler/state").status_code == 403
    service = StorageService(dsn)
    for role, policy in core.core_identity_policies().items():
        client = service.client(credentials[role]["token"])
        for dtype in policy.reads:
            assert client.query(Query(dtype)).items == ()
        for dtype in ("plugin_management_feedback", "plugin_management_result"):
            with pytest.raises(PermissionDenied):
                client.query(Query(dtype))
        if role != "llm_gateway":
            with pytest.raises(PermissionDenied):
                client.query(Query("llm_call"))
    # Missing one scheduler grant is NOT partial coverage or auto-repaired.
    dep = credentials["dependency"]
    with connection(dsn) as conn, writer(conn):
        conn.execute(
            "DELETE FROM storage.type_grants WHERE principal_id=%s AND data_type='person'",
            (dep["principal_id"],),
        )
    with pytest.raises(PermissionDenied):
        service.client(dep["token"]).scheduler_state()
    before = snapshot(output.parent)
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(dsn, output)
    assert snapshot(output.parent) == before


@pytest.mark.postgres
def test_reserved_marker_cannot_be_claimed_by_same_module_peer(initialized):
    dsn, output = initialized
    credentials = load(output)
    owner = StorageService(dsn).client(credentials["extraction"]["token"])
    from ear_contracts import utcnow
    from ear_storage_client import AppendRaw
    from ear_storage_client.testing import make_raw

    api = StorageService(dsn).client(credentials["api_user"]["token"])
    result = api.write(
        [AppendRaw(make_raw("feedback", {}, start=utcnow()))], Context.system()
    ).check()
    event_id = result.events[0].event_id
    owner.write([MarkProcessed("extraction.completion", event_id)], Context.system()).check()
    peer = AuthOperator(dsn).issue_principal("extraction")
    other = StorageService(dsn).client(peer.token)
    with pytest.raises(PermissionDenied):
        other.write([MarkProcessed("extraction.completion", event_id)], Context.system())
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(dsn, output)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "mutation",
    [
        "extra_grant",
        "missing_grant",
        "subscription",
        "revoked",
        "rotated",
        "admin",
        "consumer_missing",
        "consumer_other",
        "consumer_extra",
        "new_plugin",
    ],
)
def test_policy_drift_never_repairs_or_reissues(initialized, mutation):
    dsn, output = initialized
    credentials = load(output)
    pid = credentials["extraction"]["principal_id"]
    before = snapshot(output.parent)
    operator = AuthOperator(dsn)
    with connection(dsn) as conn, writer(conn):
        if mutation == "extra_grant":
            conn.execute("INSERT INTO storage.type_grants VALUES (%s,'llm_call',false)", (pid,))
        elif mutation == "missing_grant":
            conn.execute(
                "DELETE FROM storage.type_grants WHERE principal_id=%s AND data_type='rule'", (pid,)
            )
        elif mutation == "subscription":
            conn.execute(
                "UPDATE storage.type_grants SET can_subscribe=false WHERE principal_id=%s", (pid,)
            )
        elif mutation == "admin":
            conn.execute(
                "UPDATE storage.principals SET admin=true WHERE id=%s",
                (credentials["api_user"]["principal_id"],),
            )
        elif mutation == "consumer_missing":
            conn.execute(
                "DELETE FROM storage.consumer_identities WHERE name='extraction.completion'"
            )
        elif mutation == "consumer_other":
            conn.execute(
                "UPDATE storage.consumer_identities SET principal_id=%s "
                "WHERE name='extraction.jobs'",
                (credentials["adapter"]["principal_id"],),
            )
        elif mutation == "consumer_extra":
            conn.execute(
                "INSERT INTO storage.consumer_identities VALUES ('extraction.extra',%s)", (pid,)
            )
    if mutation == "revoked":
        operator.revoke(pid)
    elif mutation == "rotated":
        operator.rotate(pid)
    elif mutation == "new_plugin":
        bootstrap(
            dsn, core.core_mvp_registry().extend(DataTypeSpec("plugin_note", "semantic", "plugin"))
        )
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(dsn, output)
    assert snapshot(output.parent) == before
    assert count_principals(dsn) == 6


@pytest.mark.postgres
@pytest.mark.parametrize(
    "module", ["adapter", "attribution", "extraction", "dependency", "api", "llm_gateway"]
)
def test_missing_material_does_not_reissue_existing_identity(pg_dsn, tmp_path, module):
    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    issued = AuthOperator(pg_dsn).issue_principal(module)
    output = tmp_path / "unrelated-new-dir" / "identities.json"
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(pg_dsn, output)
    assert not output.exists()
    assert count_principals(pg_dsn) == 1
    with connection(pg_dsn) as conn, writer(conn):
        assert authenticate(conn, token_digest(issued.token)).module == module


@pytest.mark.postgres
@pytest.mark.parametrize(
    "stage", ["issue_before", "issue_after", "receipt", "reserve_before", "reserve_after", "final"]
)
def test_unknown_and_partial_outcomes_preserve_recovery_materials(
    pg_dsn, tmp_path, monkeypatch, stage
):
    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    output = tmp_path / "fault" / "identities.json"
    issue = AuthOperator.issue_principal
    reserve = AuthOperator.reserve_consumer_identity
    publish = core._Bundle.publish

    def issue_fault(self, module, **kwargs):
        if module == "attribution" and stage == "issue_before":
            raise RuntimeError("synthetic lost response")
        result = issue(self, module, **kwargs)
        if module == "attribution" and stage == "issue_after":
            raise RuntimeError("synthetic lost response")
        return result

    def reserve_fault(self, name, pid):
        if stage == "reserve_before":
            raise RuntimeError("synthetic failure")
        reserve(self, name, pid)
        if stage == "reserve_after":
            raise RuntimeError("synthetic lost response")

    def publish_fault(self, name, value):
        if (stage == "receipt" and name == "attribution.json") or (
            stage == "final" and name == output.name
        ):
            raise OSError("synthetic disk failure")
        publish(self, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(AuthOperator, "issue_principal", issue_fault)
        patch.setattr(AuthOperator, "reserve_consumer_identity", reserve_fault)
        patch.setattr(core._Bundle, "publish", publish_fault)
        with pytest.raises(core.CoreDeploymentError):
            core.init_core_identities(pg_dsn, output)
    assert not output.exists()
    assert (output.parent / "intent.json").is_file()
    assert (output.parent / "adapter.json").is_file()
    before = snapshot(output.parent)
    count = count_principals(pg_dsn)
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(pg_dsn, output)
    assert snapshot(output.parent) == before
    assert count_principals(pg_dsn) == count


@pytest.mark.postgres
@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "duplicates",
        "plan",
        "token",
        "id",
        "intent",
        "receipt",
        "missing",
        "extra",
        "mode",
        "hardlink",
        "symlink",
        "fifo",
    ],
)
def test_corrupt_or_unsafe_bundle_is_preserved(initialized, damage):
    dsn, output = initialized
    if damage == "truncated":
        output.write_text('{"credentials":')
    elif damage == "duplicates":
        output.write_text('{"codec":1,"codec":1}')
    elif damage in {"plan", "token", "id"}:
        data = json.loads(output.read_text())
        if damage == "plan":
            data["plan"]["identities"]["adapter"]["admin"] = True
        elif damage == "token":
            data["credentials"]["adapter"]["token"] = "!" * 43
        else:
            data["credentials"]["adapter"]["principal_id"] = new_id()
        output.write_text(json.dumps(data))
    elif damage == "intent":
        (output.parent / "intent.json").write_text("{}")
    elif damage == "receipt":
        (output.parent / "extraction.json").write_text("{}")
    elif damage == "missing":
        output.unlink()
    elif damage == "extra":
        (output.parent / "operator-evidence.json").write_text("{}")
    elif damage == "mode":
        output.chmod(0o640)
    elif damage == "hardlink":
        os.link(output, output.parent.parent / "linked.json")
    else:
        saved = output.parent.parent / "saved.json"
        output.rename(saved)
        if damage == "symlink":
            output.symlink_to(saved)
        else:
            os.mkfifo(output, 0o600)
    before = snapshot(output.parent)
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(dsn, output)
    assert snapshot(output.parent) == before
    assert count_principals(dsn) == 6
    if damage == "mode":
        assert stat.S_IMODE(output.stat().st_mode) == 0o640
    if damage == "fifo":
        assert stat.S_ISFIFO(output.lstat().st_mode)
    if damage == "symlink":
        assert output.is_symlink()


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "dotdot",
        "extension",
        "reserved",
        "ancestor_link",
        "parent_link",
        "writable_ancestor",
        "private_mode",
        "existing_empty",
        "parent_file",
    ],
)
def test_unsafe_paths_fail_before_issuance(tmp_path, monkeypatch, case):
    forbidden = Mock(side_effect=AssertionError("must not issue credentials"))
    monkeypatch.setattr(AuthOperator, "issue_principal", forbidden)
    output = tmp_path / "new" / "identities.json"
    if case == "relative":
        output = Path("relative/identities.json")
    elif case == "dotdot":
        output = tmp_path / ".." / "new" / "identities.json"
    elif case == "extension":
        output = tmp_path / "new" / "identities.txt"
    elif case == "reserved":
        output = tmp_path / "new" / "intent.json"
    elif case == "ancestor_link":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        output = link / "new" / "identities.json"
    elif case == "parent_link":
        output.parent.symlink_to(tmp_path, target_is_directory=True)
    elif case == "writable_ancestor":
        public = tmp_path / "writable"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        output = public / "new" / "identities.json"
    elif case == "parent_file":
        output.parent.write_text("keep")
    else:
        output.parent.mkdir(mode=0o700 if case == "existing_empty" else 0o755)
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities("host=127.0.0.1 port=1 connect_timeout=1", output)
    forbidden.assert_not_called()


def test_directory_owner_and_sticky_policy(tmp_path):
    info = tmp_path.stat()
    fields = list(info)
    fields[4] = os.getuid() + 1000  # synthetic foreign uid, not a chown of real files
    with pytest.raises(core.CoreDeploymentError):
        core._directory(os.stat_result(fields))
    fields = list(info)
    fields[0] = stat.S_IFDIR | 0o1777
    fields[4] = 0
    core._directory(os.stat_result(fields))
    with pytest.raises(core.CoreDeploymentError):
        core._directory(os.stat_result(fields), private=True)


def test_descriptor_chain_detects_renamed_ancestor_and_never_writes_replacement(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    output = parent / "new" / "identities.json"
    with core._Bundle(output) as bundle:
        bundle.publish("intent.json", {"test": "preserved"})
        parent.rename(tmp_path / "moved")
        parent.mkdir(mode=0o700)
        (parent / "new").mkdir(mode=0o700)
        with pytest.raises(core.CoreDeploymentError):
            bundle.publish("token.json", {"test": "must not be written"})
    assert not list((parent / "new").iterdir())
    assert (tmp_path / "moved" / "new" / "intent.json").exists()


def test_exclusive_publish_never_overwrites_evidence(tmp_path):
    with core._Bundle(tmp_path / "new" / "identities.json") as bundle:
        bundle.publish("intent.json", {"first": True})
        with pytest.raises(FileExistsError):
            bundle.publish("intent.json", {"second": True})
        assert bundle.read("intent.json") == {"first": True}


def test_private_directory_lock_is_exclusive(tmp_path):
    output = tmp_path / "new" / "identities.json"
    with core._Bundle(output), pytest.raises(BlockingIOError), core._Bundle(output):
        pytest.fail("must not take an already held directory lock")


@pytest.mark.postgres
def test_cooperative_database_lock_rejects_other_output(pg_dsn, tmp_path):
    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    with core._initialization(pg_dsn):
        with pytest.raises(core.CoreDeploymentError):
            core.init_core_identities(pg_dsn, tmp_path / "blocked" / "identities.json")
        with pytest.raises(core.CoreDeploymentError):
            core.bootstrap_core(pg_dsn)
    assert count_principals(pg_dsn) == 0


@pytest.mark.postgres
def test_existing_legacy_markers_are_not_adopted(pg_dsn, tmp_path):
    from ear_storage import PostgresStorage

    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    PostgresStorage(pg_dsn).client("extraction").write(
        [MarkProcessed("extraction.completion", new_id())], Context.system()
    ).check()
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(pg_dsn, tmp_path / "legacy" / "identities.json")
    assert count_principals(pg_dsn) == 0


@pytest.mark.parametrize("command", ["bootstrap-core", "init-core-identities"])
def test_cli_only_reports_nonsecret_created_status(monkeypatch, capsys, tmp_path, command):
    stub = Mock(return_value=True)
    name = "bootstrap_core" if command == "bootstrap-core" else "init_core_identities"
    monkeypatch.setattr(core, name, stub)
    monkeypatch.setenv("EAR_STORAGE_DSN", "synthetic-secret-dsn")
    argv = ["ear-storage", command]
    output = tmp_path / "new" / "identities.json"
    if command == "init-core-identities":
        argv += ["--output", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert capsys.readouterr().out == '{"status": "created"}\n'
    if command == "bootstrap-core":
        stub.assert_called_once_with("synthetic-secret-dsn")
    else:
        stub.assert_called_once_with("synthetic-secret-dsn", output)
    stub.side_effect = RuntimeError("NEVER_LOG_THIS_SECRET")
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "NEVER_LOG_THIS_SECRET" not in captured.out + captured.err
    assert "synthetic-secret-dsn" not in captured.out + captured.err


def test_cli_requires_explicit_output_and_dsn(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ear-storage", "init-core-identities"])
    monkeypatch.delenv("EAR_STORAGE_DSN", raising=False)
    with pytest.raises(SystemExit):
        cli.main()
    assert "EAR_STORAGE_DSN is required" in capsys.readouterr().err
    monkeypatch.setenv("EAR_STORAGE_DSN", "synthetic")
    with pytest.raises(SystemExit):
        cli.main()
    assert "requires --output" in capsys.readouterr().err


@pytest.mark.postgres
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "wrong_owner",
        "disabled",
        "not_ready",
        "missing_filter",
        "extra_filter",
        "wrong_kinds",
        "history",
        "extra_name",
    ],
)
def test_actual_registered_consumer_is_verified_not_just_reservation(initialized, mutation):
    dsn, output = initialized
    credentials = load(output)
    dep = credentials["dependency"]
    with connection(dsn) as conn, writer(conn):
        principal = authenticate(conn, token_digest(dep["token"]))
        config = {
            "event_types": ["data_changed"],
            "from_beginning": False,
            "filters": approved_filters(
                principal, core.core_mvp_registry(), ["data_changed"], "synthetic.events"
            ),
        }
        if mutation == "missing_filter":
            config["filters"].pop()
        elif mutation == "extra_filter":
            config["filters"].append("synthetic.events.data_changed.plugin_management_feedback.*")
        elif mutation == "wrong_kinds":
            config["event_types"] = ["reevaluation_requested"]
        elif mutation == "history":
            config["from_beginning"] = True
        conn.execute(
            "INSERT INTO storage.consumers(name,principal_id,module,generation,config_text,"
            "destination,creation_cursor,ready,enabled) VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s)",
            (
                "dependency.extra" if mutation == "extra_name" else "dependency.changes",
                credentials["adapter"]["principal_id"]
                if mutation == "wrong_owner"
                else principal.id,
                "dependency",
                new_id(),
                json.dumps(config),
                "synthetic-no-broker",
                mutation != "not_ready",
                mutation != "disabled",
            ),
        )
    before = snapshot(output.parent)
    if mutation == "none":
        assert not core.init_core_identities(dsn, output)
    else:
        with pytest.raises(core.CoreDeploymentError):
            core.init_core_identities(dsn, output)
    assert snapshot(output.parent) == before
    assert count_principals(dsn) == 6


@pytest.mark.postgres
def test_issued_extraction_gateway_caller_and_distinct_writer_over_http(initialized):
    """Real SDK/ASGI/PG permissions and request preflight; no model or broker."""
    from datetime import timedelta

    from ear_contracts import Cause, LLMCallRecord, VersionRef, utcnow
    from ear_storage_client import (
        Create,
        LLMCallPreflight,
        PublishReevaluation,
        PutSystemRecord,
        RegisterDependencies,
    )
    from ear_storage_client.remote import RemoteStorageClient

    from .test_http import TestTransport

    dsn, output = initialized
    credentials = load(output)
    service = StorageService(dsn)
    attr = service.client(credentials["attribution"]["token"])
    person, artifact, request_id = new_id(), new_id(), new_id()
    attr.write([Create("person", person, {"name": "synthetic"})], Context.system()).check()
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(
            credentials["extraction"]["token"],
            "http://synthetic.test",
            transport=TestTransport(http),
        ) as extraction,
        RemoteStorageClient(
            credentials["llm_gateway"]["token"],
            "http://synthetic.test",
            transport=TestTransport(http),
        ) as gateway,
    ):
        assert extraction.module == "extraction" and gateway.module == "llm_gateway"
        extraction.write(
            [
                Create("artifact", artifact, {"body_md": "Synthetic note"}),
                RegisterDependencies(
                    "artifact", artifact, [VersionRef(data_type="person", id=person, version=1)]
                ),
            ],
            Context.system(),
        ).check()
        service.client(credentials["dependency"]["token"]).write(
            [
                PublishReevaluation(
                    "artifact",
                    artifact,
                    Cause(data_type="person", data_id=person, old_version=None, new_version=1),
                    "synthetic cause",
                    event_id=request_id,
                )
            ],
            Context("system", 1),
        ).check()
        now = utcnow()
        preflight = extraction.preflight_llm_call(
            LLMCallPreflight(
                caller_module="extraction",
                request_event_id=request_id,
                root_feedback_id="system",
                depth=1,
                started_at=now,
            )
        )
        assert preflight.request_event_id == request_id
        assert any(t.cause.data_id == person for t in preflight.additional_causes)
        record = LLMCallRecord.model_validate(
            {
                "caller_module": "extraction",
                "purpose": "synthetic_test",
                "model": "synthetic/model",
                "started_at": now,
                "ended_at": now + timedelta(milliseconds=1),
                "latency_ms": 1,
                "status": "success",
                "usage": None,
                "root_feedback_id": "system",
                "depth": 1,
                "request_event_id": request_id,
                "additional_causes": preflight.additional_causes,
            }
        ).model_dump(mode="json")
        call_id = new_id()
        assert gateway.call_id_occupied(call_id) is False
        result = gateway.write(
            [PutSystemRecord("llm_call", call_id, record)], Context("system", 1)
        ).check()
        assert result.events == ()
        saved = gateway.get_system_record(call_id)
        assert saved is not None and saved.writer == "llm_gateway"
        assert saved.payload["request_event_id"] == request_id
        assert "request_text" not in saved.payload and "response_text" not in saved.payload
        with pytest.raises(PermissionDenied):
            extraction.write([PutSystemRecord("llm_call", new_id(), record)], Context("system", 1))
        with pytest.raises(PermissionDenied):
            gateway.write([Create("person", new_id(), {})], Context.system())
    assert not core.init_core_identities(dsn, output)


@pytest.mark.postgres
def test_partial_file_publication_is_retained_and_not_resumed(pg_dsn, tmp_path, monkeypatch):
    migrate(pg_dsn)
    core.bootstrap_core(pg_dsn)
    output = tmp_path / "partial-file" / "identities.json"
    publish = core._Bundle.publish

    def fault(self, name, value):
        if name == "extraction.json":
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
            try:
                os.write(fd, b'{"partial":')
                os.fsync(fd)
            finally:
                os.close(fd)
            raise OSError("synthetic interrupted write")
        publish(self, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(core._Bundle, "publish", fault)
        with pytest.raises(core.CoreDeploymentError):
            core.init_core_identities(pg_dsn, output)
    before = snapshot(output.parent)
    assert count_principals(pg_dsn) == 3
    with pytest.raises(core.CoreDeploymentError):
        core.init_core_identities(pg_dsn, output)
    assert snapshot(output.parent) == before
    assert count_principals(pg_dsn) == 3


@pytest.mark.postgres
def test_bundle_from_other_database_does_not_issue_into_empty_store(initialized, postgres_server):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from .conftest import TestDsn

    _, output = initialized
    database = "ear_storage_it_" + new_id().replace("-", "")
    with connection(postgres_server) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        dsn = TestDsn(make_conninfo(postgres_server, dbname=database))
        migrate(dsn)
        core.bootstrap_core(dsn)
        before = snapshot(output.parent)
        with pytest.raises(core.CoreDeploymentError):
            core.init_core_identities(dsn, output)
        assert count_principals(dsn) == 0
        assert snapshot(output.parent) == before
    finally:
        with connection(postgres_server) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
