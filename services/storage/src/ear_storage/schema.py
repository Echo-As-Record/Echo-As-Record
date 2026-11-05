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

SCHEMA_VERSION = 3
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


def check_schema(conn: psycopg.Connection) -> None:
    if one(conn, "SELECT to_regclass('storage.meta')")[0] is None:
        raise SchemaMismatch("schema absent: run explicit migrate()")
    rows = conn.execute("SELECT schema_revision FROM storage.meta WHERE singleton").fetchall()
    if rows != [(SCHEMA_VERSION,)]:
        raise SchemaMismatch("unsupported schema revision: run explicit migrations")


def migrate(dsn: str) -> None:
    """Explicit atomic v1 -> v2 -> v3 upgrade; retain data and internal replay scopes."""
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
        check_schema(conn)
        for name in (
            "snapshot_leases",
            "snapshot_items",
            "principals",
            "type_grants",
            "registry",
            "publishers",
            "modules",
            "identity",
            "dependency_sets",
            "dependency_edges",
            "processed",
            "published",
            "outbox",
            "idempotency",
            "current_dependency_sets",
            "current_dependency_edges",
        ):
            if one(conn, "SELECT to_regclass(%s)", ("storage." + name,))[0] is None:
                raise SchemaMismatch("incomplete schema: " + name)


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
                    sql.SQL("CREATE INDEX ON {} (data_type,event_start,id)").format(
                        table(module, name)
                    )
                )
            conn.execute("INSERT INTO storage.modules VALUES (%s,%s)", (module, schema))
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
