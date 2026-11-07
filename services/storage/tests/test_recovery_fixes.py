"""Bounded recovery regressions against real isolated PostgreSQL and JetStream."""

import pytest
from ear_contracts import VersionRef, new_id
from ear_storage.history_execution import claim, commit
from ear_storage.schema import connection, one
from ear_storage_client import (
    Context,
    Create,
    DependencyInput,
    PermissionDenied,
    RegisterDependencies,
    Update,
)
from ear_storage_client.consumer_wire import RegisterConsumer

from .test_history import recover, relay
from .test_history_execution import arrange, chunk
from .test_management import command, declaration
from .test_management_integration import execute
from .test_management_integration import setup as setup
from .test_reconfiguration import drain
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()


def consumerless(setup):
    _, _, _, service, operator, _, _ = setup
    body = declaration().model_copy(update={"subscribe_types": ()})
    assert execute(setup, body)[1]["status"] == "succeeded"
    plugin = operator.issue_principal("sample", read_types=body.read_types, subscribe_types=[])
    assert execute(setup, command("enable"))[1]["enabled"]
    return plugin, service.client(plugin.token)


def test_overlap_rejected_until_explicit_skip_invalidates_claim(setup):
    dsn, _, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup, catchup=True)
    lease = claim(client, job)
    _, body = chunk(client, lease, feedback)
    _, second = recover(setup, "catch_up")
    assert second["status"] == "failed" and second["error_code"] == "recovery_in_progress"
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_jobs")[0] == 1
    _, skipped = recover(setup, "skip")
    assert skipped["status"] == "succeeded" and not skipped["need_recovery_decision"]
    with pytest.raises(PermissionDenied):
        commit(client, job, body)


@pytest.mark.parametrize("choice", ["skip", "catch_up"])
@pytest.mark.parametrize("relayed", [False, True])
def test_consumerless_handoff_preserves_w_plus_without_new_type_history(setup, relayed, choice):
    dsn, backend, _, service, _, _, _ = setup
    plugin, client = consumerless(setup)
    old, future, unrelated = new_id(), new_id(), new_id()
    backend.client("extraction").write([Create("activity", old, {})], SYS).check()
    _, state = execute(setup, command("set_permissions", read_types=("sample_item",)))
    _, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person"),
            resume_choice=choice,
            expected_revision=state["revision"],
        ),
    )
    assert result["status"] == "succeeded"
    if choice == "catch_up":
        assert claim(client, result["history_job_id"])["status"] == "completed"
    backend.client("extraction").write([Create("activity", future, {})], SYS).check()
    backend.client("attribution").write([Create("person", unrelated, {})], SYS).check()
    execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity", "person"),
            subscribe_types=("activity", "person"),
        ),
    )
    if relayed:
        relay(setup)
    assert service.consumers is not None
    assert (
        service.consumers.register(
            plugin.token, RegisterConsumer(consumer="sample.late", event_types=["data_changed"])
        )["status"]
        == "ready"
    )
    relay(setup)
    assert drain(service.consumers, plugin.token, "sample.late") == [future]
    with connection(dsn) as conn:
        assert (
            one(
                conn,
                "SELECT count(*) FROM storage.module_type_handoffs "
                "WHERE module='sample' AND data_type='activity'",
            )[0]
            == 1
        )


@pytest.mark.parametrize("changed", [True, False])
def test_readonly_unsubscribed_stale_dependency_without_request(setup, changed):
    dsn, backend, _, _, _, _, _ = setup
    _, client = consumerless(setup)
    source, target = new_id(), new_id()
    backend.client("extraction").write(
        [Create("activity", source, {"used": 1, "unused": 1})], SYS
    ).check()
    client.write(
        [
            Create("sample_item", target, {}),
            RegisterDependencies(
                "sample_item",
                target,
                [
                    DependencyInput(
                        VersionRef(data_type="activity", id=source, version=1), ("used",)
                    )
                ],
            ),
        ],
        SYS,
    ).check()
    _, state = execute(setup, command("set_permissions", read_types=("sample_item",)))
    backend.client("extraction").write(
        [Update("activity", source, 1, {"used": 2 if changed else 1, "unused": 2})], SYS
    ).check()
    _, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity"),
            resume_choice="catch_up",
            expected_revision=state["revision"],
        ),
    )
    assert result["status"] == "succeeded"
    status = claim(client, result["history_job_id"])
    assert status["status"] == ("blocked" if changed else "completed")
    if changed:
        assert status["error_code"] == "scheduler_unavailable"
        assert status["item"]["id"] == target
    assert execute(setup, command("status"))[1]["need_recovery_decision"] == changed
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.published")[0] == 0
        assert one(conn, "SELECT count(*) FROM storage.history_items")[0] == int(changed)


