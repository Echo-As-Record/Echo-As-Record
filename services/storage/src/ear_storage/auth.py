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
from ear_storage_client.consumer_wire import ConsumerRequest
from ear_storage_client.errors import StorageError

from ear_storage.schema import check_schema, connection, module_schema, one, writer


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
    admin: bool = False

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
    admin = False
    if conn.execute("SELECT to_regclass('storage.module_lifecycle')").fetchone() != (None,):
        lifecycle = conn.execute(
            "SELECT enabled,resources_ready FROM storage.module_lifecycle WHERE module=%s",
            (row[1],),
        ).fetchone()
        if lifecycle is not None:
            if not lifecycle[0] or not lifecycle[1]:
                raise AuthenticationError("invalid credentials")
            allowed = dict(
                conn.execute(
                    "SELECT data_type,can_subscribe FROM storage.module_permissions "
                    "WHERE module=%s",
                    (row[1],),
                ).fetchall()
            )
            grants = [(n, sub and allowed[n]) for n, sub in grants if n in allowed]
        admin = one(conn, "SELECT admin FROM storage.principals WHERE id=%s", (row[0],))[0]
    return Principal(
        row[0],
        row[1],
        frozenset(r[0] for r in grants),
        frozenset(r[0] for r in grants if r[1]),
        admin,
    )


def can_use_consumer(conn: psycopg.Connection, principal: Principal, name: str) -> bool:
    """Legacy namespaces survive migration; registered names have an exclusive owner."""
    if not principal.owns_consumer(name):
        return False
    # Only relevant while exercising an explicit old-revision migration fixture.
    if conn.execute("SELECT to_regclass('storage.consumers')").fetchone() == (None,):
        return True
    row = conn.execute(
        "SELECT principal_id,enabled FROM storage.consumers WHERE name=%s", (name,)
    ).fetchone()
    if row is not None:
        return row[0] == principal.id and row[1]
    reserved = conn.execute(
        "SELECT principal_id FROM storage.consumer_identities WHERE name=%s", (name,)
    ).fetchone()
    # Unadopted legacy progress remains read-only under the original module policy.
    return reserved is None or reserved[0] == principal.id


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
        admin: bool = False,
    ) -> IssuedPrincipal:
        module_schema(module)
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            if not conn.execute(
                "SELECT 1 FROM storage.modules WHERE module=%s", (module,)
            ).fetchone():
                raise InvalidRequest("module not bootstrapped")
            # Managed plugins have one stable identity, including while revoked.
            # Core workers may have independent deployment identities. Keep genuine
            # pre-v6 fixtures operable without changing their historical definition.
            if conn.execute("SELECT to_regclass('storage.module_lifecycle')").fetchone() != (None,):
                managed = conn.execute(
                    "SELECT 1 FROM storage.module_lifecycle WHERE module=%s", (module,)
                ).fetchone()
                if (
                    managed
                    and conn.execute(
                        "SELECT 1 FROM storage.principals WHERE module=%s", (module,)
                    ).fetchone()
                ):
                    raise InvalidRequest("managed principal already exists; rotate it")
            token, principal_id = secrets.token_urlsafe(32), new_id()
            conn.execute(
                "INSERT INTO storage.principals (id,module,token_hash) VALUES (%s,%s,%s)",
                (principal_id, module, token_digest(token)),
            )
            if admin:
                if module != "api":
                    raise InvalidRequest("admin requires api owner")
                conn.execute(
                    "UPDATE storage.principals SET admin=true WHERE id=%s", (principal_id,)
                )
            _replace_grants(conn, principal_id, read_types, subscribe_types)
        # The only plaintext delivery. No retrieval endpoint and no plaintext DB column.
        return IssuedPrincipal(principal_id, token)

    def reserve_consumer_identity(self, name: str, principal_id: str) -> None:
        """Deployment-only binding for authenticated markers without a broker dependency."""
        self._reserve_consumer(name, principal_id, adopt=False)

    def adopt_consumer_markers(self, name: str, principal_id: str) -> None:
        """Explicitly attest provenance of legacy markers; never exposed over HTTP."""
        self._reserve_consumer(name, principal_id, adopt=True)

    def _reserve_consumer(self, name: str, principal_id: str, *, adopt: bool) -> None:
        try:
            ConsumerRequest(consumer=name)
        except ValueError:
            raise InvalidRequest("invalid consumer name") from None
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            owner = conn.execute(
                "SELECT module FROM storage.principals WHERE id=%s AND active", (principal_id,)
            ).fetchone()
            if owner is None or not (name == owner[0] or name.startswith(owner[0] + ".")):
                raise InvalidRequest("invalid consumer owner")
            registered = conn.execute(
                "SELECT principal_id FROM storage.consumers WHERE name=%s", (name,)
            ).fetchone()
            reserved = conn.execute(
                "SELECT principal_id FROM storage.consumer_identities WHERE name=%s", (name,)
            ).fetchone()
            if any(row is not None and row[0] != principal_id for row in (registered, reserved)):
                raise InvalidRequest("consumer already owned")
            if (
                not adopt
                and reserved is None
                and registered is None
                and conn.execute(
                    "SELECT 1 FROM storage.processed WHERE consumer=%s LIMIT 1", (name,)
                ).fetchone()
            ):
                raise InvalidRequest("legacy consumer requires deployment adoption")
            conn.execute(
                "INSERT INTO storage.consumer_identities(name,principal_id) VALUES (%s,%s) "
                "ON CONFLICT(name) DO NOTHING",
                (name, principal_id),
            )

    def set_admin(self, principal_id: str, *, admin: bool) -> None:
        """Deployment-only role revocation, serialized with submit and execution."""
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            row = conn.execute(
                "SELECT module FROM storage.principals WHERE id=%s", (principal_id,)
            ).fetchone()
            if row is None or (admin and row[0] != "api"):
                raise InvalidRequest("invalid admin owner")
            conn.execute(
                "UPDATE storage.principals SET admin=%s WHERE id=%s", (admin, principal_id)
            )

    def rotate(self, principal_id: str) -> IssuedPrincipal:
        """Deployment emergency rotation, including core workers; retain ID and grants.

        Never exposed via plugin feedback. Also fences any pending plugin claim.
        It cannot enable a module or restore withdrawn permissions.
        """
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            if not conn.execute(
                "SELECT 1 FROM storage.principals WHERE id=%s", (principal_id,)
            ).fetchone():
                raise InvalidRequest("invalid principal")
            token = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE storage.principals SET token_hash=%s,active=true,"
                "credential_epoch=credential_epoch+1 WHERE id=%s",
                (token_digest(token), principal_id),
            )
        return IssuedPrincipal(principal_id, token)

    def revoke(self, principal_id: str) -> None:
        with connection(self._dsn) as conn, writer(conn):
            check_schema(conn)
            conn.execute(
                "UPDATE storage.principals SET active=false,token_hash=NULL,"
                "credential_epoch=credential_epoch+1 WHERE id=%s",
                (principal_id,),
            )

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
