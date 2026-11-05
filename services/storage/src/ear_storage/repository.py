"""Bounded SQL projection for the pure validator; never loads all historical state.

P1a deliberately scans all CURRENT semantic objects and their current dependency
sets (including superseded nodes before cycle filtering). Cost is O(current graph),
not O(version history). All row/edge/byte limits fail closed before hydration.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from ear_contracts import (
    DataTypeSpec,
    DependencyRecord,
    RawRecord,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    SystemRecord,
    VersionRef,
)
from ear_storage_client import (
    AppendRaw,
    AssertTypeRevision,
    Create,
    FinishReevaluation,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)
from ear_storage_client.engine import State, Transaction, canonical_json
from ear_storage_client.queries import DependencySet
from psycopg import sql

from ear_storage.schema import ProjectionLimit, SchemaMismatch, has_type_revisions, table


@dataclass(frozen=True)
class Limits:
    max_graph_nodes: int = 10_000
    max_graph_edges: int = 50_000
    max_projection_bytes: int = 32 * 1024 * 1024
    max_batch_ops: int = 2_000
    max_request_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            min(
                self.max_graph_nodes,
                self.max_graph_edges,
                self.max_projection_bytes,
                self.max_batch_ops,
                self.max_request_bytes,
            )
            < 1
        ):
            raise ValueError("limits must be positive")


def registry_from_db(conn: psycopg.Connection) -> Registry:
    types = {}
    core = set()
    for name, text, is_core in conn.execute(
        "SELECT name,spec_text,is_core FROM storage.registry ORDER BY name"
    ):
        types[name] = DataTypeSpec.from_json(name, json.loads(text))
        if is_core:
            core.add(name)
    publishers = {
        name: tuple(json.loads(text))
        for name, text in conn.execute("SELECT event_type,modules_text FROM storage.publishers")
    }
    return Registry(types, publishers, frozenset(core))


def table_name(kind: str) -> str:
    return {
        "raw_record": "raw_records",
        "semantic": "versions",
        "rule": "versions",
        "system_record": "system_records",
    }[kind]


class Projection:
    def __init__(self, conn: psycopg.Connection, limits: Limits) -> None:
        self.conn, self.limits = conn, limits
        self.bytes = 0

    def texts(
        self, query: sql.SQL | sql.Composed, params: Sequence[Any] = (), *, cap: int
    ) -> list[str]:
        row = self.conn.execute(
            sql.SQL(
                "SELECT count(*),coalesce(sum(octet_length(record_text)),0) FROM ({}) q"
            ).format(query),
            params,
        ).fetchone()
        assert row is not None
        if row[0] > cap or self.bytes + row[1] > self.limits.max_projection_bytes:
            raise ProjectionLimit("validator projection row/byte limit exceeded; nothing committed")
        self.bytes += row[1]
        return [r[0] for r in self.conn.execute(query, params)]

    def load(self, ops: Sequence[WriteOp], registry: Registry) -> State:
        s = State()
        needed = {
            op.record.data_type if isinstance(op, AppendRaw) else op.data_type
            for op in ops
            if isinstance(
                op, AppendRaw | AssertTypeRevision | Create | Update | Supersede | PutSystemRecord
            )
        }
        needed.update(
            registry.data_types[op.result.target_type].writer + "_completion"
            for op in ops
            if isinstance(op, FinishReevaluation) and op.result.target_type in registry.data_types
        )
        if has_type_revisions(self.conn):
            s.type_revisions = dict(
                self.conn.execute(
                    "SELECT data_type,revision FROM storage.type_revisions WHERE data_type=ANY(%s)",
                    (sorted(needed),),
                )
            )
        modules = [r[0] for r in self.conn.execute("SELECT module FROM storage.modules")]
        for module in modules:
            query = sql.SQL(
                "SELECT r.record_text FROM {} r JOIN storage.identity i "
                "ON i.id=r.id AND i.current_seq=r.seq WHERE i.kind IN ('semantic','rule')"
            )
            for text in self.texts(
                query.format(table(module, "versions")),
                cap=self.limits.max_graph_nodes - len(s.versions),
            ):
                record = SemanticRecord.model_validate_json(text)
                s.versions[record.id] = (record,)
                if record.dedup_key is not None:
                    s.semantic_keys[(record.data_type, record.dedup_key)] = record.id
        groups = self.conn.execute(
            "SELECT s.* FROM storage.current_dependency_sets s "
            "JOIN storage.identity i ON i.id=s.dependent_id AND i.current_seq=s.dependent_version "
            "WHERE i.kind IN ('semantic','rule') LIMIT %s",
            (self.limits.max_graph_nodes + 1,),
        ).fetchall()
        if len(groups) > self.limits.max_graph_nodes:
            raise ProjectionLimit("dependency set limit exceeded")
        edges = self.texts(
            sql.SQL(
                "SELECT e.record_text FROM storage.current_dependency_edges e "
                "JOIN storage.identity i ON i.id=e.dependent_id "
                "AND i.current_seq=e.dependent_version "
                "WHERE i.kind IN ('semantic','rule')"
            ),
            cap=self.limits.max_graph_edges,
        )
        by_key: dict[tuple[str, int], list[DependencyRecord]] = {}
        refs: set[tuple[str, int]] = set()
        ids: set[str] = set()
        recomputed_ids = {op.id for op in ops if isinstance(op, Create | Update)}
        for text in edges:
            dep = DependencyRecord.model_validate_json(text)
            key = (dep.dependent.id, dep.dependent.version)
            by_key.setdefault(key, []).append(dep)
            s.deps_by_target[dep.depends_on.id] = s.deps_by_target.get(
                dep.depends_on.id, frozenset()
            ) | {key}
            if dep.dependent.id in recomputed_ids:
                refs.add((dep.depends_on.id, dep.depends_on.version))
        for id, version, _seq, dtype, who, when in groups:
            key = (id, version)
            s.deps[key] = DependencySet(
                VersionRef(data_type=dtype, id=id, version=version),
                tuple(sorted(by_key.get(key, []), key=lambda d: d.depends_on.id)),
                who,
                when,
            )
        raw_keys = set()
        processed_keys = set()
        published_ids = set()
        request_ids = set()
        for op in ops:
            if isinstance(op, FinishReevaluation):
                request_ids.add(op.result.request_event_id)
                ids.update((op.result.request_event_id, op.result.target_id))
                for cause in op.result.extra_causes:
                    refs.add((cause.data_id, cause.new_version))
                    if cause.old_version is not None:
                        refs.add((cause.data_id, cause.old_version))
            for attr in ("id", "dependent_id", "target_id"):
                value = getattr(op, attr, None)
                if value is not None:
                    ids.add(value)
            if isinstance(op, AppendRaw):
                ids.add(op.record.id)
                raw_keys.add((op.record.data_type, op.record.adapter_version, op.record.dedup_key))
            if isinstance(op, RegisterDependencies):
                refs.update((item.id, item.version) for item in op.inputs())
            if isinstance(op, Create | Update | PutSystemRecord):
                try:
                    time_from = registry.get(op.data_type).time_from
                except LookupError:
                    time_from = None  # The pure engine reports the invalid type with its op index.
                value = op.payload.get(time_from) if time_from is not None else None
                if isinstance(value, str):
                    ids.add(value)
            if isinstance(op, MarkProcessed):
                processed_keys.add((op.consumer, op.event_id))
            if isinstance(op, PublishReevaluation) and op.event_id is not None:
                published_ids.add(op.event_id)
        if len(refs) > self.limits.max_graph_edges:
            raise ProjectionLimit("batch referenced-version limit exceeded")
        if raw_keys:
            keys = sorted(raw_keys)
            for dtype, adapter, dedup, id in self.conn.execute(
                "SELECT i.data_type,i.adapter_version,i.dedup_key,i.id FROM storage.identity i "
                "JOIN unnest(%s::text[],%s::text[],%s::text[]) k(t,a,d) "
                "ON i.data_type=k.t AND i.adapter_version=k.a AND i.dedup_key=k.d "
                "WHERE i.kind='raw_record'",
                tuple([k[i] for k in keys] for i in range(3)),
            ):
                s.raw_keys[(dtype, adapter, dedup)] = id
        if processed_keys:
            keys = sorted(processed_keys)
            for consumer, event in self.conn.execute(
                "SELECT p.consumer,p.event_id FROM storage.processed p "
                "JOIN unnest(%s::text[],%s::text[]) k(c,e) "
                "ON p.consumer=k.c AND p.event_id=k.e",
                tuple([k[i] for k in keys] for i in range(2)),
            ):
                s.processed[(consumer, event)] = None
        if published_ids:
            s.published.update(
                (r[0], None)
                for r in self.conn.execute(
                    "SELECT event_id FROM storage.published WHERE event_id=ANY(%s)",
                    (sorted(published_ids),),
                )
            )
        if request_ids:
            for text in self.texts(
                sql.SQL(
                    "SELECT event_text AS record_text FROM storage.outbox WHERE event_id=ANY(%s)"
                ),
                (sorted(request_ids),),
                cap=len(request_ids),
            ):
                event = json.loads(text)
                if event.get("event_type") == "reevaluation_requested":
                    request = ReevaluationRequested.model_validate(event)
                    s.requests[request.event_id] = request
            s.completions.update(
                self.conn.execute(
                    "SELECT event_id,result_type FROM storage.request_completions "
                    "WHERE event_id=ANY(%s)",
                    (sorted(request_ids),),
                )
            )
        ids.update(id for id, _ in refs)
        identities = self.conn.execute(
            "SELECT id,module,kind,current_seq FROM storage.identity WHERE id=ANY(%s)",
            (sorted(ids),),
        ).fetchall()
        s.occupied.update(r[0] for r in identities)
        for module in modules:
            for kind in ("raw_record", "system_record"):
                wanted = [r[0] for r in identities if r[1] == module and r[2] == kind]
                if not wanted:
                    continue
                query = sql.SQL(
                    "SELECT r.record_text FROM {} r JOIN storage.identity i "
                    "ON i.id=r.id AND i.current_seq=r.seq WHERE r.id=ANY(%s)"
                )
                for text in self.texts(
                    query.format(table(module, table_name(kind))), (wanted,), cap=len(wanted)
                ):
                    if kind == "raw_record":
                        raw = RawRecord.model_validate_json(text)
                        s.raw[raw.id] = raw
                        s.raw_keys[(raw.data_type, raw.adapter_version, raw.dedup_key)] = raw.id
                    else:
                        system = SystemRecord.model_validate_json(text)
                        s.system[system.id] = system
            wanted_refs = [
                (id, ver)
                for id, ver in refs
                if id in s.versions
                and s.versions[id][-1].writer == module
                and s.versions[id][-1].version != ver
            ]
            if wanted_refs:
                query = sql.SQL(
                    "SELECT v.record_text FROM {} v "
                    "JOIN unnest(%s::text[],%s::int[]) k(id,seq) ON v.id=k.id AND v.seq=k.seq"
                ).format(table(module, "versions"))
                params = [[pair[i] for pair in sorted(wanted_refs)] for i in range(2)]
                for text in self.texts(query, params, cap=len(wanted_refs)):
                    record = SemanticRecord.model_validate_json(text)
                    s.prior[(record.id, record.version)] = record
        return s


def persist(conn: psycopg.Connection, before: State, txn: Transaction, seq: int) -> None:
    s = txn.s
    if has_type_revisions(conn):
        for dtype, revision in s.type_revisions.items():
            delta = revision - before.type_revisions.get(dtype, 0)
            if delta:
                updated = conn.execute(
                    "UPDATE storage.type_revisions SET revision=revision+%s WHERE data_type=%s",
                    (delta, dtype),
                )
                if updated.rowcount != 1:
                    raise SchemaMismatch("missing collection revision")
    records: list[RawRecord | SemanticRecord | SystemRecord] = list(
        r for id, r in s.raw.items() if id not in before.raw
    )
    records.extend(
        r
        for id, vs in s.versions.items()
        for r in vs
        if not before.versions.get(id) or r.version > before.versions[id][-1].version
    )
    records.extend(txn.system_writes)
    for record in records:
        spec = txn.registry.get(record.data_type)
        revision = record.revision if isinstance(record, SystemRecord) else record.version
        conn.execute(
            "INSERT INTO storage.identity "
            "(id,module,data_type,kind,current_seq,dedup_key,adapter_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) "
            "DO UPDATE SET current_seq=excluded.current_seq",
            (
                record.id,
                spec.writer,
                record.data_type,
                spec.kind,
                revision,
                getattr(record, "dedup_key", None),
                getattr(record, "adapter_version", None),
            ),
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (id,seq,data_type,record_text,payload_text,"
                "event_start,event_end,source_user,superseded,commit_seq) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            ).format(table(spec.writer, table_name(spec.kind))),
            (
                record.id,
                revision,
                record.data_type,
                canonical_json(record.model_dump(mode="json")),
                canonical_json(record.payload),
                record.event_start,
                record.event_end,
                record.source.user if isinstance(record, RawRecord) else None,
                isinstance(record, SemanticRecord) and record.superseded,
                seq,
            ),
        )
    for key, group in s.deps.items():
        if before.deps.get(key) == group:
            continue
        conn.execute(
            "INSERT INTO storage.dependency_sets VALUES (%s,%s,%s,%s,%s,%s)",
            (*key, seq, group.dependent.data_type, group.registered_by, group.registered_at),
        )
        for dep in group.dependencies:
            conn.execute(
                "INSERT INTO storage.dependency_edges VALUES (%s,%s,%s,%s,%s)",
                (*key, seq, dep.depends_on.id, canonical_json(dep.model_dump(mode="json"))),
            )
    for consumer, event in s.processed.keys() - before.processed.keys():
        conn.execute("INSERT INTO storage.processed VALUES (%s,%s,%s)", (consumer, event, seq))
    for event in s.published.keys() - before.published.keys():
        conn.execute("INSERT INTO storage.published VALUES (%s,%s)", (event, seq))
    for event_id in s.completions.keys() - before.completions.keys():
        conn.execute(
            "INSERT INTO storage.request_completions(event_id,result_type,commit_seq) "
            "VALUES (%s,%s,%s)",
            (event_id, s.completions[event_id], seq),
        )
    for ordinal, event in enumerate(txn.events):
        conn.execute(
            "INSERT INTO storage.outbox (commit_seq,ordinal,event_id,event_text) "
            "VALUES (%s,%s,%s,%s)",
            (seq, ordinal, event.event_id, canonical_json(event.model_dump(mode="json"))),
        )
