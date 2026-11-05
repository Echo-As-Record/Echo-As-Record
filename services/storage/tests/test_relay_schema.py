from __future__ import annotations

from dataclasses import replace
from typing import LiteralString, cast

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, new_id
from ear_storage import PostgresStorage, SchemaMismatch, bootstrap, migrate, schema
from ear_storage.auth import AuthOperator
from ear_storage.relay import RelayConfig, RelayStore
from ear_storage.schema import (
    DDL,
    DDL_V2,
    DDL_V3,
    DDL_V4,
    DDL_V5,
    DDL_V6,
    DDL_V7,
    SCHEMA_VERSION,
    connection,
    writer,
)
from ear_storage.service import StorageService
from ear_storage_client import Context, Create, PutSystemRecord
from psycopg import sql

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("revision", [1, 2, 3, 4, 5, 6, 7])
def test_genuine_legacy_upgrade_preserves_events_and_existing_state(pg_dsn, monkeypatch, revision):
    statements = (
        DDL
        + (DDL_V2 if revision >= 2 else ())
        + (DDL_V3 if revision >= 3 else ())
        + (DDL_V4 if revision >= 4 else ())
        + (DDL_V5 if revision >= 5 else ())
        + (DDL_V6 if revision >= 6 else ())
        + (DDL_V7 if revision >= 7 else ())
    )
    with connection(pg_dsn) as conn, writer(conn):
        for statement in statements:
            conn.execute(cast(LiteralString, statement))
        conn.execute("INSERT INTO storage.meta VALUES (true,%s,0)", (revision,))
    snapshot = issued = result = None
    record_id = new_id()
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", revision)
        bootstrap(pg_dsn)
        # Revision 1 has the old idempotency key; existing v1 regression covers replay.
        if revision >= 2:
            store = PostgresStorage(pg_dsn)
            record_id = new_id()
            result = (
                store.client("extraction")
                .write(
                    [
                        Create("activity", record_id, {"legacy": True}),
                        PutSystemRecord("extraction_run", new_id(), {}),
                    ],
                    Context.system(),
                )
                .check()
            )
            if revision >= 3:
                issued = AuthOperator(pg_dsn).issue_principal("extraction", read_types=["activity"])
                snapshot = StorageService(pg_dsn).create_snapshot(issued.token)
        with connection(pg_dsn) as conn:
            before = conn.execute(
                "SELECT commit_seq,ordinal,event_id,event_text FROM storage.outbox"
            ).fetchall()
    migrate(pg_dsn)
    migrate(pg_dsn)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            SCHEMA_VERSION,
        )
        assert (
            conn.execute(
                "SELECT commit_seq,ordinal,event_id,event_text FROM storage.outbox"
            ).fetchall()
            == before
        )
        assert conn.execute(
            "SELECT fence,paused,destination FROM storage.relay_state"
        ).fetchone() == (0, False, None)
        assert conn.execute(
            "SELECT attempts,delivered_at,claim_token FROM storage.outbox"
        ).fetchall() == [(0, None, None)] * len(before)
    if snapshot is not None:
        assert issued is not None and result is not None
        client = StorageService(pg_dsn).client(issued.token)
        assert client.read_snapshot(snapshot.snapshot_id).get(record_id) is not None
        assert PostgresStorage(pg_dsn).outbox()[0][1] == result.events
    state = RelayStore(pg_dsn, RelayConfig(servers=("nats://127.0.0.1:4222",)))
    assert state.health()["pending"] == len(before)


@pytest.mark.parametrize("length", [55, 56])
def test_same_named_new_type_keeps_existing_module_limit(pg_dsn, length):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    name = "m" * length
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec(name, "semantic", name))
    if length == 55:
        bootstrap(pg_dsn, registry)
        result = (
            PostgresStorage(pg_dsn)
            .client(name)
            .write([Create(name, new_id(), {"synthetic": True})], Context.system())
            .check()
        )
        assert len(result.events) == 1
    else:
        with pytest.raises(ValueError, match="oversized module"):
            bootstrap(pg_dsn, registry)
        assert PostgresStorage(pg_dsn).registry == DEFAULT_REGISTRY
        with connection(pg_dsn) as conn:
            assert (
                conn.execute("SELECT 1 FROM storage.modules WHERE module=%s", (name,)).fetchone()
                is None
            )


@pytest.mark.parametrize("name", ["bad.type", "bad*", "bad>", "bad\n", "1bad", ""])
def test_invalid_registry_type_rejected_before_bootstrap_changes(pg_dsn, name):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec(name, "semantic", "synthetic_new"))
    with pytest.raises(ValueError, match="invalid registry type"):
        bootstrap(pg_dsn, registry)
    assert PostgresStorage(pg_dsn).registry == DEFAULT_REGISTRY
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ear_m_synthetic_new')").fetchone() == (None,)


