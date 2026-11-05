"""P3 credentials: real PG gates and HTTP, no production data or credentials."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from ear_contracts import new_id
from ear_storage import schema
from ear_storage.auth import AuthenticationError, token_digest
from ear_storage.credentials import claim_credentials
from ear_storage.http import create_app
from ear_storage.management import FEEDBACK, RESULT, submit
from ear_storage.management_input import ManagementInput
from ear_storage.schema import connection
from ear_storage_client import Context, Create, InvalidRequest, PermissionDenied, Query
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .test_management import command, declaration, run
from .test_management import mgmt as mgmt

SYS = Context.system()


def headers(token):
    return {"Authorization": "Bearer " + token}


def claim(http, admin, accepted):
    return http.post(
        "/v1/management/credentials/" + accepted["feedback_id"] + "/claim",
        headers=headers(admin.token),
    )


def test_credential_actions_are_closed_and_legacy_payloads_unchanged():
    for action in ("rotate_credentials", "revoke_credentials"):
        body = command(action).payload()
        assert ManagementInput.model_validate(body).payload() == body
        for extra in (
            {"token": "sensitive"},
            {"token_hash": "sensitive"},
            {"admin": True},
            {"read_types": ["activity"]},
            {"target_feedback_id": new_id()},
        ):
            with pytest.raises(ValidationError):
                ManagementInput.model_validate(body | extra)


@pytest.mark.postgres
def test_first_issue_pending_without_generation_or_secrets_and_disabled_claim(
    mgmt, monkeypatch, caplog
):
    dsn, backend, service, _, admin, processor = mgmt
    run(mgmt, declaration())
    body = command("rotate_credentials")
    accepted = submit(service.client(admin.token), body)
    with TestClient(create_app(service)) as http:
        assert claim(http, admin, accepted).status_code == 403
        with monkeypatch.context() as patch:

            def no_generation(*args, **kwargs):
                raise AssertionError("worker must not generate credential material")

            patch.setattr("ear_storage.credentials.secrets.token_urlsafe", no_generation)
            final = processor.process(accepted["feedback_id"])
        assert final["status"] == "succeeded"
        with connection(dsn) as conn:
            row = conn.execute(
                "SELECT id,token_hash,active,credential_epoch FROM storage.principals "
                "WHERE module='sample'"
            ).fetchone()
            assert row and row[1:] == (None, False, 1)
            assert conn.execute(
                "SELECT status,claimed_at FROM storage.credential_exchanges"
            ).fetchone() == ("pending", None)
        response = claim(http, admin, accepted)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        pid, token = response.json()["principal_id"], response.json()["token"]
        assert pid == row[0]
        assert claim(http, admin, accepted).status_code == 403
        assert http.get("/v1/identity", headers=headers(token)).status_code == 401
        with connection(dsn) as conn:
            assert conn.execute(
                "SELECT token_hash,active FROM storage.principals WHERE id=%s", (pid,)
            ).fetchone() == (token_digest(token), True)
            exchanges = str(conn.execute("SELECT * FROM storage.credential_exchanges").fetchall())
        records = service.client(admin.token).query(Query(RESULT))
        exposed = repr(records) + repr(final) + repr(backend.outbox()) + exchanges + caplog.text
        assert token not in exposed and token_digest(token) not in exposed
        assert processor.process(accepted["feedback_id"]) == final
        assert submit(service.client(admin.token), body) == accepted
        assert claim(http, admin, accepted).status_code == 403
        with pytest.raises(AuthenticationError):
            service.client(token).write([Create("sample_item", new_id(), {})], SYS)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "denial",
    [
        "nonadmin",
        "different_admin",
        "role",
        "principal",
        "expired",
        "revision",
        "resources",
        "revoke",
        "new_rotate",
        "operator_revoke",
        "failed",
    ],
)
def test_claim_denial_live_authority_epoch_version_and_ttl(mgmt, denial):
    dsn, _, service, operator, admin, _ = mgmt
    run(mgmt, declaration())
    accepted, _ = run(mgmt, command("rotate_credentials"))
    claimant = admin
    if denial == "nonadmin":
        claimant = operator.issue_principal("api")
    elif denial == "different_admin":
        claimant = operator.issue_principal("api", admin=True)
    elif denial == "role":
        operator.set_admin(admin.principal_id, admin=False)
    elif denial == "principal":
        operator.revoke(admin.principal_id)
    elif denial == "revoke":
        run(mgmt, command("revoke_credentials"))
    elif denial == "new_rotate":
        run(mgmt, command("rotate_credentials"))
    elif denial == "operator_revoke":
        with connection(dsn) as conn:
            row = conn.execute("SELECT id FROM storage.principals WHERE module='sample'").fetchone()
            assert row
        operator.revoke(row[0])
    else:
        with connection(dsn) as conn:
            if denial == "expired":
                conn.execute("UPDATE storage.credential_exchanges SET expires_at='-infinity'")
            elif denial == "revision":
                conn.execute("UPDATE storage.module_lifecycle SET revision=revision+1")
            elif denial == "resources":
                conn.execute("UPDATE storage.module_lifecycle SET resources_ready=false")
            elif denial == "failed":
                conn.execute(
                    "UPDATE storage.management_tasks SET status='failed' WHERE feedback_id=%s",
                    (accepted["feedback_id"],),
                )
    with TestClient(create_app(service)) as http:
        result = claim(http, claimant, accepted)
        assert result.status_code == (401 if denial == "principal" else 403)
        assert "no-store" in result.headers["cache-control"]
        assert "token" not in result.text
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.credential_exchanges WHERE claimed_at IS NOT NULL"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM storage.principals WHERE module='sample' AND active"
        ).fetchone() == (0,)


@pytest.mark.postgres
def test_concurrent_claim_exactly_one_and_lost_response_requires_new_rotate(mgmt):
    dsn, _, service, _, admin, _ = mgmt
    run(mgmt, declaration())
    accepted, _ = run(mgmt, command("rotate_credentials"))
    with TestClient(create_app(service)) as http:
        with ThreadPoolExecutor(5) as pool:
            replies = list(pool.map(lambda _: claim(http, admin, accepted), range(5)))
        assert sorted(r.status_code for r in replies) == [200, 403, 403, 403, 403]
        old = next(r for r in replies if r.status_code == 200).json()
        # Treat the response as lost: no same-request plaintext retrieval/generation.
        assert claim(http, admin, accepted).status_code == 403
        replacement, _ = run(mgmt, command("rotate_credentials"))
        issued = claim(http, admin, replacement).json()
        assert issued["principal_id"] == old["principal_id"]
        assert issued["token"] != old["token"]
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.principals WHERE module='sample'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM storage.credential_exchanges WHERE status='claimed'"
        ).fetchone() == (2,)


@pytest.mark.postgres
def test_rotation_does_not_restore_withdrawn_grants_or_enable_and_single_identity(mgmt):
    dsn, _, service, operator, admin, _ = mgmt
    run(mgmt, declaration())
    accepted, _ = run(mgmt, command("rotate_credentials"))
    first = claim_credentials(service.client(admin.token), accepted["feedback_id"])
    operator.set_grants(first.principal_id, read_types=["sample_item"])
    with pytest.raises(InvalidRequest):
        operator.issue_principal("sample")
    run(mgmt, command("revoke_credentials"))
    # Grant changes are not credential recovery.
    operator.set_grants(first.principal_id, read_types=["sample_item"])
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT active FROM storage.principals WHERE id=%s", (first.principal_id,)
        ).fetchone() == (False,)
    accepted, _ = run(mgmt, command("rotate_credentials"))
    second = claim_credentials(service.client(admin.token), accepted["feedback_id"])
    assert first.principal_id == second.principal_id
    assert first.token not in repr(first) and second.token not in repr(second)
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT data_type FROM storage.type_grants WHERE principal_id=%s",
            (second.principal_id,),
        ).fetchall() == [("sample_item",)]
        assert conn.execute("SELECT enabled FROM storage.module_lifecycle").fetchone() == (False,)
        epoch = conn.execute(
            "SELECT credential_epoch FROM storage.principals WHERE id=%s", (second.principal_id,)
        ).fetchone()
    body = command("revoke_credentials")
    accepted, _ = run(mgmt, body)
    run(mgmt, body)
    with connection(dsn) as conn:
        assert epoch
        assert conn.execute(
            "SELECT credential_epoch FROM storage.principals WHERE id=%s", (second.principal_id,)
        ).fetchone() == (epoch[0] + 1,)
    assert not hasattr(service.client(admin.token), "rotate")


@pytest.mark.postgres
@pytest.mark.parametrize("module", ["api", "storage", "extraction", "unregistered"])
def test_feedback_cannot_rotate_core_or_unknown_principals(mgmt, module):
    _, _, service, operator, admin, _ = mgmt
    core = None
    if module != "unregistered":
        core = operator.issue_principal(module)
    accepted, result = run(mgmt, command("rotate_credentials", module))
    assert result["status"] == "failed"
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), accepted["feedback_id"])
    if core is not None:
        assert service.client(core.token).module == module
    _, result = run(mgmt, command("revoke_credentials", module))
    assert result["status"] == "failed"


@pytest.mark.postgres
def test_core_operator_rotate_preserves_identity_receipts_and_snapshot(mgmt):
    dsn, backend, service, operator, _, _ = mgmt
    core = operator.issue_principal("extraction", read_types=["activity"])
    client = service.client(core.token)
    rid, request = new_id(), new_id()
    ops = [Create("activity", rid, {})]
    receipt = client.write(ops, SYS, request_id=request)
    lease = client.create_snapshot()
    rotated = operator.rotate(core.principal_id)
    assert rotated.principal_id == core.principal_id
    with pytest.raises(AuthenticationError):
        client.get(rid)
    current = service.client(rotated.token)
    assert current.read_snapshot(lease.snapshot_id).get(rid)
    assert current.write(ops, SYS, request_id=request) == receipt
    assert len(backend.history(rid)) == 1
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT scope FROM storage.idempotency WHERE request_id=%s", (request,)
        ).fetchone() == ("principal:" + core.principal_id,)
    operator.revoke(core.principal_id)
    with pytest.raises(AuthenticationError):
        current.get(rid)
    recovered = operator.rotate(core.principal_id)
    assert recovered.principal_id == core.principal_id
    assert service.client(recovered.token).get(rid)


@pytest.mark.postgres
def test_stage_result_failure_rolls_back_invalidation_and_exchange(mgmt, monkeypatch):
    dsn, _, service, operator, admin, processor = mgmt
    run(mgmt, declaration())
    plugin = operator.issue_principal("sample")
    accepted = submit(service.client(admin.token), command("rotate_credentials"))
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic result failure")

        patch.setattr(processor, "_record", fail)
        with pytest.raises(RuntimeError):
            processor.process(accepted["feedback_id"])
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT token_hash,active,credential_epoch FROM storage.principals WHERE id=%s",
            (plugin.principal_id,),
        ).fetchone() == (token_digest(plugin.token), True, 0)
        assert conn.execute("SELECT count(*) FROM storage.credential_exchanges").fetchone() == (0,)
    assert processor.process(accepted["feedback_id"])["status"] == "succeeded"


@pytest.mark.postgres
def test_unknown_revoke_is_idempotent_and_schema7_has_no_secret_exchange_columns(mgmt):
    dsn, _, _, _, _, _ = mgmt
    run(mgmt, declaration())
    body = command("revoke_credentials")
    first = run(mgmt, body)
    assert first == run(mgmt, body) and first[1]["status"] == "succeeded"
    with connection(dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            schema.SCHEMA_VERSION,
        )
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='storage' AND table_name='credential_exchanges'"
            )
        }
        assert columns == {
            "feedback_id",
            "principal_id",
            "credential_epoch",
            "module_revision",
            "expires_at",
            "claimed_at",
            "status",
        }
        assert conn.execute(
            "SELECT count(*) FROM storage.principals WHERE module='sample'"
        ).fetchone() == (0,)
    assert "token" not in json.dumps(first)


@pytest.mark.postgres
def test_claim_transaction_failure_never_commits_partial_hash_or_claim(mgmt, monkeypatch):
    dsn, _, service, _, admin, _ = mgmt
    run(mgmt, declaration())
    accepted, _ = run(mgmt, command("rotate_credentials"))
    # Abort after both updates but before COMMIT; neither mutation may survive.
    from contextlib import contextmanager

    client = service.client(admin.token)
    session = client._session

    @contextmanager
    def aborted():
        with session() as values:
            yield values
            raise RuntimeError("synthetic precommit failure")

    with monkeypatch.context() as patch:
        patch.setattr(client, "_session", aborted)
        with pytest.raises(RuntimeError):
            claim_credentials(client, accepted["feedback_id"])
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT token_hash,active FROM storage.principals WHERE module='sample'"
        ).fetchone() == (None, False)
        assert conn.execute(
            "SELECT status,claimed_at FROM storage.credential_exchanges"
        ).fetchone() == ("pending", None)
    assert claim_credentials(client, accepted["feedback_id"])


@pytest.mark.postgres
def test_legacy_six_upgrade_preserves_management_authority_and_principal(pg_dsn, monkeypatch):
    from typing import LiteralString, cast

    from ear_storage.auth import AuthOperator
    from ear_storage.backend import PostgresStorage
    from ear_storage.management import ManagementProcessor
    from ear_storage.schema import bootstrap, migrate, writer
    from ear_storage.service import StorageService

    with connection(pg_dsn) as conn, writer(conn):
        for statement in (
            schema.DDL
            + schema.DDL_V2
            + schema.DDL_V3
            + schema.DDL_V4
            + schema.DDL_V5
            + schema.DDL_V6
        ):
            conn.execute(cast(LiteralString, statement))
        conn.execute("INSERT INTO storage.meta VALUES (true,6,0)")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 6)
        bootstrap(pg_dsn)
        backend = PostgresStorage(pg_dsn)
        service = StorageService(pg_dsn)
        operator = AuthOperator(pg_dsn)
        admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
        processor = ManagementProcessor(backend)
        legacy = (pg_dsn, backend, service, operator, admin, processor)
        body = declaration()
        accepted, result = run(legacy, body)
        plugin = operator.issue_principal(
            "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
        )
        with connection(pg_dsn) as conn:
            before = conn.execute("SELECT * FROM storage.management_authority").fetchall()
            grants = conn.execute("SELECT * FROM storage.type_grants ORDER BY 1,2").fetchall()
    migrate(pg_dsn)
    migrate(pg_dsn)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            schema.SCHEMA_VERSION,
        )
        assert conn.execute("SELECT * FROM storage.management_authority").fetchall() == before
        assert conn.execute("SELECT * FROM storage.type_grants ORDER BY 1,2").fetchall() == grants
        assert conn.execute(
            "SELECT token_hash,credential_epoch FROM storage.principals WHERE id=%s",
            (plugin.principal_id,),
        ).fetchone() == (token_digest(plugin.token), 0)
    assert run(legacy, body) == (accepted, result)
    rotated, final = run(legacy, command("rotate_credentials"))
    assert final["status"] == "succeeded"
    issued = claim_credentials(service.client(admin.token), rotated["feedback_id"])
    assert issued.principal_id == plugin.principal_id


@pytest.mark.postgres
def test_legacy_ambiguous_plugin_principals_fail_closed(mgmt):
    dsn, _, service, operator, admin, _ = mgmt
    run(mgmt, declaration())
    first = operator.issue_principal("sample")
    # Reproduce an actual legacy-v6 multiple-principal database, without guessing an owner.
    import secrets

    with connection(dsn) as conn:
        conn.execute(
            "INSERT INTO storage.principals(id,module,token_hash) VALUES (%s,'sample',%s)",
            (new_id(), token_digest(secrets.token_urlsafe(32))),
        )
    accepted, failed = run(mgmt, command("rotate_credentials"))
    assert failed["status"] == "failed"
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), accepted["feedback_id"])
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT active,token_hash FROM storage.principals WHERE id=%s", (first.principal_id,)
        ).fetchone() == (True, token_digest(first.token))

    _, revoked = run(mgmt, command("revoke_credentials"))
    assert revoked["status"] == "succeeded"
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM storage.principals WHERE module='sample' AND active"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM storage.principals WHERE module='sample'"
        ).fetchone() == (2,)


@pytest.mark.postgres
def test_revoke_blocks_even_when_resources_not_ready(mgmt):
    dsn, _, service, _, admin, _ = mgmt
    run(mgmt, declaration())
    pending, _ = run(mgmt, command("rotate_credentials"))
    with connection(dsn) as conn:
        conn.execute("UPDATE storage.module_lifecycle SET resources_ready=false")
    _, result = run(mgmt, command("revoke_credentials"))
    assert result["status"] == "succeeded"
    # Repairing resources must not resurrect the old authorization.
    with connection(dsn) as conn:
        conn.execute("UPDATE storage.module_lifecycle SET resources_ready=true")
    with pytest.raises(PermissionDenied):
        claim_credentials(service.client(admin.token), pending["feedback_id"])
