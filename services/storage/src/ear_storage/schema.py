"""Explicit, atomic migrations and trusted registry bootstrap. No implicit DDL on startup."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, LiteralString

import psycopg
from ear_contracts import DEFAULT_REGISTRY, Registry
from ear_storage_client.engine import canonical_json
from psycopg import sql

SCHEMA_VERSION = 8
WRITER_GATE = 0x45415253544F5245


class SchemaMismatch(RuntimeError):
    pass


class ProjectionLimit(RuntimeError):
    """Fail closed: no graph nodes/edges or validator inputs were silently omitted."""


def module_schema(module: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,54}", module):
        raise ValueError("unsafe or oversized module identifier")
    return "ear_m_" + module


def table(module: str, name: str) -> sql.Identifier:
    return sql.Identifier(module_schema(module), name)


@contextmanager
def connection(dsn: str) -> Iterator[psycopg.Connection]:
    # Each operation owns its connection; no hidden pool or idle transactions.
    with psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=5,
        options="-c lock_timeout=5s -c statement_timeout=30s -c search_path=pg_catalog",
    ) as conn:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        yield conn


def one(
    conn: psycopg.Connection,
    query: sql.SQL | sql.Composed | LiteralString,
    params: Sequence[Any] = (),
) -> tuple[Any, ...]:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise SchemaMismatch("expected database row missing")
    return row


@contextmanager
def writer(conn: psycopg.Connection) -> Iterator[None]:
    with conn.transaction():
        # First application statement after BEGIN. Same gate for migrations/bootstrap/batches.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITER_GATE,))
        yield


DDL = (
    "CREATE SCHEMA storage",
    "CREATE TABLE storage.meta (singleton boolean PRIMARY KEY CHECK(singleton), "
    "schema_revision integer NOT NULL, commit_seq bigint NOT NULL CHECK(commit_seq >= 0))",
    "CREATE TABLE storage.registry (name text PRIMARY KEY, spec_text text NOT NULL, "
    "is_core boolean NOT NULL)",
    "CREATE TABLE storage.publishers (event_type text PRIMARY KEY, modules_text text NOT NULL)",
    "CREATE TABLE storage.modules (module text PRIMARY KEY, schema_name text UNIQUE NOT NULL)",
    "CREATE TABLE storage.identity (id text PRIMARY KEY, module text NOT NULL "
    "REFERENCES storage.modules, data_type text NOT NULL REFERENCES storage.registry(name), "
    "kind text NOT NULL CHECK(kind IN ('raw_record','semantic','rule','system_record')), "
    "current_seq integer NOT NULL CHECK(current_seq > 0), dedup_key text, adapter_version text)",
    "CREATE UNIQUE INDEX raw_dedup ON storage.identity(data_type,adapter_version,dedup_key) "
    "WHERE kind='raw_record'",
    "CREATE UNIQUE INDEX semantic_dedup ON storage.identity(data_type,dedup_key) "
    "WHERE kind IN ('semantic','rule') AND dedup_key IS NOT NULL",
    "CREATE INDEX identity_type ON storage.identity(data_type,id)",
    "CREATE TABLE storage.dependency_sets (dependent_id text NOT NULL REFERENCES storage.identity, "
    "dependent_version integer NOT NULL, commit_seq bigint NOT NULL, dependent_type text NOT NULL, "
    "registered_by text NOT NULL, registered_at timestamptz NOT NULL, "
    "PRIMARY KEY(dependent_id,dependent_version,commit_seq))",
    "CREATE TABLE storage.dependency_edges (dependent_id text NOT NULL, "
    "dependent_version integer NOT NULL, commit_seq bigint NOT NULL, target_id text NOT NULL "
    "REFERENCES storage.identity, record_text text NOT NULL, "
    "PRIMARY KEY(dependent_id,dependent_version,commit_seq,target_id), "
    "FOREIGN KEY(dependent_id,dependent_version,commit_seq) "
    "REFERENCES storage.dependency_sets)",
    "CREATE INDEX dependency_target ON storage.dependency_edges(target_id)",
    "CREATE TABLE storage.processed (consumer text NOT NULL, event_id text NOT NULL, "
    "commit_seq bigint NOT NULL, PRIMARY KEY(consumer,event_id))",
    "CREATE TABLE storage.published (event_id text PRIMARY KEY, commit_seq bigint NOT NULL)",
    "CREATE TABLE storage.outbox (commit_seq bigint NOT NULL, ordinal integer NOT NULL, "
    "event_id text UNIQUE NOT NULL, event_text text NOT NULL, "
    "PRIMARY KEY(commit_seq,ordinal))",
    "CREATE TABLE storage.idempotency (module text NOT NULL REFERENCES storage.modules, "
    "request_id text NOT NULL, fingerprint text NOT NULL, result_text text NOT NULL, "
    "required_types_text text NOT NULL, commit_seq bigint NOT NULL, "
    "PRIMARY KEY(module,request_id))",
    "CREATE VIEW storage.current_dependency_sets AS SELECT DISTINCT ON "
    "(dependent_id,dependent_version) * FROM storage.dependency_sets "
    "ORDER BY dependent_id,dependent_version,commit_seq DESC",
    "CREATE VIEW storage.current_dependency_edges AS SELECT e.* FROM storage.dependency_edges e "
    "JOIN storage.current_dependency_sets s USING(dependent_id,dependent_version,commit_seq)",
)


# DDL above is immutable revision 1; never label old DDL as revision 2.
DDL_V2 = (
    "CREATE TABLE storage.principals (id text PRIMARY KEY, module text NOT NULL "
    "REFERENCES storage.modules, token_hash text UNIQUE NOT NULL "
    "CHECK(token_hash ~ '^[0-9a-f]{64}$'), active boolean NOT NULL DEFAULT true)",
    "CREATE TABLE storage.type_grants (principal_id text NOT NULL REFERENCES storage.principals, "
    "data_type text NOT NULL REFERENCES storage.registry(name), "
    "can_subscribe boolean NOT NULL DEFAULT false, PRIMARY KEY(principal_id,data_type))",
    "ALTER TABLE storage.idempotency ADD COLUMN scope text",
    "UPDATE storage.idempotency SET scope='internal:' || module",
    "ALTER TABLE storage.idempotency ALTER COLUMN scope SET NOT NULL",
    "ALTER TABLE storage.idempotency DROP CONSTRAINT idempotency_pkey",
    "ALTER TABLE storage.idempotency ADD PRIMARY KEY(scope,request_id)",
)


# Explicit additive revision 3: leases and individually addressable canonical items.
DDL_V3 = (
    "CREATE TABLE storage.snapshot_leases (snapshot_id text PRIMARY KEY, "
    "principal_id text NOT NULL REFERENCES storage.principals, "
    "expires_at timestamptz NOT NULL, commit_seq bigint NOT NULL, "
    "item_count bigint NOT NULL CHECK(item_count>=0), "
    "byte_count bigint NOT NULL CHECK(byte_count>=0))",
    "CREATE INDEX snapshot_owner_expiry ON storage.snapshot_leases(principal_id,expires_at)",
    "CREATE INDEX snapshot_expiry ON storage.snapshot_leases(expires_at,snapshot_id)",
    "CREATE TABLE storage.snapshot_items (snapshot_id text NOT NULL REFERENCES "
    "storage.snapshot_leases ON DELETE CASCADE, kind text NOT NULL, item_key text NOT NULL, "
    "data_type text NOT NULL, record_text text NOT NULL, "
    "PRIMARY KEY(snapshot_id,kind,item_key))",
)


# Additive revision 4. The outbox event bytes and all previous revisions stay intact.
DDL_V4 = (
    "ALTER TABLE storage.outbox ADD COLUMN attempts bigint NOT NULL DEFAULT 0, "
    "ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT '-infinity', "
    "ADD COLUMN last_error text, ADD COLUMN claim_token bigint, "
    "ADD COLUMN delivered_at timestamptz, ADD COLUMN ack_stream text, "
    "ADD COLUMN ack_seq bigint",
    "CREATE INDEX outbox_pending ON storage.outbox(commit_seq,ordinal) WHERE delivered_at IS NULL",
    "CREATE TABLE storage.relay_state (singleton boolean PRIMARY KEY CHECK(singleton), "
    "fence bigint NOT NULL DEFAULT 0, lease_until timestamptz NOT NULL DEFAULT '-infinity', "
    "paused boolean NOT NULL DEFAULT false, destination text)",
    "INSERT INTO storage.relay_state(singleton) VALUES (true)",
)


# Revision 5 adds proxy-owned consumers; revisions 1--4 remain immutable.
DDL_V5 = (
    "CREATE TABLE storage.consumer_identities (name text PRIMARY KEY, "
    "principal_id text NOT NULL REFERENCES storage.principals)",
    "CREATE TABLE storage.consumers (name text PRIMARY KEY, principal_id text NOT NULL "
    "REFERENCES storage.principals, module text NOT NULL, generation text UNIQUE NOT NULL, "
    "config_text text NOT NULL, destination text NOT NULL, creation_cursor bigint NOT NULL, "
    "created_at timestamptz NOT NULL DEFAULT clock_timestamp(), "
    "ready boolean NOT NULL DEFAULT false, "
    "enabled boolean NOT NULL DEFAULT true, fence bigint NOT NULL DEFAULT 0, "
    "lease_until timestamptz NOT NULL DEFAULT '-infinity')",
    "CREATE TABLE storage.consumer_deliveries "
    "(consumer text NOT NULL REFERENCES storage.consumers, "
    "delivery_id text NOT NULL, queue_seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE, "
    "event_id text NOT NULL REFERENCES storage.outbox(event_id), "
    "replay_of text, attempt integer NOT NULL DEFAULT 0, token text UNIQUE, "
    "state text NOT NULL CHECK(state IN ('retry','inflight','acked','dead')), "
    "available_at timestamptz NOT NULL DEFAULT '-infinity', "
    "lease_until timestamptz NOT NULL DEFAULT '-infinity', broker_reply text, "
    "ack_pending boolean NOT NULL DEFAULT false, error_code text, "
    "historical_filtered boolean NOT NULL DEFAULT false, broker_subject text, "
    "CHECK(NOT historical_filtered OR (state='acked' AND replay_of IS NULL "
    "AND attempt=0 AND token IS NULL AND broker_subject IS NOT NULL)), "
    "PRIMARY KEY(consumer,delivery_id))",
    "CREATE UNIQUE INDEX consumer_original_event ON storage.consumer_deliveries(consumer,event_id) "
    "WHERE replay_of IS NULL",
    "CREATE INDEX consumer_pending ON storage.consumer_deliveries(consumer,queue_seq) "
    "WHERE state IN ('retry','inflight')",
    "CREATE UNIQUE INDEX consumer_one_inflight ON storage.consumer_deliveries(consumer) "
    "WHERE state='inflight'",
    "CREATE TABLE storage.consumer_receipts (token text PRIMARY KEY, consumer text NOT NULL, "
    "delivery_id text NOT NULL, FOREIGN KEY(consumer,delivery_id) "
    "REFERENCES storage.consumer_deliveries(consumer,delivery_id))",
)


# Revision 6 is additive. Management authority is not ordinary record payload.
DDL_V6 = (
    "ALTER TABLE storage.principals ADD COLUMN admin boolean NOT NULL DEFAULT false",
    "CREATE TABLE storage.module_lifecycle (module text PRIMARY KEY REFERENCES storage.modules, "
    "enabled boolean NOT NULL DEFAULT false, resources_ready boolean NOT NULL DEFAULT false, "
    "revision bigint NOT NULL DEFAULT 1, ever_enabled boolean NOT NULL DEFAULT false, "
    "recovery_pending boolean NOT NULL DEFAULT false, interrupted_seq bigint, "
    "recovery_types_text text NOT NULL DEFAULT '[]')",
    "CREATE TABLE storage.module_permissions (module text NOT NULL REFERENCES "
    "storage.module_lifecycle, data_type text NOT NULL REFERENCES storage.registry(name), "
    "can_subscribe boolean NOT NULL, PRIMARY KEY(module,data_type))",
    "CREATE TABLE storage.management_authority (feedback_id text PRIMARY KEY REFERENCES "
    "storage.identity(id), principal_id text NOT NULL REFERENCES storage.principals, "
    "request_id text NOT NULL, body_text text NOT NULL, UNIQUE(principal_id,request_id))",
    "CREATE TABLE storage.management_tasks (feedback_id text PRIMARY KEY REFERENCES "
    "storage.management_authority, event_id text NOT NULL REFERENCES storage.outbox(event_id), "
    "result_id text UNIQUE NOT NULL, status text NOT NULL DEFAULT 'accepted', "
    "stage text NOT NULL DEFAULT 'accepted', error_code text, "
    "CHECK(status IN ('accepted','pending','succeeded','failed')))",
    "CREATE TABLE storage.management_audit (feedback_id text PRIMARY KEY REFERENCES "
    "storage.management_tasks, module text NOT NULL, revision bigint NOT NULL, "
    "action text NOT NULL, state_text text NOT NULL)",
)
_REQUIRED_V6_COLUMNS = {
    "principals": "admin",
    "module_lifecycle": "module enabled resources_ready revision ever_enabled "
    "recovery_pending interrupted_seq recovery_types_text",
    "module_permissions": "module data_type can_subscribe",
    "management_authority": "feedback_id principal_id request_id body_text",
    "management_tasks": "feedback_id event_id result_id status stage error_code",
    "management_audit": "feedback_id module revision action state_text",
}

# Revision 7: no plaintext or recoverable credential material in exchanges.
# Nullable hash means no credential exists while an authorized claim is pending.
DDL_V7 = (
    "ALTER TABLE storage.principals ALTER COLUMN token_hash DROP NOT NULL",
    "ALTER TABLE storage.principals ADD COLUMN credential_epoch bigint NOT NULL DEFAULT 0 "
    "CHECK(credential_epoch>=0)",
    "CREATE TABLE storage.credential_exchanges (feedback_id text PRIMARY KEY REFERENCES "
    "storage.management_tasks, principal_id text NOT NULL REFERENCES storage.principals, "
    "credential_epoch bigint NOT NULL, module_revision bigint NOT NULL, "
    "expires_at timestamptz NOT NULL, claimed_at timestamptz, "
    "status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed')), "
    "CHECK((status='claimed') = (claimed_at IS NOT NULL)))",
)
_REQUIRED_V7_COLUMNS = {
    "principals": "credential_epoch",
    "credential_exchanges": "feedback_id principal_id credential_epoch module_revision "
    "expires_at claimed_at status",
}


# Revision 8 preserves v7 data and snapshots each immutable broker configuration.
DDL_V8 = (
    "CREATE TABLE storage.consumer_generations (consumer text NOT NULL REFERENCES "
    "storage.consumers, generation text PRIMARY KEY, config_text text NOT NULL, "
    "destination text NOT NULL, state text NOT NULL CHECK(state IN "
    "('prepared','active','retired')), feedback_id text REFERENCES storage.management_tasks)",
    "INSERT INTO storage.consumer_generations "
    "SELECT name,generation,config_text,destination,'active',NULL FROM storage.consumers",
    "ALTER TABLE storage.consumer_deliveries ADD COLUMN broker_generation text",
    "UPDATE storage.consumer_deliveries d SET broker_generation=c.generation "
    "FROM storage.consumers c WHERE c.name=d.consumer AND d.broker_reply IS NOT NULL",
    "CREATE TABLE storage.consumer_type_cursors (consumer text NOT NULL REFERENCES "
    "storage.consumers, data_type text NOT NULL REFERENCES storage.registry(name), "
    "effective_commit_seq bigint NOT NULL CHECK(effective_commit_seq>=0), "
    "PRIMARY KEY(consumer,data_type))",
    "INSERT INTO storage.consumer_type_cursors SELECT DISTINCT c.name, "
    "split_part(f.value,'.',array_length(string_to_array(f.value,'.'),1)-1), "
    "c.creation_cursor FROM storage.consumers c CROSS JOIN LATERAL "
    "jsonb_array_elements_text(c.config_text::jsonb->'filters') f(value)",
    "CREATE TABLE storage.consumer_reconfigurations (feedback_id text PRIMARY KEY "
    "REFERENCES storage.management_tasks, module text NOT NULL REFERENCES "
    "storage.module_lifecycle, plan_text text NOT NULL, "
    "state text NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','active','cancelled')))",
    "CREATE UNIQUE INDEX consumer_reconfiguration_pending ON "
    "storage.consumer_reconfigurations(module) WHERE state='prepared'",
)
_REQUIRED_V8_COLUMNS = {
    "consumer_generations": "consumer generation config_text destination state feedback_id",
    "consumer_deliveries": "broker_generation",
    "consumer_type_cursors": "consumer data_type effective_commit_seq",
    "consumer_reconfigurations": "feedback_id module plan_text state",
}


# A revision number is not proof of a complete schema. Never repair local old-v5 implicitly.
_REQUIRED_V5_COLUMNS = {
    "meta": "singleton schema_revision commit_seq",
    "registry": "name spec_text is_core",
    "publishers": "event_type modules_text",
    "modules": "module schema_name",
    "identity": "id module data_type kind current_seq dedup_key adapter_version",
    "dependency_sets": (
        "dependent_id dependent_version commit_seq dependent_type registered_by registered_at"
    ),
    "dependency_edges": "dependent_id dependent_version commit_seq target_id record_text",
    "current_dependency_sets": (
        "dependent_id dependent_version commit_seq dependent_type registered_by registered_at"
    ),
    "current_dependency_edges": "dependent_id dependent_version commit_seq target_id record_text",
    "processed": "consumer event_id commit_seq",
    "published": "event_id commit_seq",
    "outbox": (
        "commit_seq ordinal event_id event_text attempts next_attempt_at last_error "
        "claim_token delivered_at ack_stream ack_seq"
    ),
    "idempotency": "module request_id fingerprint result_text required_types_text commit_seq scope",
    "principals": "id module token_hash active",
    "type_grants": "principal_id data_type can_subscribe",
    "snapshot_leases": "snapshot_id principal_id expires_at commit_seq item_count byte_count",
    "snapshot_items": "snapshot_id kind item_key data_type record_text",
    "relay_state": "singleton fence lease_until paused destination",
    "consumer_identities": "name principal_id",
    "consumers": (
        "name principal_id module generation config_text destination creation_cursor "
        "created_at ready enabled fence lease_until"
    ),
    "consumer_deliveries": (
        "consumer delivery_id queue_seq event_id replay_of attempt token state "
        "available_at lease_until broker_reply ack_pending error_code "
        "historical_filtered broker_subject"
    ),
    "consumer_receipts": "token consumer delivery_id",
}


def check_schema(conn: psycopg.Connection) -> None:
    if one(conn, "SELECT to_regclass('storage.meta')")[0] is None:
        raise SchemaMismatch("schema absent: run explicit migrate()")
    columns: list[tuple[Any, ...]] = []
    present: set[tuple[str, str]] = set()
    if SCHEMA_VERSION >= 5:
        columns = conn.execute(
            "SELECT c.relname,a.attname,a.attidentity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid "
            "WHERE n.nspname='storage' AND c.relkind IN ('r','v') "
            "AND a.attnum>0 AND NOT a.attisdropped"
        ).fetchall()
        present = {(name, column) for name, column, _ in columns}
        # Check revision before v5 structure so genuine v1--v4 still report migration required.
        if not all(("meta", column) in present for column in _REQUIRED_V5_COLUMNS["meta"].split()):
            raise SchemaMismatch("incomplete schema: meta")
    rows = conn.execute("SELECT schema_revision FROM storage.meta WHERE singleton").fetchall()
    if rows != [(SCHEMA_VERSION,)]:
        raise SchemaMismatch("unsupported schema revision: run explicit migrations")
    if SCHEMA_VERSION >= 5:
        for name, required in _REQUIRED_V5_COLUMNS.items():
            if any((name, column) not in present for column in required.split()):
                raise SchemaMismatch("incomplete schema: " + name)
        if SCHEMA_VERSION >= 6:
            for name, required in _REQUIRED_V6_COLUMNS.items():
                if any((name, column) not in present for column in required.split()):
                    raise SchemaMismatch("incomplete schema: " + name)
        if SCHEMA_VERSION >= 7:
            for name, required in _REQUIRED_V7_COLUMNS.items():
                if any((name, column) not in present for column in required.split()):
                    raise SchemaMismatch("incomplete schema: " + name)
        if SCHEMA_VERSION >= 8:
            for name, required in _REQUIRED_V8_COLUMNS.items():
                if any((name, column) not in present for column in required.split()):
                    raise SchemaMismatch("incomplete schema: " + name)
        if ("consumer_deliveries", "queue_seq", "a") not in columns:
            raise SchemaMismatch("incomplete schema: consumer_deliveries queue identity")


def migrate(dsn: str) -> None:
    """Explicit atomic v1 through v8 upgrade; retain data and internal replay scopes."""
    with connection(dsn) as conn, writer(conn):
        if one(conn, "SELECT to_regnamespace('storage')")[0] is None:
            for statement in DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO storage.meta VALUES (true,%s,0)", (1,))
        revision = one(conn, "SELECT schema_revision FROM storage.meta WHERE singleton")[0]
        if revision == 1:
            for statement in DDL_V2:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=2 WHERE singleton")
        if revision in (1, 2):
            for statement in DDL_V3:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=3 WHERE singleton")
        if revision in (1, 2, 3):
            for statement in DDL_V4:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=4 WHERE singleton")
        if revision in (1, 2, 3, 4):
            for statement in DDL_V5:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=5 WHERE singleton")
        if revision in (1, 2, 3, 4, 5):
            for statement in DDL_V6:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=6 WHERE singleton")
        if revision in (1, 2, 3, 4, 5, 6):
            for statement in DDL_V7:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=7 WHERE singleton")
        if revision in (1, 2, 3, 4, 5, 6, 7):
            for statement in DDL_V8:
                conn.execute(statement)
            conn.execute("UPDATE storage.meta SET schema_revision=8 WHERE singleton")
        check_schema(conn)


def bootstrap(dsn: str, registry: Registry = DEFAULT_REGISTRY) -> None:
    """Deployment-trusted registry only. Existing entries cannot be changed/upgraded implicitly."""
    modules = {s.writer for s in registry.data_types.values()}
    modules.update(m for ms in registry.event_publishers.values() for m in ms)
    for module in modules:
        module_schema(module)
    for name, spec in registry.data_types.items():
        if name != spec.name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("invalid registry type")
    with connection(dsn) as conn, writer(conn):
        check_schema(conn)
        for module in sorted(modules):
            schema = module_schema(module)
            row = conn.execute(
                "SELECT schema_name FROM storage.modules WHERE module=%s", (module,)
            ).fetchone()
            if row is not None:
                if row[0] != schema:
                    raise SchemaMismatch("module schema mapping mismatch")
                continue
            create_module(conn, module)
        for name, spec in sorted(registry.data_types.items()):
            text, core = canonical_json(spec.to_json()), registry.is_core(name)
            row = conn.execute(
                "SELECT spec_text,is_core FROM storage.registry WHERE name=%s", (name,)
            ).fetchone()
            if row is not None and row != (text, core):
                raise SchemaMismatch("registry bootstrap conflict: " + name)
            if row is None:
                conn.execute("INSERT INTO storage.registry VALUES (%s,%s,%s)", (name, text, core))
        for event, publishers in sorted(registry.event_publishers.items()):
            text = canonical_json(publishers)
            row = conn.execute(
                "SELECT modules_text FROM storage.publishers WHERE event_type=%s", (event,)
            ).fetchone()
            if row is not None and row != (text,):
                raise SchemaMismatch("publisher bootstrap conflict")
            if row is None:
                conn.execute("INSERT INTO storage.publishers VALUES (%s,%s)", (event, text))


def create_module(conn: psycopg.Connection, module: str) -> None:
    """Trusted server-only DDL; caller holds writer gate and validates uniqueness."""
    schema = module_schema(module)
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for name in ("raw_records", "versions", "system_records"):
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (id text NOT NULL REFERENCES "
                "storage.identity, seq integer NOT NULL CHECK(seq>0), "
                "data_type text NOT NULL, "
                "record_text text NOT NULL, payload_text text NOT NULL, "
                "event_start timestamptz, event_end timestamptz, source_user text, "
                "superseded boolean NOT NULL DEFAULT false, commit_seq bigint NOT NULL, "
                "PRIMARY KEY(id,seq))"
            ).format(table(module, name))
        )
        conn.execute(
            sql.SQL("CREATE INDEX ON {} (data_type,event_start,id)").format(table(module, name))
        )
    conn.execute("INSERT INTO storage.modules VALUES (%s,%s)", (module, schema))
