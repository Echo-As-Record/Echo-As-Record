"""Token-authenticated service with bounded persistent leases; HTTP is in http.py.

Reads deliberately share WRITER_GATE with writes and administration. Revocation
commits only after earlier admitted operations finish; any operation admitted
after it sees fresh credentials/grants. No lock survives a method return.
Owner writes need active identity and ownership, not an implicit owner read grant.
Write receipts (including replay) are metadata, not a read of the owner's payload.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from typing import Any

import psycopg
from ear_contracts import (
    DependencyRecord,
    Record,
    Registry,
    SemanticRecord,
    SystemRecord,
    is_id,
    new_id,
)
from ear_storage_client import (
    AppendRaw,
    Context,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    Query,
    RegisterDependencies,
    WriteOp,
    WriteResult,
)
from ear_storage_client.queries import DependencySet, Page, StaleDependency
from ear_storage_client.results import Conflict

from ear_storage.auth import AuthenticationError, Principal, authenticate, token_digest
from ear_storage.backend import PostgresClient, PostgresStorage
from ear_storage.codec import decode_request, encode_request
from ear_storage.reader import ConnectionBoundReader
from ear_storage.repository import Limits, registry_from_db
from ear_storage.schema import ProjectionLimit, check_schema, connection, writer
from ear_storage.snapshots import (
    SnapshotLease,
    SnapshotLimits,
    SnapshotUnavailable,
    create_lease,
    load_reader,
    validate_lease,
)


def _write_permissions(
    principal: Principal, reader: ConnectionBoundReader, registry: Registry, ops: Sequence[WriteOp]
) -> frozenset[str]:
    reads: set[str] = set()
    referenced_ids: list[str] = []
    for op in ops:
        if isinstance(op, MarkProcessed):
            if not principal.owns_consumer(op.consumer):
                raise PermissionDenied("access denied")
            continue
        if isinstance(op, PublishReevaluation):
            if principal.module not in registry.publishers_of("reevaluation_requested"):
                raise PermissionDenied("access denied")
            reads.update((op.target_type, op.cause.data_type))
            referenced_ids.extend((op.target_id, op.cause.data_id))
            continue
        dtype = (
            op.record.data_type
            if isinstance(op, AppendRaw)
            else op.dependent_type
            if isinstance(op, RegisterDependencies)
            else op.data_type
        )
        spec = registry.data_types.get(dtype)
        if spec is None or spec.writer != principal.module:
            raise PermissionDenied("access denied")
        if isinstance(op, RegisterDependencies):
            for item in op.inputs():
                reads.add(item.data_type)
                referenced_ids.append(item.id)
        # Implicit time-source reads must not become an existence/timestamp oracle.
        if spec.time_from is not None:
            payload = getattr(op, "payload", {})
            source = payload.get(spec.time_from)
            if isinstance(source, str):
                referenced_ids.append(source)
    reads.update(reader.record_types(referenced_ids).values())
    principal.require_read(reads)
    return frozenset(reads)


class StorageService:
    """Trusted composition root. Only client(token) belongs at an untrusted boundary.

    AuthOperator is intentionally separate, never accessible through AuthenticatedClient.
    Python object introspection is not a sandbox; plugins will use the future transport.
    """

    def __init__(
        self,
        dsn: str,
        *,
        limits: Limits | None = None,
        snapshot_limits: SnapshotLimits | None = None,
    ) -> None:
        self._backend = PostgresStorage(dsn, limits=limits)
        self._snapshot_limits = snapshot_limits or SnapshotLimits()

    def client(self, token: str) -> AuthenticatedClient:
        return AuthenticatedClient(self._backend, token_digest(token), self._snapshot_limits)

    def create_snapshot(self, token: str, *, ttl_seconds: int | None = None) -> SnapshotLease:
        return self.client(token).create_snapshot(ttl_seconds=ttl_seconds)

    def read_snapshot(self, token: str, snapshot_id: str) -> SnapshotReader:
        return self.client(token).read_snapshot(snapshot_id)

    def release_snapshot(self, token: str, snapshot_id: str) -> None:
        self.client(token).release_snapshot(snapshot_id)


class AuthorizedReaderMethods:
    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def get(self, id: str) -> Record | None:
        return self._read("get", id)

    def get_version(self, id: str, version: int) -> Record | None:
        return self._read("get_version", id, version)

    def history(self, id: str) -> list[SemanticRecord]:
        return self._read("history", id)

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        return self._read("get_many", ids)

    def get_system_record(self, id: str) -> SystemRecord | None:
        return self._read("get_system_record", id)

    def system_history(self, id: str) -> list[SystemRecord]:
        return self._read("system_history", id)

    def query(self, query: Query) -> Page:
        return self._read("query", query)

    def count(self, query: Query) -> int:
        return self._read("count", query)

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        return self._read("dependency_set", dependent_id, version)

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        found = self.dependency_set(dependent_id, version)
        return [] if found is None else list(found.dependencies)

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        return self._read(
            "dependents_of", target_id, only_current_dependents=only_current_dependents
        )

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        return self._read(
            "stale_dependencies", targets, only_current_dependents=only_current_dependents
        )

    def is_processed(self, consumer: str, event_id: str) -> bool:
        return self._read("is_processed", consumer, event_id)


class AuthenticatedClient(AuthorizedReaderMethods):
    def __init__(
        self, backend: PostgresStorage, digest: str, snapshot_limits: SnapshotLimits | None = None
    ) -> None:
        self._backend, self._digest = backend, digest
        self._snapshot_limits = snapshot_limits or SnapshotLimits()

    @contextmanager
    def _session(self) -> Iterator[tuple[psycopg.Connection, Principal, ConnectionBoundReader]]:
        with connection(self._backend._dsn) as conn, writer(conn):
            check_schema(conn)
            principal = authenticate(conn, self._digest)
            yield conn, principal, ConnectionBoundReader(conn)

    @property
    def module(self) -> str:
        with self._session() as (_conn, principal, _reader):
            return principal.module

    @property
    def registry(self) -> Registry:
        with self._session() as (conn, principal, _reader):
            registry = registry_from_db(conn)
            types = {
                name: spec
                for name, spec in registry.data_types.items()
                if name in principal.read_types or spec.writer == principal.module
            }
            publishers = {
                event: (principal.module,)
                for event, modules in registry.event_publishers.items()
                if principal.module in modules
            }
            return Registry(types, publishers, registry.core_types & frozenset(types))

    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._session() as (_conn, principal, reader):

            def visible(ids: Sequence[str]) -> set[str]:
                return {
                    id
                    for id, dtype in reader.record_types(ids).items()
                    if dtype in principal.read_types
                }

            if method in {"query", "count"}:
                query = args[0]
                principal.require_read([query.data_type])
                if query.unreferenced_by is not None:
                    principal.require_read([query.unreferenced_by[0]])
            elif method == "is_processed":
                if not principal.owns_consumer(args[0]):
                    return False
            elif method == "get_many":
                args = (list(visible(args[0])),)
            elif method == "stale_dependencies":
                allowed = visible(args[0])
                args = ([id for id in args[0] if id in allowed],)
            elif not visible([args[0]]):
                return [] if method in {"history", "system_history", "dependents_of"} else None
            result = getattr(reader, method)(*args, **kwargs)
            if method == "dependency_set" and result is not None:
                # Never pretend a redacted set is complete (especially an empty set).
                principal.require_read(edge.depends_on.data_type for edge in result.dependencies)
            elif method in {"dependents_of", "stale_dependencies"} and result is not None:
                result = [
                    edge
                    for edge in result
                    if edge.dependent.data_type in principal.read_types
                    and edge.depends_on.data_type in principal.read_types
                ]
            return result

    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str | None = None
    ) -> WriteResult:
        request_id = new_id() if request_id is None else request_id
        if not is_id(request_id):
            raise InvalidRequest("invalid request_id")
        limits = self._backend.limits
        ops = tuple(ops)
        if len(ops) > limits.max_batch_ops:
            raise ProjectionLimit("batch operation limit exceeded")
        text = encode_request(ops, context)
        if len(text.encode()) > limits.max_request_bytes:
            raise ProjectionLimit("request byte limit exceeded")
        ops, context = decode_request(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._session() as (conn, principal, reader):
            required = _write_permissions(principal, reader, registry_from_db(conn), ops)
            try:
                result = PostgresClient(self._backend, principal.module)._write_locked(
                    conn,
                    ops,
                    context,
                    request_id,
                    digest,
                    scope="principal:" + principal.id,
                    required_types=required,
                    authorize_replay=principal.require_read,
                )
            except PermissionDenied:
                raise PermissionDenied("access denied") from None
            except InvalidRequest:
                # Validator diagnostics can contain foreign owners, ids or payload values.
                raise InvalidRequest("invalid write request") from None
            if result.conflict is not None:
                conflict = result.conflict
                result = replace(
                    result, conflict=Conflict(conflict.index, conflict.reason, "write conflict")
                )
            return result

    def create_snapshot(self, *, ttl_seconds: int | None = None) -> SnapshotLease:
        """Create and commit before returning; descriptor.to_json() is transport-ready."""
        with self._session() as (conn, principal, _reader):
            return create_lease(conn, principal, self._snapshot_limits, ttl_seconds)

    @contextmanager
    def _lease_session(self, snapshot_id: str):
        try:
            with self._session() as (conn, principal, _reader):
                conn.execute("SET LOCAL transaction_timeout = '30s'")
                validate_lease(conn, principal, snapshot_id)
                yield conn, principal
        except AuthenticationError:
            raise SnapshotUnavailable("snapshot unavailable") from None

    def read_snapshot(self, snapshot_id: str) -> SnapshotReader:
        """Return a handle, not a cached authorized reader; every method revalidates."""
        with self._lease_session(snapshot_id):
            pass
        return SnapshotReader(self, snapshot_id)

    def release_snapshot(self, snapshot_id: str) -> None:
        """Only the active owner may release an unexpired lease; otherwise generic denial."""
        with self._lease_session(snapshot_id) as (conn, _principal):
            conn.execute("DELETE FROM storage.snapshot_leases WHERE snapshot_id=%s", (snapshot_id,))

    @contextmanager
    def snapshot(self) -> Iterator[SnapshotReader]:
        lease = self.create_snapshot()
        try:
            yield self.read_snapshot(lease.snapshot_id)
        finally:
            # Revoked/expired leases are reclaimed by deployment-only sweeping.
            with suppress(SnapshotUnavailable):
                self.release_snapshot(lease.snapshot_id)

    def subscribe(self, *args, **kwargs):
        raise NotImplementedError("authorized remote subscription and relay belong to P2")


class SnapshotReader(AuthorizedReaderMethods):
    """Lease-bound Reader. No open connection, transaction or cached data between calls."""

    def __init__(self, client: AuthenticatedClient, snapshot_id: str) -> None:
        self._client, self.snapshot_id = client, snapshot_id

    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._client._lease_session(self.snapshot_id) as (conn, principal):
            reader = load_reader(conn, principal, self.snapshot_id, self._client._snapshot_limits)
            result = reader.authorized_read(method, *args, **kwargs)
            validate_lease(conn, principal, self.snapshot_id)
            return result
