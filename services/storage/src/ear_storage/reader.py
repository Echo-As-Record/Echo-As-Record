"""Direct SQL readers; internal only, no plugin/HTTP authorization boundary."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from ear_contracts import (
    DependencyRecord,
    RawRecord,
    Record,
    ReevaluationResult,
    SemanticRecord,
    SystemRecord,
    VersionRef,
)
from ear_storage_client import InvalidRequest, Query
from ear_storage_client.completion import _indexed_result
from ear_storage_client.engine import State, canonical_json, stale
from ear_storage_client.queries import DependencySet, Page, QueryItem, StaleDependency
from psycopg import sql

from ear_storage.causality import visible_trace_sql
from ear_storage.repository import registry_from_db, table_name
from ear_storage.schema import connection, has_type_revisions, one, table


@contextmanager
def read_connection(dsn: str) -> Iterator[psycopg.Connection]:
    with connection(dsn) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield conn


def get_record(
    conn: psycopg.Connection, id: str, version: int | None = None, *, system: bool = False
) -> QueryItem | None:
    row = conn.execute(
        "SELECT module,kind,current_seq FROM storage.identity WHERE id=%s", (id,)
    ).fetchone()
    if row is None or (row[1] == "system_record") != system:
        return None
    module, kind, current = row
    row = conn.execute(
        sql.SQL("SELECT record_text FROM {} WHERE id=%s AND seq=%s").format(
            table(module, table_name(kind))
        ),
        (id, current if version is None else version),
    ).fetchone()
    if row is None:
        return None
    model = RawRecord if kind == "raw_record" else SystemRecord if system else SemanticRecord
    return model.model_validate_json(row[0])


def group(conn: psycopg.Connection, id: str, version: int | None) -> DependencySet | None:
    if version is None:
        row = conn.execute(
            "SELECT current_seq FROM storage.identity WHERE id=%s AND kind IN ('semantic','rule')",
            (id,),
        ).fetchone()
        if row is None:
            return None
        version = row[0]
    row = conn.execute(
        "SELECT dependent_type,registered_by,registered_at,commit_seq "
        "FROM storage.current_dependency_sets WHERE dependent_id=%s AND dependent_version=%s",
        (id, version),
    ).fetchone()
    if row is None:
        return None
    dtype, who, when, seq = row
    edges = tuple(
        DependencyRecord.model_validate_json(r[0])
        for r in conn.execute(
            "SELECT record_text FROM storage.dependency_edges WHERE dependent_id=%s "
            "AND dependent_version=%s AND commit_seq=%s ORDER BY target_id",
            (id, version, seq),
        )
    )
    assert version is not None
    return DependencySet(VersionRef(data_type=dtype, id=id, version=version), edges, who, when)


def dependents(
    conn: psycopg.Connection, ids: Sequence[str], current: bool
) -> list[DependencyRecord]:
    query = "SELECT e.record_text FROM storage.current_dependency_edges e "
    if current:
        query += "JOIN storage.identity i ON i.id=e.dependent_id "
        query += "AND i.current_seq=e.dependent_version "
    query += "WHERE e.target_id=ANY(%s) "
    if current:
        # Superseded current records cannot be dependents; use identity + precise row lookup below.
        query += "AND i.kind IN ('semantic','rule') "
    query += "ORDER BY e.target_id,e.dependent_id,e.dependent_version"
    edges = [DependencyRecord.model_validate_json(r[0]) for r in conn.execute(query, (list(ids),))]
    if current:
        active: dict[str, bool] = {}
        for edge in edges:
            id = edge.dependent.id
            if id not in active:
                record = get_record(conn, id)
                active[id] = isinstance(record, SemanticRecord) and not record.superseded
        edges = [edge for edge in edges if active[edge.dependent.id]]
    return edges


class PostgresReader:
    read_types: frozenset[str] | None = None

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connection(self):
        return read_connection(self._dsn)

    def type_revision(self, data_type: str) -> int:
        with self._connection() as conn:
            if not has_type_revisions(conn):
                raise InvalidRequest("collection revisions require schema migration")
            row = conn.execute(
                "SELECT revision FROM storage.type_revisions WHERE data_type=%s", (data_type,)
            ).fetchone()
            if row is None:
                raise InvalidRequest("unknown data type")
            return row[0]

    def record_types(self, ids: Sequence[str]) -> dict[str, str]:
        """Internal auth preflight metadata only; no payloads, credentials or DB config."""
        with self._connection() as conn:
            return dict(
                conn.execute(
                    "SELECT id,data_type FROM storage.identity WHERE id=ANY(%s)", (list(ids),)
                )
            )

    def get(self, id: str) -> Record | None:
        with self._connection() as conn:
            record = get_record(conn, id)
            assert record is None or isinstance(record, RawRecord | SemanticRecord)
            return record

    def get_version(self, id: str, version: int) -> Record | None:
        with self._connection() as conn:
            record = get_record(conn, id, version)
            assert record is None or isinstance(record, RawRecord | SemanticRecord)
            return record

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        with self._connection() as conn:
            result = {}
            rows = conn.execute(
                "SELECT id,module,kind,current_seq FROM storage.identity "
                "WHERE id=ANY(%s) AND kind <> 'system_record'",
                (list(ids),),
            ).fetchall()
            for module, kind in {(r[1], r[2]) for r in rows}:
                wanted = [r[0] for r in rows if (r[1], r[2]) == (module, kind)]
                query = sql.SQL(
                    "SELECT r.record_text FROM {} r JOIN storage.identity i "
                    "ON i.id=r.id AND i.current_seq=r.seq WHERE r.id=ANY(%s)"
                ).format(table(module, table_name(kind)))
                for (text,) in conn.execute(query, (wanted,)):
                    model = RawRecord if kind == "raw_record" else SemanticRecord
                    record = model.model_validate_json(text)
                    result[record.id] = record
            return result

    def history(self, id: str) -> list[SemanticRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT module FROM storage.identity WHERE id=%s AND kind IN ('semantic','rule')",
                (id,),
            ).fetchone()
            if row is None:
                return []
            return [
                SemanticRecord.model_validate_json(r[0])
                for r in conn.execute(
                    sql.SQL("SELECT record_text FROM {} WHERE id=%s ORDER BY seq").format(
                        table(row[0], "versions")
                    ),
                    (id,),
                )
            ]

    def get_reevaluation_result(self, request_event_id: str) -> ReevaluationResult | None:
        with self._connection() as conn:
            if conn.execute("SELECT to_regclass('storage.request_completions')").fetchone() == (
                None,
            ):
                raise InvalidRequest("authoritative completion reads require schema migration")
            row = conn.execute(
                "SELECT result_type FROM storage.request_completions WHERE event_id=%s",
                (request_event_id,),
            ).fetchone()
            if row is None:
                return None
            record = get_record(conn, request_event_id, system=True)
            assert record is None or isinstance(record, SystemRecord)
            return _indexed_result(registry_from_db(conn), request_event_id, row[0], record)

    def get_system_record(self, id: str) -> SystemRecord | None:
        with self._connection() as conn:
            record = get_record(conn, id, system=True)
            assert record is None or isinstance(record, SystemRecord)
            return record

    def system_history(self, id: str) -> list[SystemRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT module FROM storage.identity WHERE id=%s AND kind='system_record'", (id,)
            ).fetchone()
            if row is None:
                return []
            return [
                SystemRecord.model_validate_json(r[0])
                for r in conn.execute(
                    sql.SQL("SELECT record_text FROM {} WHERE id=%s ORDER BY seq").format(
                        table(row[0], "system_records")
                    ),
                    (id,),
                )
            ]

    def _query(self, conn: psycopg.Connection, query: Query, *, count: bool) -> Any:
        registry = registry_from_db(conn)
        try:
            spec = registry.get(query.data_type)
        except LookupError as exc:
            raise InvalidRequest(str(exc)) from None
        if query.source_user is not None and spec.kind != "raw_record":
            raise InvalidRequest("source_user only applies to raw records")
        params: list[Any] = [query.data_type]
        predicates: list[sql.SQL | sql.Composed] = [sql.SQL("r.data_type=%s")]
        if self.read_types is not None:
            predicates.append(visible_trace_sql(self.read_types))
        if spec.versioned and not query.include_superseded:
            predicates.append(sql.SQL("NOT r.superseded"))
        if query.source_user is not None:
            predicates.append(sql.SQL("r.source_user=%s"))
            params.append(query.source_user)
        if query.time is not None:
            if query.time.start == query.time.end:
                predicates.append(sql.SQL("false"))
            else:
                predicates.append(
                    sql.SQL(
                        "((r.event_start=r.event_end AND "
                        "r.event_start >= %s AND r.event_start < %s) "
                        "OR (r.event_start<>r.event_end "
                        "AND r.event_start < %s AND r.event_end > %s))"
                    )
                )
                params.extend([query.time.start, query.time.end, query.time.end, query.time.start])
        for key, value in query.where.items():
            predicates.append(sql.SQL("(r.payload_text::json -> %s::text)::text = %s"))
            params.extend([key, canonical_json(value)])
        for key, value in query.contains.items():
            predicates.append(
                sql.SQL(
                    "EXISTS (SELECT 1 FROM json_array_elements(CASE WHEN "
                    "json_typeof(r.payload_text::json -> %s::text)='array' THEN "
                    "r.payload_text::json -> %s::text ELSE '[]'::json END) v WHERE v::text=%s)"
                )
            )
            params.extend([key, key, canonical_json(value)])
        if query.unreferenced_by is not None:
            dtype, key = query.unreferenced_by
            try:
                other = registry.get(dtype)
            except LookupError as exc:
                raise InvalidRequest(str(exc)) from None
            if not other.versioned:
                raise InvalidRequest("unreferenced_by requires a versioned type")
            predicates.append(
                sql.SQL(
                    "NOT EXISTS (SELECT 1 FROM {} u JOIN storage.identity j "
                    "ON j.id=u.id AND j.current_seq=u.seq "
                    "WHERE u.data_type=%s AND NOT u.superseded "
                    "AND json_typeof(u.payload_text::json -> %s::text)='string' "
                    "AND u.payload_text::json ->> %s::text=r.id)"
                ).format(table(other.writer, "versions"))
            )
            params.extend([dtype, key, key])
        statement = sql.SQL(
            "SELECT {} FROM {} r JOIN storage.identity i "
            "ON i.id=r.id AND i.current_seq=r.seq WHERE {}"
        ).format(
            sql.SQL("count(*)" if count else "r.record_text"),
            table(spec.writer, table_name(spec.kind)),
            sql.SQL(" AND ").join(predicates),
        )
        if count:
            return one(conn, statement, params)[0]
        try:
            offset = 0 if query.cursor is None else int(query.cursor)
        except ValueError:
            raise InvalidRequest("invalid cursor") from None
        if offset < 0:
            raise InvalidRequest("invalid cursor")
        statement += sql.SQL(
            " ORDER BY "
            + ("r.id" if query.order == "id" else "r.event_start ASC NULLS LAST,r.id")
            + " LIMIT %s OFFSET %s"
        )
        rows = conn.execute(statement, [*params, query.limit + 1, offset]).fetchall()
        model = (
            RawRecord
            if spec.kind == "raw_record"
            else SemanticRecord
            if spec.versioned
            else SystemRecord
        )
        return Page(
            tuple(model.model_validate_json(r[0]) for r in rows[: query.limit]),
            str(offset + query.limit) if len(rows) > query.limit else None,
        )

    def query(self, query: Query) -> Page:
        with self._connection() as conn:
            return self._query(conn, query, count=False)

    def count(self, query: Query) -> int:
        with self._connection() as conn:
            return self._query(conn, query, count=True)

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        with self._connection() as conn:
            return group(conn, dependent_id, version)

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        found = self.dependency_set(dependent_id, version)
        return [] if found is None else list(found.dependencies)

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        with self._connection() as conn:
            return dependents(conn, [target_id], only_current_dependents)

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        with self._connection() as conn:
            edges = dependents(conn, list(dict.fromkeys(targets)), only_current_dependents)
            state = State()
            for id in {e.depends_on.id for e in edges}:
                record = get_record(conn, id)
                if isinstance(record, RawRecord):
                    state.raw[id] = record
                elif isinstance(record, SemanticRecord):
                    state.versions[id] = (record,)
            for edge in edges:
                ref = edge.depends_on
                record = get_record(conn, ref.id, ref.version)
                if isinstance(record, RawRecord | SemanticRecord):
                    state.prior[(ref.id, ref.version)] = record
            # Preserve the caller's target order, matching the reference Reader.
            order = {id: index for index, id in enumerate(dict.fromkeys(targets))}
            edges.sort(key=lambda e: (order[e.depends_on.id], e.dependent.id, e.dependent.version))
            result = []
            for edge in edges:
                ref = edge.depends_on
                current = state.current(ref.id)
                if current is not None and stale(state, ref, edge.fields):
                    result.append(
                        StaleDependency(
                            edge.dependent,
                            ref,
                            current.version,
                            isinstance(current, SemanticRecord) and current.superseded,
                            edge.fields,
                        )
                    )
            return result

    def is_processed(self, consumer: str, event_id: str) -> bool:
        with self._connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM storage.processed WHERE consumer=%s AND event_id=%s",
                    (consumer, event_id),
                ).fetchone()
                is not None
            )


class ConnectionBoundReader(PostgresReader):
    """Internal reader borrowing an already authorized short transaction; never escapes it."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        yield self._conn
