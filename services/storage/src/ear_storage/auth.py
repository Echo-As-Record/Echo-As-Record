"""Deployment-only principal administration and transaction-local authentication.

No automatic grants, including owner reads. A grant row means read permission;
can_subscribe is an additional capability on that row, never a standalone grant.
Do not expose AuthOperator through a token-authenticated transport.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field

import psycopg
from ear_contracts import new_id
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.errors import StorageError

from ear_storage.schema import check_schema, connection, module_schema, writer


class AuthenticationError(StorageError):
    """Transport mapping: 401. Never includes credentials or principal existence."""


@dataclass(frozen=True, slots=True)
class IssuedPrincipal:
    principal_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    module: str
    read_types: frozenset[str]
    subscribe_types: frozenset[str]

    def require_read(self, types: Iterable[str]) -> None:
        if not self.read_types.issuperset(types):
            raise PermissionDenied("access denied")

    def owns_consumer(self, consumer: str) -> bool:
        return consumer == self.module or consumer.startswith(self.module + ".")


def token_digest(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise AuthenticationError("invalid credentials")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def authenticate(conn: psycopg.Connection, digest: str) -> Principal:
    """Only under WRITER_GATE, READ COMMITTED, after any lock wait; never cached."""
    row = conn.execute(
        "SELECT id,module FROM storage.principals WHERE token_hash=%s AND active", (digest,)
    ).fetchone()
    if row is None:
        raise AuthenticationError("invalid credentials")
    grants = conn.execute(
        "SELECT data_type,can_subscribe FROM storage.type_grants WHERE principal_id=%s", (row[0],)
    ).fetchall()
    return Principal(
        row[0], row[1], frozenset(r[0] for r in grants), frozenset(r[0] for r in grants if r[1])
    )


def _replace_grants(
    conn: psycopg.Connection,
    principal_id: str,
    read_types: Iterable[str],
    subscribe_types: Iterable[str],
) -> None:
    reads, subscriptions = frozenset(read_types), frozenset(subscribe_types)
    if not subscriptions <= reads:
        raise InvalidRequest("subscription grants must be a subset of read grants")
    known = {r[0] for r in conn.execute("SELECT name FROM storage.registry")}
    if not reads <= known:
        raise InvalidRequest("invalid grant types")
    if not conn.execute("SELECT 1 FROM storage.principals WHERE id=%s", (principal_id,)).fetchone():
        raise InvalidRequest("invalid principal")
    conn.execute("DELETE FROM storage.type_grants WHERE principal_id=%s", (principal_id,))
    for dtype in sorted(reads):
        conn.execute(
            "INSERT INTO storage.type_grants VALUES (%s,%s,%s)",
            (principal_id, dtype, dtype in subscriptions),
        )


class AuthOperator:
    """Trusted deployment capability, NOT a user-facing service/registry method."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def issue_principal(
        self,
        module: str,
        *,
        read_types: Iterable[str] = (),
        subscribe_types: Iterable[str] = (),
    ) -> IssuedPrincipal:
        module_schema(module)
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            if not conn.execute(
                "SELECT 1 FROM storage.modules WHERE module=%s", (module,)
            ).fetchone():
                raise InvalidRequest("module not bootstrapped")
            token, principal_id = secrets.token_urlsafe(32), new_id()
            conn.execute(
                "INSERT INTO storage.principals (id,module,token_hash) VALUES (%s,%s,%s)",
                (principal_id, module, token_digest(token)),
            )
            _replace_grants(conn, principal_id, read_types, subscribe_types)
        # The only plaintext delivery. No retrieval endpoint and no plaintext DB column.
        return IssuedPrincipal(principal_id, token)

    def revoke(self, principal_id: str) -> None:
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            conn.execute("UPDATE storage.principals SET active=false WHERE id=%s", (principal_id,))

    def set_grants(
        self,
        principal_id: str,
        *,
        read_types: Iterable[str] = (),
        subscribe_types: Iterable[str] = (),
    ) -> None:
        """Replace the complete explicit grant set atomically; does not reactivate credentials."""
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            _replace_grants(conn, principal_id, read_types, subscribe_types)
