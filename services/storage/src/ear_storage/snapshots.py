"""Bounded durable leases, captured under the shared READ COMMITTED writer gate.

No pickle/state blob: one canonical text per record revision, edge, set, grant
specification or processed marker. SQL measures every source before hydration.
Only deployment code may call sweep_expired; transport exposes client methods.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from ear_contracts import (
    DataTypeSpec,
    DependencyRecord,
    RawRecord,
    ReevaluationResult,
    Registry,
    SemanticRecord,
    SystemRecord,
    VersionRef,
    is_id,
)
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.completion import _indexed_result
from ear_storage_client.engine import State, canonical_json
from ear_storage_client.memory import _StateReader
from ear_storage_client.queries import DependencySet
from psycopg import sql

from ear_storage.auth import Principal, can_use_consumer
from ear_storage.causality import record_input_types, visible_trace_sql
from ear_storage.schema import (
    ProjectionLimit,
    check_schema,
    connection,
    has_type_revisions,
    one,
    table,
    writer,
)


class SnapshotUnavailable(PermissionDenied):
    """Missing, expired, foreign and inactive-owner leases share this generic error."""


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    max_rows: int = 50_000
    max_bytes: int = 32 * 1024 * 1024
    max_active_leases: int = 4
    default_ttl_seconds: int = 300
    max_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        values = (
            self.max_rows,
            self.max_bytes,
            self.max_active_leases,
            self.default_ttl_seconds,
            self.max_ttl_seconds,
        )
        if any(type(v) is not int or v < 1 for v in values):
            raise ValueError("snapshot limits must be positive integers")
        if self.default_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("default TTL exceeds maximum TTL")


@dataclass(frozen=True, slots=True)
class SnapshotLease:
    snapshot_id: str
    expires_at: datetime
    commit_seq: int
    item_count: int
    byte_count: int

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "expires_at": self.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "commit_seq": self.commit_seq,
            "item_count": self.item_count,
            "byte_count": self.byte_count,
        }


def validate_lease(conn: psycopg.Connection, principal: Principal, snapshot_id: str) -> None:
    if (
        not is_id(snapshot_id)
        or not conn.execute(
            "SELECT 1 FROM storage.snapshot_leases WHERE snapshot_id=%s AND principal_id=%s "
            "AND expires_at>clock_timestamp()",
            (snapshot_id, principal.id),
        ).fetchone()
    ):
        raise SnapshotUnavailable("snapshot unavailable")


def _sources(conn: psycopg.Connection, principal: Principal):
    reads = sorted(principal.read_types)
    # All queries yield (kind, key, type, text). Metadata is also counted, not a free blob.
    yield (
        sql.SQL(
            "SELECT 'registry',name,name,json_build_array(spec_text,is_core)::text "
            "FROM storage.registry WHERE name=ANY(%s)"
        ),
        [reads],
    )
    if has_type_revisions(conn):
        yield (
            sql.SQL(
                "SELECT 'type_revision',data_type,data_type,revision::text "
                "FROM storage.type_revisions WHERE data_type=ANY(%s)"
            ),
            [reads],
        )
    if conn.execute("SELECT to_regclass('storage.request_completions')").fetchone() != (None,):
        # A constant marker distinguishes an empty captured index from a legacy lease.
        yield (sql.SQL("SELECT 'completion_index','v1','','1'"), [])
        for (module,) in conn.execute("SELECT module FROM storage.modules"):
            yield (
                sql.SQL(
                    "SELECT 'completion',c.event_id,c.result_type,to_json(c.result_type)::text "
                    "FROM storage.request_completions c JOIN {} r "
                    "ON r.id=c.event_id WHERE c.result_type=ANY(%s) AND {}"
                ).format(table(module, "system_records"), visible_trace_sql(reads)),
                [reads],
            )
    modules = conn.execute(
        "SELECT DISTINCT module FROM storage.identity WHERE data_type=ANY(%s)", (reads,)
    )
    for (module,) in modules:
        for kind, name in (
            ("raw", "raw_records"),
            ("version", "versions"),
            ("system", "system_records"),
        ):
            yield (
                sql.SQL(
                    "SELECT %s::text,r.id||'/'||r.seq,r.data_type,r.record_text FROM {} r "
                    "WHERE r.data_type=ANY(%s) AND {} AND NOT EXISTS "
                    "(SELECT 1 FROM {} c WHERE c.id=r.id AND NOT ({}))"
                ).format(
                    table(module, name),
                    visible_trace_sql(reads),
                    table(module, name),
                    visible_trace_sql(reads, text="c.record_text", dtype="c.data_type"),
                ),
                [kind, reads],
            )
    yield (
        sql.SQL(
            "SELECT 'group',s.dependent_id||'/'||s.dependent_version,s.dependent_type,"
            "json_build_array(s.dependent_id,s.dependent_version,s.dependent_type,"
            "s.registered_by,s.registered_at,EXISTS (SELECT 1 FROM "
            "storage.current_dependency_edges e "
            "JOIN storage.identity t ON t.id=e.target_id WHERE e.dependent_id=s.dependent_id "
            "AND e.dependent_version=s.dependent_version AND NOT (t.data_type=ANY(%s))))::text "
            "FROM storage.current_dependency_sets s WHERE s.dependent_type=ANY(%s)"
        ),
        [reads, reads],
    )
    yield (
        sql.SQL(
            "SELECT 'edge',e.dependent_id||'/'||e.dependent_version||'/'||e.target_id,"
            "d.data_type,e.record_text FROM storage.current_dependency_edges e "
            "JOIN storage.identity d ON d.id=e.dependent_id "
            "JOIN storage.identity t ON t.id=e.target_id "
            "WHERE d.data_type=ANY(%s) AND t.data_type=ANY(%s)"
        ),
        [reads, reads],
    )
    # New snapshots must not even count another principal's registered progress.
    registered = conn.execute("SELECT to_regclass('storage.consumers')").fetchone() != (None,)
    owner_filter = (
        " AND NOT EXISTS (SELECT 1 FROM storage.consumers c WHERE c.name=p.consumer "
        "AND (c.principal_id<>%s OR NOT c.enabled))"
        if registered
        else ""
    )
    yield (
        sql.SQL(
            "SELECT 'processed',consumer||'/'||event_id,''::text,"
            "json_build_array(consumer,event_id)::text FROM storage.processed p "
            "WHERE (consumer=%s OR starts_with(consumer,%s))"
        )
        + sql.SQL(owner_filter),
        [principal.module, principal.module + ".", *([principal.id] if registered else [])],
    )


def create_lease(
    conn: psycopg.Connection, principal: Principal, limits: SnapshotLimits, ttl: int | None
) -> SnapshotLease:
    # PostgreSQL 17: cap the entire materialization transaction, not each SQL alone.
    conn.execute("SET LOCAL transaction_timeout = '30s'")
    ttl = limits.default_ttl_seconds if ttl is None else ttl
    if type(ttl) is not int or not 1 <= ttl <= limits.max_ttl_seconds:
        raise InvalidRequest("invalid snapshot TTL")
    active = one(
        conn,
        "SELECT count(*) FROM storage.snapshot_leases WHERE principal_id=%s "
        "AND expires_at>clock_timestamp()",
        (principal.id,),
    )[0]
    if active >= limits.max_active_leases:
        raise ProjectionLimit("snapshot active lease limit exceeded")
    sources = list(_sources(conn, principal))
    rows = size = 0
    for query, params in sources:
        count, byte_count = one(
            conn,
            sql.SQL(
                "SELECT count(*),coalesce(sum(octet_length(k)+octet_length(i)+"
                "octet_length(t)+octet_length(v)),0) FROM ({}) q(k,i,t,v)"
            ).format(query),
            params,
        )
        rows += count
        size += byte_count
        if rows > limits.max_rows or size > limits.max_bytes:
            raise ProjectionLimit("snapshot row/byte limit exceeded")
    now, expires, seq = one(
        conn,
        "SELECT clock_timestamp(),clock_timestamp()+(%s * interval '1 second'),commit_seq "
        "FROM storage.meta WHERE singleton",
        (ttl,),
    )
    # UUIDv7 timestamp + independently random 74 bits (no predictable monotonic counter).
    value = (int(now.timestamp() * 1000) << 80) | (7 << 76)
    value |= (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62)
    sid = str(UUID(int=value))
    conn.execute(
        "INSERT INTO storage.snapshot_leases VALUES (%s,%s,%s,%s,%s,%s)",
        (sid, principal.id, expires, seq, rows, size),
    )
    actual_size = 0
    insert = "INSERT INTO storage.snapshot_items VALUES (%s,%s,%s,%s,%s)"
    with conn.cursor() as sink:
        for query, params in sources:
            batch: list[tuple[str, str, str, str, str]] = []
            for kind, key, dtype, text in conn.execute(query, params):
                text = canonical_json(json.loads(text))
                actual_size += sum(len(v.encode("utf-8")) for v in (kind, key, dtype, text))
                if actual_size > limits.max_bytes:
                    raise ProjectionLimit("snapshot row/byte limit exceeded")
                batch.append((sid, kind, key, dtype, text))
                if len(batch) == 256:
                    sink.executemany(insert, batch)
                    batch.clear()
            if batch:
                sink.executemany(insert, batch)
    conn.execute(
        "UPDATE storage.snapshot_leases SET byte_count=%s WHERE snapshot_id=%s", (actual_size, sid)
    )
    validate_lease(conn, principal, sid)
    return SnapshotLease(sid, expires, seq, rows, actual_size)


class MaterializedReader(_StateReader):
    """Internal short-lived materialization, never handed to callers without reauthorization."""

    def __init__(
        self,
        state: State,
        registry: Registry,
        principal: Principal,
        systems: dict[str, list[SystemRecord]],
        denied: set[str],
        blocked: set[tuple[str, int]],
        completion_index: bool,
    ) -> None:
        super().__init__(state, registry)
        self.completion_index = completion_index
        self.principal, self.systems, self.denied, self.blocked = (
            principal,
            systems,
            denied,
            blocked,
        )

    def type_revision(self, data_type: str) -> int:
        self.principal.require_read([data_type])
        if data_type not in self._registry.data_types:
            raise PermissionDenied("access denied")
        if data_type not in self._s.type_revisions:
            # A v8 lease has no revision metadata; never fabricate zero.
            raise SnapshotUnavailable("snapshot lacks collection revisions; create a new snapshot")
        return self._s.type_revisions[data_type]

    def get_reevaluation_result(self, request_event_id: str) -> ReevaluationResult | None:
        if not self.completion_index:
            raise SnapshotUnavailable("snapshot lacks completion index; create a new snapshot")
        dtype = self._s.completions.get(request_event_id)
        if dtype is None:
            return None
        return _indexed_result(
            self._registry, request_event_id, dtype, self._s.system.get(request_event_id)
        )

    def system_history(self, id: str) -> list[SystemRecord]:
        return [r.model_copy(deep=True) for r in self.systems.get(id, [])]

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        if version is None:
            current = self._s.current(dependent_id)
            version = current.version if current is not None else None
        if (dependent_id, version) in self.blocked:
            raise PermissionDenied("access denied")
        return super().dependency_set(dependent_id, version)

    def authorized_read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "type_revision":
            return self.type_revision(args[0])
        if method in {"query", "count"}:
            query = args[0]
            required = {query.data_type}
            if query.unreferenced_by is not None:
                required.add(query.unreferenced_by[0])
            # New grants never retroactively expand the captured scope, even for empty types.
            if not required <= self._registry.data_types.keys():
                raise PermissionDenied("access denied")
        elif method == "is_processed":
            if not self.principal.owns_consumer(args[0]):
                return False
        else:
            ids = args[0] if method in {"get_many", "stale_dependencies"} else [args[0]]
            if self.denied.intersection(ids):
                raise PermissionDenied("access denied")
        return getattr(self, method)(*args, **kwargs)


def load_reader(
    conn: psycopg.Connection, principal: Principal, sid: str, limits: SnapshotLimits
) -> MaterializedReader:
    # Recheck actual persisted sizes before driver buffering, including after service restart.
    count, size = one(
        conn,
        "SELECT count(*),coalesce(sum(octet_length(kind)+octet_length(item_key)+"
        "octet_length(data_type)+octet_length(record_text)),0) FROM storage.snapshot_items "
        "WHERE snapshot_id=%s",
        (sid,),
    )
    if count > limits.max_rows or size > limits.max_bytes:
        raise ProjectionLimit("snapshot row/byte limit exceeded")
    state = State()
    completion_index = False
    specs: dict[str, DataTypeSpec] = {}
    core: set[str] = set()
    systems: dict[str, list[SystemRecord]] = {}
    versions: dict[str, list[SemanticRecord]] = {}
    denied: set[str] = set()
    blocked: set[tuple[str, int]] = set()
    groups: list[Sequence[Any]] = []
    edges: dict[tuple[str, int], list[DependencyRecord]] = {}
    for kind, key, dtype, text in conn.execute(
        "SELECT kind,item_key,data_type,record_text FROM storage.snapshot_items "
        "WHERE snapshot_id=%s ORDER BY kind,item_key",
        (sid,),
    ):
        if dtype and dtype not in principal.read_types:
            if kind in {"raw", "version", "system", "completion"}:
                denied.add(key.split("/")[0])
            continue
        if kind == "completion_index":
            if key != "v1" or json.loads(text) != 1:
                raise SnapshotUnavailable("invalid completion index metadata")
            completion_index = True
        elif kind == "completion":
            if json.loads(text) != dtype:
                raise SnapshotUnavailable("invalid completion index metadata")
            state.completions[key] = dtype
        elif kind == "registry":
            spec_text, is_core = json.loads(text)
            specs[dtype] = DataTypeSpec.from_json(dtype, json.loads(spec_text))
            if is_core:
                core.add(dtype)
        elif kind == "type_revision":
            revision = json.loads(text)
            if type(revision) is not int or revision < 0:
                raise SnapshotUnavailable("invalid collection revision metadata")
            state.type_revisions[dtype] = revision
        elif kind == "raw":
            raw = RawRecord.model_validate_json(text)
            state.raw[raw.id] = raw
        elif kind == "version":
            record = SemanticRecord.model_validate_json(text)
            if any(t.cause.data_type not in principal.read_types for t in record.additional_causes):
                denied.add(record.id)
                continue
            versions.setdefault(record.id, []).append(record)
        elif kind == "system":
            system = SystemRecord.model_validate_json(text)
            if (
                not record_input_types(system, Registry(specs, {}, frozenset(core)))
                <= principal.read_types
            ):
                denied.add(system.id)
                continue
            systems.setdefault(system.id, []).append(system)
        elif kind == "group":
            groups.append(json.loads(text))
        elif kind == "edge":
            edge = DependencyRecord.model_validate_json(text)
            group_key = (edge.dependent.id, edge.dependent.version)
            if edge.depends_on.data_type not in principal.read_types:
                blocked.add(group_key)
                continue
            edges.setdefault(group_key, []).append(edge)
            state.deps_by_target[edge.depends_on.id] = state.deps_by_target.get(
                edge.depends_on.id, frozenset()
            ) | {group_key}
        elif kind == "processed":
            consumer, event = json.loads(text)
            if can_use_consumer(conn, principal, consumer):
                state.processed[(consumer, event)] = None
    state.versions = {
        id: tuple(sorted(vs, key=lambda r: r.version))
        for id, vs in versions.items()
        if id not in denied
    }
    for id, history in systems.items():
        history.sort(key=lambda r: r.revision)
        state.system[id] = history[-1]
    state.completions = {
        eid: dtype for eid, dtype in state.completions.items() if eid in state.system
    }
    for id, version, dtype, who, when, hidden in groups:
        group_key = (id, version)
        if hidden:
            blocked.add(group_key)
        state.deps[group_key] = DependencySet(
            VersionRef(data_type=dtype, id=id, version=version),
            tuple(sorted(edges.get(group_key, []), key=lambda e: e.depends_on.id)),
            who,
            datetime.fromisoformat(when),
        )
    return MaterializedReader(
        state,
        Registry(specs, {}, frozenset(core)),
        principal,
        systems,
        denied,
        blocked,
        completion_index,
    )


def sweep_expired(dsn: str, *, batch_size: int = 100) -> int:
    """Deployment-only maintenance: one bounded batch, never loop until empty."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise InvalidRequest("invalid snapshot cleanup batch size")
    with connection(dsn) as conn, writer(conn):
        check_schema(conn)
        conn.execute("SET LOCAL transaction_timeout = '30s'")
        rows = conn.execute(
            "DELETE FROM storage.snapshot_leases WHERE snapshot_id IN "
            "(SELECT snapshot_id FROM storage.snapshot_leases WHERE expires_at<=clock_timestamp() "
            "ORDER BY expires_at,snapshot_id LIMIT %s) RETURNING snapshot_id",
            (batch_size,),
        ).fetchall()
        return len(rows)