@pytest.mark.parametrize(
    "outcome,error", [("failed", "business_failed"), ("blocked", "needs_operator")]
)
def test_explicit_new_catchup_replaces_terminal_and_rejects_old_receipt(setup, outcome, error):
    _, client, _, job, feedback = arrange(setup, catchup=True)
    lease = claim(client, job)
    _, body = chunk(client, lease, feedback, outcome=outcome, error=error)
    assert commit(client, job, body)["job_state"] == outcome
    accepted, result = recover(setup, "catch_up")
    assert result["status"] == "succeeded"
    replacement = result["history_job_id"]
    assert replacement != job
    with pytest.raises(PermissionDenied):
        commit(client, job, body)
    newer = claim(client, replacement)
    _, newbody = chunk(client, newer, accepted["feedback_id"])
    assert commit(client, replacement, newbody)["job_state"] == "completed"
    assert not execute(setup, command("status"))[1]["need_recovery_decision"]


def test_existing_overlap_cannot_release_or_advance_other_failed_job(setup):
    from types import SimpleNamespace

    from ear_storage.history import switch

    dsn, _, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup, catchup=True)
    lease = claim(client, job)
    _, body = chunk(client, lease, feedback)
    # Legacy v12 permitted an independent decision at the unchanged enabled revision.
    with connection(dsn) as conn:
        revision = one(conn, "SELECT revision FROM storage.module_lifecycle WHERE module='sample'")[
            0
        ]
        old = one(conn, "SELECT decision_id FROM storage.history_jobs WHERE job_id=%s", (job,))[0]
        conn.execute(
            "INSERT INTO storage.recovery_decisions SELECT %s,feedback_id,module,%s,mode,"
            "types_text,"
            "watermark,coverage_lower_bound,'authorized' FROM storage.recovery_decisions "
            "WHERE decision_id=%s",
            (new_id(), revision, old),
        )
    assert commit(client, job, body)["job_state"] == "completed"
    assert execute(setup, command("status"))[1]["need_recovery_decision"]
    with connection(dsn) as conn:
        assert (
            one(conn, "SELECT state FROM storage.recovery_decisions WHERE decision_id=%s", (old,))[
                0
            ]
            == "authorized"
        )
        assert not switch(conn, SimpleNamespace(module="sample", expected_revision=revision))
    assert recover(setup, "skip")[1]["status"] == "succeeded"
    assert not execute(setup, command("status"))[1]["need_recovery_decision"]


@pytest.mark.parametrize("case", ["other_owner", "denied_input", "limit", "dedup"])
def test_dependency_scope_permission_and_bounded_planning(setup, monkeypatch, case):
    dsn, backend, _, service, operator, _, _ = setup
    _, client = consumerless(setup)
    source, source2, target = new_id(), new_id(), new_id()
    backend.client("extraction").write(
        [Create("activity", source, {"n": 1}), Create("activity", source2, {"n": 1})], SYS
    ).check()
    refs = [VersionRef(data_type="activity", id=rid, version=1) for rid in (source, source2)]
    if case == "other_owner":
        body = declaration("other").model_copy(update={"subscribe_types": ()})
        execute(setup, body)
        other = operator.issue_principal("other", read_types=body.read_types)
        execute(setup, command("enable", "other"))
        owner = service.client(other.token)
        dtype = "other_item"
    else:
        owner, dtype = client, "sample_item"
    if case == "denied_input":
        execute(setup, command("set_permissions", read_types=("sample_item", "activity", "person")))
        person = new_id()
        backend.client("attribution").write([Create("person", person, {})], SYS).check()
        refs.append(VersionRef(data_type="person", id=person, version=1))
    owner.write([Create(dtype, target, {}), RegisterDependencies(dtype, target, refs)], SYS).check()
    _, state = execute(setup, command("set_permissions", read_types=("sample_item",)))
    backend.client("extraction").write(
        [Update("activity", source, 1, {"n": 2}), Update("activity", source2, 1, {"n": 2})], SYS
    ).check()
    if case == "limit":
        monkeypatch.setattr("ear_storage.history.MAX_ITEMS", 1)
    _, result = execute(
        setup,
        command(
            "set_permissions",
            read_types=("sample_item", "activity"),
            resume_choice="catch_up",
            expected_revision=state["revision"],
        ),
    )
    if case == "limit":
        assert result["error_code"] == "history_input_limit"
        with connection(dsn) as conn:
            assert one(conn, "SELECT count(*) FROM storage.history_jobs")[0] == 0
        return
    assert result["status"] == "succeeded"
    status = claim(client, result["history_job_id"])
    assert status["status"] == ("completed" if case == "other_owner" else "blocked")
    if case == "denied_input":
        assert status["error_code"] == "input_unavailable"
    with connection(dsn) as conn:
        items = conn.execute("SELECT data_type,object_id FROM storage.history_items").fetchall()
        assert items == ([] if case == "other_owner" else [("sample_item", target)])