def test_bootstrap_conflict_rolls_back_new_registration_not_existing(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    store = PostgresStorage(pg_dsn)
    event = (
        store.client("extraction")
        .write([Create("activity", new_id(), {"synthetic": True})], Context.system())
        .check()
        .events[0]
    )
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec("a" * 129, "semantic", "synthetic_new"))
    registry = replace(
        registry,
        data_types={
            **registry.data_types,
            "activity": replace(registry.get("activity"), description="synthetic conflict"),
        },
    )
    with pytest.raises(SchemaMismatch, match="registry bootstrap conflict"):
        bootstrap(pg_dsn, registry)
    assert PostgresStorage(pg_dsn).registry == DEFAULT_REGISTRY
    assert PostgresStorage(pg_dsn).outbox()[0][1] == (event,)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('ear_m_synthetic_new')").fetchone() == (None,)


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE storage.consumer_receipts",
        "ALTER TABLE storage.consumer_identities DROP COLUMN principal_id",
        "ALTER TABLE storage.consumer_deliveries DROP COLUMN queue_seq",
        "ALTER TABLE storage.consumer_deliveries DROP COLUMN historical_filtered",
        "ALTER TABLE storage.consumer_deliveries DROP COLUMN broker_subject",
        "ALTER TABLE storage.consumers DROP COLUMN config_text",
        "ALTER TABLE storage.outbox DROP COLUMN ack_seq",
        "ALTER TABLE storage.consumer_deliveries ALTER COLUMN queue_seq DROP IDENTITY",
    ],
)
def test_incomplete_revision_five_fails_closed_without_implicit_repair(pg_dsn, damage):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    with connection(pg_dsn) as conn:
        conn.execute(cast(LiteralString, damage))
    for operation in (StorageService, PostgresStorage, migrate, bootstrap):
        with pytest.raises(SchemaMismatch, match="incomplete schema"):
            operation(pg_dsn)
    with connection(pg_dsn) as conn:
        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            SCHEMA_VERSION,
        )


def test_revision7_consumers_receipts_markers_credentials_and_grants_preserved(pg_dsn, monkeypatch):
    from dataclasses import asdict

    from ear_storage.consumers import ConsumerPolicy
    from ear_storage.management import FEEDBACK, RESULT, ManagementProcessor, submit
    from ear_storage_client import MarkProcessed
    from ear_storage_client.engine import canonical_json

    from .test_management import declaration

    with connection(pg_dsn) as conn, writer(conn):
        for statement in DDL + DDL_V2 + DDL_V3 + DDL_V4 + DDL_V5 + DDL_V6 + DDL_V7:
            conn.execute(cast(LiteralString, statement))
        conn.execute("INSERT INTO storage.meta VALUES (true,7,0)")
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", 7)
        bootstrap(pg_dsn)
        backend = PostgresStorage(pg_dsn)
        service = StorageService(pg_dsn)
        operator = AuthOperator(pg_dsn)
        owner = operator.issue_principal(
            "extraction", read_types=["activity"], subscribe_types=["activity"]
        )
        admin = operator.issue_principal("api", admin=True, read_types=[FEEDBACK, RESULT])
        task = submit(service.client(admin.token), declaration())
        assert ManagementProcessor(backend).process(task["feedback_id"])["status"] == "succeeded"
        plugin = operator.issue_principal("sample", read_types=["activity"])
        events = (
            backend.client("extraction")
            .write(
                [Create("activity", new_id(), {}), Create("activity", new_id(), {})],
                Context.system(),
            )
            .check()
            .events
        )
        operator.reserve_consumer_identity("extraction.legacy", owner.principal_id)
        service.client(owner.token).write(
            [MarkProcessed("extraction.legacy", events[1].event_id)], Context.system()
        ).check()
        policy = asdict(ConsumerPolicy())
        policy.pop("history_modules")
        config = canonical_json(
            dict(
                policy=policy,
                event_types=["data_changed"],
                filters=["ear.data_changed.activity.*"],
                from_beginning=False,
                start_seq=1,
            )
        )
        did = new_id()
        with connection(pg_dsn) as conn, writer(conn):
            conn.execute(
                "INSERT INTO "
                "storage.consumers(name,principal_id,module,generation,config_text,"
                "destination,creation_cursor,ready) "
                "VALUES "
                "('extraction.legacy',%s,'extraction','legacy_generation',%s,'legacy_destination',0,true)",
                (owner.principal_id, config),
            )
            conn.execute(
                "INSERT INTO "
                "storage.consumer_deliveries(consumer,delivery_id,event_id,state,attempt,"
                "token,broker_reply,broker_subject) "
                "VALUES "
                "('extraction.legacy',%s,%s,'inflight',1,'old_receipt','old_reply','ear.data_changed.activity.extraction')",
                (did, events[0].event_id),
            )
            conn.execute(
                "INSERT INTO storage.consumer_receipts VALUES "
                "('old_receipt','extraction.legacy',%s)",
                (did,),
            )
            conn.execute(
                "INSERT INTO "
                "storage.credential_exchanges(feedback_id,principal_id,credential_epoch,"
                "module_revision,expires_at) "
                "VALUES (%s,%s,0,1,clock_timestamp()+interval '5 minutes')",
                (task["feedback_id"], plugin.principal_id),
            )
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='storage' ORDER BY tablename"
                )
            ]
            before = {
                t: conn.execute(
                    sql.SQL(
                        "SELECT to_jsonb(t) - 'broker_generation' FROM storage.{} t ORDER BY 1"
                    ).format(sql.Identifier(t))
                ).fetchall()
                for t in tables
            }
    migrate(pg_dsn)
    migrate(pg_dsn)
    with connection(pg_dsn) as conn:
        for name, rows in before.items():
            if name == "meta":
                continue
            current = conn.execute(
                sql.SQL(
                    "SELECT to_jsonb(t) - 'broker_generation' FROM storage.{} t ORDER BY 1"
                ).format(sql.Identifier(name))
            ).fetchall()
            assert current == rows, name
        assert conn.execute(
            "SELECT broker_generation FROM storage.consumer_deliveries"
        ).fetchone() == ("legacy_generation",)
        assert conn.execute(
            "SELECT consumer,generation,config_text,destination,state FROM "
            "storage.consumer_generations"
        ).fetchone() == (
            "extraction.legacy",
            "legacy_generation",
            config,
            "legacy_destination",
            "active",
        )
        assert conn.execute("SELECT * FROM storage.consumer_type_cursors").fetchone() == (
            "extraction.legacy",
            "activity",
            0,
        )
