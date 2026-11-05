"""Service-internal PostgreSQL backend, NOT an authenticated plugin API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

import psycopg
from ear_contracts import Event, Registry, is_id, new_id, utcnow
from ear_storage_client import Context, InvalidRequest, RegisterDependencies, WriteOp, WriteResult
from ear_storage_client.engine import Abort, Transaction, canonical_json

from ear_storage.codec import (
    EVENT_ADAPTER,
    decode_request,
    decode_result,
    encode_request,
    encode_result,
)
from ear_storage.reader import PostgresReader, read_connection
from ear_storage.repository import Limits, Projection, persist, registry_from_db
from ear_storage.schema import (
    ProjectionLimit,
    SchemaMismatch,
    check_schema,
    connection,
    module_schema,
    one,
    writer,
)


class PostgresStorage(PostgresReader):
    def __init__(self, dsn: str, *, limits: Limits | None = None) -> None:
        super().__init__(dsn)
        self.limits = limits or Limits()
        with connection(dsn) as conn:
            check_schema(conn)
            if not conn.execute("SELECT 1 FROM storage.registry LIMIT 1").fetchone():
                raise SchemaMismatch("registry absent: run trusted bootstrap()")

    @property
    def registry(self) -> Registry:
        with read_connection(self._dsn) as conn:
            return registry_from_db(conn)

    def client(self, module: str) -> PostgresClient:
        """Bind a deployment-trusted service module, NEVER an untrusted HTTP field."""
        module_schema(module)
        with read_connection(self._dsn) as conn:
            if not conn.execute(
                "SELECT 1 FROM storage.modules WHERE module=%s", (module,)
            ).fetchone():
                raise InvalidRequest("module not bootstrapped")
        return PostgresClient(self, module)

    def outbox(
        self, *, after: int = 0, limit: int = 100
    ) -> tuple[tuple[int, tuple[Event, ...]], ...]:
        """Page by complete commit groups; no partial boundary. No relay/ack yet."""
        if not 1 <= limit <= 1000 or after < 0:
            raise InvalidRequest("invalid outbox page")
        with read_connection(self._dsn) as conn:
            rows = conn.execute(
                "SELECT commit_seq,event_text FROM storage.outbox WHERE commit_seq "
                "IN (SELECT DISTINCT commit_seq FROM storage.outbox WHERE commit_seq>%s "
                "ORDER BY commit_seq LIMIT %s) ORDER BY commit_seq,ordinal",
                (after, limit),
            )
            groups: dict[int, list[Event]] = {}
            for seq, text in rows:
                groups.setdefault(seq, []).append(EVENT_ADAPTER.validate_json(text))
            return tuple((seq, tuple(events)) for seq, events in groups.items())

    @property
    def commit_seq(self) -> int:
        with read_connection(self._dsn) as conn:
            return one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]


class PostgresClient(PostgresReader):
    def __init__(self, storage: PostgresStorage, module: str) -> None:
        super().__init__(storage._dsn)
        self._storage, self.module = storage, module

    @property
    def registry(self) -> Registry:
        return self._storage.registry

    def request_required_types(self, request_id: str) -> frozenset[str] | None:
        """Internal authorization preflight; not a substitute for future atomic auth checks."""
        with read_connection(self._dsn) as conn:
            row = conn.execute(
                "SELECT required_types_text FROM storage.idempotency "
                "WHERE scope=%s AND request_id=%s",
                ("internal:" + self.module, request_id),
            ).fetchone()
            return None if row is None else frozenset(json.loads(row[0]))

    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str | None = None
    ) -> WriteResult:
        request_id = new_id() if request_id is None else request_id
        if not is_id(request_id):
            raise InvalidRequest("request_id must be lowercase UUIDv7")
        limits = self._storage.limits
        ops = tuple(ops)
        if len(ops) > limits.max_batch_ops:
            raise ProjectionLimit("batch operation limit exceeded")
        # Freeze the canonical request before opening a DB transaction. Caller mutation
        # cannot change a batch after its idempotency fingerprint was computed.
        request_text = encode_request(ops, context)
        if len(request_text.encode()) > limits.max_request_bytes:
            raise ProjectionLimit("request byte limit exceeded")
        digest = hashlib.sha256(request_text.encode()).hexdigest()
        ops, context = decode_request(request_text)
        with connection(self._dsn) as conn, writer(conn):
            return self._write_locked(conn, ops, context, request_id, digest)

    def _write_locked(
        self,
        conn: psycopg.Connection,
        ops: Sequence[WriteOp],
        context: Context,
        request_id: str,
        digest: str,
        *,
        scope: str | None = None,
        required_types: frozenset[str] | None = None,
        authorize_replay: Callable[[frozenset[str]], None] | None = None,
    ) -> WriteResult:
        """Internal only: caller owns WRITER_GATE and authenticates AFTER acquiring it."""
        limits = self._storage.limits
        scope = "internal:" + self.module if scope is None else scope
        check_schema(conn)
        old = conn.execute(
            "SELECT fingerprint,result_text,required_types_text FROM storage.idempotency "
            "WHERE scope=%s AND request_id=%s",
            (scope, request_id),
        ).fetchone()
        if old is not None:
            if authorize_replay is not None:
                authorize_replay(frozenset(json.loads(old[2])))
            if old[0] != digest:
                raise InvalidRequest("request_id reused with a different payload/context")
            return decode_result(old[1])
        registry = registry_from_db(conn)
        before = Projection(conn, limits).load(ops, registry)
        txn = Transaction(
            before.copy(),
            registry,
            self.module,
            context,
            utcnow(),
            new_id(),
            authorize_inputs=authorize_replay,
        )
        try:
            txn.run(ops)
        except Abort as abort:
            result = WriteResult(batch_id=None, conflict=abort.conflict)
        else:
            if (
                len(txn.s.versions) > limits.max_graph_nodes
                or sum(len(g.dependencies) for g in txn.s.deps.values()) > limits.max_graph_edges
            ):
                raise ProjectionLimit("resulting graph exceeds validator limit")
            result = WriteResult(
                batch_id=txn.batch_id, results=tuple(txn.results), events=tuple(txn.events)
            )
        seq = one(
            conn,
            "UPDATE storage.meta SET commit_seq=commit_seq+1 WHERE singleton RETURNING commit_seq",
        )[0]
        if result.ok:
            persist(conn, before, txn, seq)
        required = set()
        for op in ops:
            for attr in ("data_type", "dependent_type", "target_type"):
                value = getattr(op, attr, None)
                if value:
                    required.add(value)
            record = getattr(op, "record", None)
            if record is not None:
                required.add(record.data_type)
            if isinstance(op, RegisterDependencies):
                required.update(item.data_type for item in op.inputs())
            cause = getattr(op, "cause", None)
            if cause is not None:
                required.add(cause.data_type)
        if result.conflict is not None:
            if result.conflict.data_type:
                required.add(result.conflict.data_type)
            required.update(node["data_type"] for node in result.conflict.details.get("path", []))
        conn.execute(
            "INSERT INTO storage.idempotency "
            "(module,request_id,fingerprint,result_text,required_types_text,commit_seq,scope) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                self.module,
                request_id,
                digest,
                encode_result(result),
                canonical_json(
                    sorted(
                        (required if required_types is None else required_types)
                        | txn.used_input_types
                    )
                ),
                seq,
                scope,
            ),
        )
        return result

    def snapshot(self):
        raise NotImplementedError("persistent snapshot leases are a later P1 slice")

    def subscribe(self, *args, **kwargs):
        raise NotImplementedError("remote subscription and relay belong to P2")