def test_schema12_upgrade_preserves_recovery_jobs_and_claims(pg_dsn, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from typing import LiteralString, cast

    from ear_storage import schema
    from ear_storage.auth import AuthOperator
    from ear_storage.backend import PostgresStorage
    from ear_storage.management import FEEDBACK, RESULT, ManagementProcessor
    from ear_storage.service import StorageService

    from .test_management import run

    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 12)
        schema.migrate(pg_dsn)
        schema.bootstrap(pg_dsn)
        backend = PostgresStorage(pg_dsn)
        service = StorageService(pg_dsn)
        operator = AuthOperator(pg_dsn)
        admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
        mgmt = (pg_dsn, backend, service, operator, admin, ManagementProcessor(backend))
        assert run(mgmt, declaration())[1]["status"] == "succeeded"
        day = datetime(2026, 1, 1, tzinfo=UTC)
        source = new_id()
        backend.client("extraction").write(
            [Create("activity", source, {}, event_start=day, event_end=day)], SYS
        ).check()
        _, result = run(
            mgmt,
            command(
                "history_backfill",
                input_types=("activity",),
                rangeStart=day,
                rangeEnd=day + timedelta(days=1),
            ),
        )
        assert result["status"] == "succeeded"
        with connection(pg_dsn) as conn:
            conn.execute(
                "UPDATE storage.history_items SET fence=7,attempts=2,claim_hash='synthetic-hash'"
            )
            conn.execute(
                "INSERT INTO storage.recovery_decisions(decision_id,feedback_id,module,"
                "lifecycle_revision,mode,types_text,watermark,state) SELECT %s,feedback_id,"
                "owner,1,'skip','[\"activity\"]',recovery_boundary,'switched' "
                "FROM storage.history_jobs",
                (new_id(),),
            )
            before = {
                name: conn.execute(cast(LiteralString, query)).fetchall()
                for name, query in {
                    "jobs": "SELECT row_to_json(t)::text FROM storage.history_jobs t "
                    "ORDER BY row_to_json(t)::text",
                    "items": "SELECT row_to_json(t)::text FROM storage.history_items t "
                    "ORDER BY row_to_json(t)::text",
                    "outbox": "SELECT row_to_json(t)::text FROM storage.outbox t "
                    "ORDER BY row_to_json(t)::text",
                    "grants": "SELECT row_to_json(t)::text FROM storage.type_grants t "
                    "ORDER BY row_to_json(t)::text",
                    "idempotency": "SELECT row_to_json(t)::text FROM storage.idempotency t "
                    "ORDER BY row_to_json(t)::text",
                }.items()
            }
    schema.migrate(pg_dsn)
    schema.migrate(pg_dsn)
    with connection(pg_dsn) as conn:
        assert one(conn, "SELECT schema_revision FROM storage.meta")[0] == schema.SCHEMA_VERSION
        for name, query in {
            "jobs": "SELECT row_to_json(t)::text FROM storage.history_jobs t "
            "ORDER BY row_to_json(t)::text",
            "items": "SELECT row_to_json(t)::text FROM storage.history_items t "
            "ORDER BY row_to_json(t)::text",
            "outbox": "SELECT row_to_json(t)::text FROM storage.outbox t "
            "ORDER BY row_to_json(t)::text",
            "grants": "SELECT row_to_json(t)::text FROM storage.type_grants t "
            "ORDER BY row_to_json(t)::text",
            "idempotency": "SELECT row_to_json(t)::text FROM storage.idempotency t "
            "ORDER BY row_to_json(t)::text",
        }.items():
            assert conn.execute(cast(LiteralString, query)).fetchall() == before[name]
        assert one(conn, "SELECT count(*) FROM storage.module_type_handoffs")[0] == 1
        conn.execute("DROP TABLE storage.module_type_handoffs")
    with pytest.raises(schema.SchemaMismatch, match="module_type_handoffs"):
        PostgresStorage(pg_dsn)
