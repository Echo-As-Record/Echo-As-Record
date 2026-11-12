"""Explicit, bounded mvp-core initialization, never an HTTP capability or upgrade.

Operator order: migrate an empty database, bootstrap-core, independently run the
existing init-identities for management FIRST, then init-core-identities --output an
absolute NEW private directory/identities.json. No service is started here.
The extraction storage credential is also its gateway caller credential; the
llm_gateway writer is separate. API-user *storage* credentials are not public
API bearer tokens. Management credentials are neither issued nor replaced here.
If management is needed, initialize it FIRST: legacy init-identities deliberately
refuses issuance once an api user already exists. This tool does not bypass it.

AuthOperator commits independently. intent.json is fsynced BEFORE issuance;
per-module JSON receipts retain returned tokens before consumer reservation.
The final output is published exclusively only after DB verification. Any
interruption without that final output requires manual recovery, even if every
DB operation actually committed. An issue commit whose response was lost may
have no recoverable token: retain intent/receipts/DB together; never auto-reissue.
A final output is not authority: every retry verifies live DB identities, exact
stored/effective grants, epochs and consumer ownership. No repair, chmod of old
files, overwrite, unlink, migration, grant expansion or broker creation.

Local POSIX files only: no-follow descriptor-relative traversal, private leaf,
owner/mode/link checks, fsync and cooperative directory/DB locks. This does not
resist malicious root/same-UID code, mount replacement, all filesystem races or
noncooperating deployment operators; keep workers/admin operations stopped while
initializing. Reservations are NOT ready JetStream consumers or online workers.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import psycopg
from ear_contracts import DEFAULT_REGISTRY, Registry, is_id, new_id
from ear_storage_client.engine import canonical_json
from psycopg import sql

from ear_storage.auth import AuthOperator, Principal, authenticate, token_digest
from ear_storage.consumers import approved_filters
from ear_storage.repository import registry_from_db
from ear_storage.scheduler_access import PROFILE_TYPES, scheduler_state, strict_profile
from ear_storage.schema import check_schema, connection, create_module, module_schema, writer

PROFILE = "mvp-core-v1"
_INIT_LOCK = 0x454152434F5245
_MAX_JSON = 65536
_ERROR = "core initialization refused; preserve all recovery materials; operator recovery required"
RAW_TYPES = frozenset(
    {
        "audio_ref",
        "transcript_segment",
        "local_speaker",
        "voiceprint",
        "ambient_sound",
        "gps_point",
        "phone_info",
    }
)


class CoreDeploymentError(ValueError):
    """Sanitized failure; never carries credentials, DSN or filesystem contents."""


def core_mvp_registry() -> Registry:
    """Fresh trusted profile; leaves the public DEFAULT_REGISTRY untouched."""
    types = dict(DEFAULT_REGISTRY.data_types)
    for name in ("attribution", "artifact"):
        types[name] = replace(types[name], requires_dependencies=True, supports_reevaluation=True)
    registry = Registry(types, dict(DEFAULT_REGISTRY.event_publishers), DEFAULT_REGISTRY.core_types)
    registry = registry.extend_core(*PROFILE_TYPES)
    if strict_profile(registry) is None:
        raise CoreDeploymentError(_ERROR)
    return registry


@dataclass(frozen=True)
class IdentityPolicy:
    module: str
    reads: frozenset[str]
    subscriptions: frozenset[str] = frozenset()
    consumers: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "admin": False,
            "read_types": sorted(self.reads),
            "subscribe_types": sorted(self.subscriptions),
            "consumers": list(self.consumers),
        }


def core_identity_policies() -> dict[str, IdentityPolicy]:
    """Fixed deployment-approved scopes, NOT computed from a live plugin registry."""
    registry = core_mvp_registry()
    business = frozenset(
        n
        for n, s in registry.data_types.items()
        if s.emits_events and n != "plugin_management_feedback"
    )
    completions = frozenset(n for n, s in registry.data_types.items() if s.reevaluation_result)
    return {
        "adapter": IdentityPolicy("adapter", RAW_TYPES),
        "attribution": IdentityPolicy(
            "attribution",
            frozenset(
                {
                    "voiceprint",
                    "gps_point",
                    "person",
                    "place",
                    "attribution",
                    "feedback",
                    "attribution_state",
                    "attribution_feedback_result",
                    "attribution_completion",
                }
            ),
            frozenset({"voiceprint", "gps_point", "feedback", "attribution"}),
            ("attribution",),
        ),
        "extraction": IdentityPolicy(
            "extraction",
            RAW_TYPES
            | frozenset(
                {
                    "person",
                    "place",
                    "attribution",
                    "feedback",
                    "activity",
                    "rule",
                    "rule_selection",
                    "artifact",
                    "action_suggestion",
                    "extraction_run",
                    "extraction_completion",
                }
            ),
            frozenset({"feedback", "artifact", "rule_selection"}),
            ("extraction.jobs", "extraction.completion"),
        ),
        "dependency": IdentityPolicy(
            "dependency",
            business
            | completions
            | frozenset({"propagation_record", "attribution_feedback_result"}),
            business,
            ("dependency.changes",),
        ),
        "api_user": IdentityPolicy(
            "api", business | frozenset({"extraction_run", "attribution_feedback_result"})
        ),
        # Audit replay reads the saved call and all its target/cause types. It
        # does not require scheduler internals, completion bodies or management.
        "llm_gateway": IdentityPolicy("llm_gateway", business | frozenset({"llm_call"})),
    }


def _profile(conn: psycopg.Connection) -> str:
    check_schema(conn)
    expected = core_mvp_registry()
    actual = registry_from_db(conn)
    if actual.to_json() != expected.to_json() or actual.core_types != expected.core_types:
        raise CoreDeploymentError(_ERROR)
    modules = {s.writer for s in expected.data_types.values()}
    modules.update(m for ms in expected.event_publishers.values() for m in ms)
    if set(conn.execute("SELECT module,schema_name FROM storage.modules")) != {
        (m, module_schema(m)) for m in modules
    }:
        raise CoreDeploymentError(_ERROR)
    fingerprint = strict_profile(actual)
    if fingerprint is None:
        raise CoreDeploymentError(_ERROR)
    return fingerprint


@contextmanager
def _initialization(dsn: str) -> Iterator[psycopg.Connection]:
    with connection(dsn) as conn:
        # Session lock serializes this tool even across AuthOperator's separate
        # transactions. Never hold WRITER_GATE while calling AuthOperator.
        if conn.execute("SELECT pg_try_advisory_lock(%s)", (_INIT_LOCK,)).fetchone() != (True,):
            raise CoreDeploymentError(_ERROR)
        try:
            yield conn
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_INIT_LOCK,))


def bootstrap_core(dsn: str) -> bool:
    """Initialize an explicitly migrated EMPTY store, or verify an exact profile.

    Uses the existing module creation primitive in one writer transaction. Unlike
    generic bootstrap, never adds a partial bundle to an existing legacy registry.
    Schema DDL/migrations remain exclusively in the old explicit migrate command.
    """
    try:
        with _initialization(dsn) as conn, writer(conn):
            check_schema(conn)
            if conn.execute("SELECT 1 FROM storage.registry LIMIT 1").fetchone():
                _profile(conn)
                return False
            for name in (
                "modules",
                "publishers",
                "principals",
                "identity",
                "processed",
                "consumer_identities",
                "consumers",
                "outbox",
                "idempotency",
            ):
                if conn.execute(
                    sql.SQL("SELECT 1 FROM storage.{} LIMIT 1").format(sql.Identifier(name))
                ).fetchone():
                    raise CoreDeploymentError(_ERROR)
            if conn.execute("SELECT commit_seq FROM storage.meta").fetchall() != [(0,)]:
                raise CoreDeploymentError(_ERROR)
            registry = core_mvp_registry()
            modules = {s.writer for s in registry.data_types.values()}
            modules.update(m for ms in registry.event_publishers.values() for m in ms)
            for module in sorted(modules):
                create_module(conn, module)
            for name, spec in sorted(registry.data_types.items()):
                conn.execute(
                    "INSERT INTO storage.registry VALUES (%s,%s,%s)",
                    (name, canonical_json(spec.to_json()), registry.is_core(name)),
                )
                conn.execute("INSERT INTO storage.type_revisions(data_type) VALUES (%s)", (name,))
            for event, publishers in sorted(registry.event_publishers.items()):
                conn.execute(
                    "INSERT INTO storage.publishers VALUES (%s,%s)",
                    (event, canonical_json(publishers)),
                )
            _profile(conn)
        return True
    except Exception:
        raise CoreDeploymentError(_ERROR) from None


def _directory(info: os.stat_result, *, private: bool = False) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
        raise CoreDeploymentError(_ERROR)
    if private:
        if info.st_uid != os.getuid() or mode != 0o700:
            raise CoreDeploymentError(_ERROR)
    elif mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX):
        raise CoreDeploymentError(_ERROR)


def _regular(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > _MAX_JSON
    ):
        raise CoreDeploymentError(_ERROR)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CoreDeploymentError(_ERROR)
        result[key] = value
    return result


class _Bundle:
    """Pinned, rechecked ancestor chain. All writes are exclusive and retained."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.fds: list[int] = []
        self.names: list[str] = []
        self.created = False

    def __enter__(self) -> _Bundle:
        try:
            if (
                not self.output.is_absolute()
                or ".." in self.output.parts
                or self.output.suffix != ".json"
                or len(self.output.parts) < 4
                or self.output.name
                in {"intent.json", *(k + ".json" for k in core_identity_policies())}
            ):
                raise CoreDeploymentError(_ERROR)
            self.fds.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            _directory(os.fstat(self.fds[0]))
            parts = self.output.parent.parts[1:]
            for index, name in enumerate(parts):
                private = index == len(parts) - 1
                parent = self.fds[-1]
                if private:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                        self.created = True
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.fds.append(fd)
                self.names.append(name)
                _directory(os.fstat(fd), private=private)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def check(self) -> None:
        _directory(os.fstat(self.fds[0]))
        for i, name in enumerate(self.names):
            current = os.stat(name, dir_fd=self.fds[i], follow_symlinks=False)
            pinned = os.fstat(self.fds[i + 1])
            _directory(current, private=i == len(self.names) - 1)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise CoreDeploymentError(_ERROR)

    def read(self, name: str) -> dict[str, Any]:
        self.check()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            _regular(before)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_JSON + 1)
            after = os.fstat(fd)
            linked = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (
                len(raw) > _MAX_JSON
                or _signature(before) != _signature(after)
                or (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise CoreDeploymentError(_ERROR)
            value = json.loads(raw, object_pairs_hook=_pairs)
            if not isinstance(value, dict):
                raise CoreDeploymentError(_ERROR)
            self.check()
            return value
        finally:
            os.close(fd)

    def publish(self, name: str, value: dict[str, Any]) -> None:
        self.check()
        data = (canonical_json(value) + "\n").encode()
        if len(data) > _MAX_JSON:
            raise CoreDeploymentError(_ERROR)
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )
        try:
            os.fchmod(fd, 0o600)  # New file only; never repairs an existing object.
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.fsync(self.fd)
            self.check()
        finally:
            os.close(fd)
        if self.read(name) != value:
            raise CoreDeploymentError(_ERROR)

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __exit__(self, *args: Any) -> None:
        self.close()


def _plan(fingerprint: str, policies: dict[str, IdentityPolicy]) -> dict[str, Any]:
    return {
        "codec": 1,
        "profile": PROFILE,
        "profile_fingerprint": fingerprint,
        "identities": {role: p.document() for role, p in policies.items()},
        "gateway_caller": "extraction",
        "gateway_writer": "llm_gateway",
    }


def _scope(conn: psycopg.Connection, policies: dict[str, IdentityPolicy]) -> set[str]:
    """Separate existing API admins from this tool's six non-admin identities."""
    modules = sorted({p.module for p in policies.values()})
    return {
        row[0]
        for row in conn.execute(
            "SELECT id FROM storage.principals WHERE module=ANY(%s) "
            "AND NOT (module='api' AND admin)",
            (modules,),
        )
    }


def _preflight(conn: psycopg.Connection, policies: dict[str, IdentityPolicy]) -> str:
    fingerprint = _profile(conn)
    if _scope(conn, policies):
        raise CoreDeploymentError(_ERROR)
    names = [n for p in policies.values() for n in p.consumers]
    for table in ("consumer_identities", "consumers", "processed"):
        column = "consumer" if table == "processed" else "name"
        if conn.execute(
            sql.SQL("SELECT 1 FROM storage.{} WHERE {}=ANY(%s) LIMIT 1").format(
                sql.Identifier(table), sql.Identifier(column)
            ),
            (names,),
        ).fetchone():
            raise CoreDeploymentError(_ERROR)
    if conn.execute(
        "SELECT 1 FROM storage.module_lifecycle WHERE module=ANY(%s) LIMIT 1",
        (sorted({p.module for p in policies.values()}),),
    ).fetchone():
        raise CoreDeploymentError(_ERROR)
    return fingerprint


def _verify_consumers(
    conn: psycopg.Connection, principal: Principal, policy: IdentityPolicy
) -> None:
    # Reservations alone are not broker readiness. If a worker has since
    # registered a consumer, refuse stale/wrong-owner/expanded configurations.
    rows = conn.execute(
        "SELECT name,principal_id,module,enabled,ready,config_text FROM storage.consumers "
        "WHERE principal_id=%s OR name=ANY(%s)",
        (principal.id, list(policy.consumers)),
    ).fetchall()
    for name, pid, module, enabled, ready, text in rows:
        if (
            name not in policy.consumers
            or name == "extraction.completion"
            or pid != principal.id
            or module != policy.module
            or not enabled
            or not ready
        ):
            raise CoreDeploymentError(_ERROR)
        config = json.loads(text, object_pairs_hook=_pairs)
        kinds = (
            ["data_changed"]
            if module == "dependency"
            else ["data_changed", "reevaluation_requested"]
        )
        filters = config["filters"]
        if (
            config["event_types"] != kinds
            or config["from_beginning"] is not False
            or not isinstance(filters, list)
            or not filters
            or not all(isinstance(f, str) for f in filters)
        ):
            raise CoreDeploymentError(_ERROR)
        prefix = filters[0].rsplit(".", 3)[0]
        if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,7}", prefix) is None:
            raise CoreDeploymentError(_ERROR)
        expected = approved_filters(principal, core_mvp_registry(), kinds, prefix)
        if sorted(filters) != sorted(expected):
            raise CoreDeploymentError(_ERROR)


def _verify(
    conn: psycopg.Connection, policies: dict[str, IdentityPolicy], credentials: dict[str, Any]
) -> None:
    if set(credentials) != set(policies):
        raise CoreDeploymentError(_ERROR)
    ids = set()
    for role, policy in policies.items():
        entry = credentials[role]
        if not isinstance(entry, dict) or set(entry) != {"principal_id", "token"}:
            raise CoreDeploymentError(_ERROR)
        principal = authenticate(conn, token_digest(entry["token"]))
        ids.add(principal.id)
        stored = conn.execute(
            "SELECT module,admin,active,credential_epoch FROM storage.principals WHERE id=%s",
            (principal.id,),
        ).fetchone()
        grants = set(
            conn.execute(
                "SELECT data_type,can_subscribe FROM storage.type_grants WHERE principal_id=%s",
                (principal.id,),
            )
        )
        expected = {(n, n in policy.subscriptions) for n in policy.reads}
        if (
            principal.id != entry["principal_id"]
            or principal.module != policy.module
            or principal.admin
            or stored != (policy.module, False, True, 0)
            or principal.read_types != policy.reads
            or principal.subscribe_types != policy.subscriptions
            or grants != expected
        ):
            raise CoreDeploymentError(_ERROR)
        reserved = set(
            conn.execute(
                "SELECT name FROM storage.consumer_identities WHERE principal_id=%s",
                (principal.id,),
            )
        )
        if reserved != {(n,) for n in policy.consumers}:
            raise CoreDeploymentError(_ERROR)
        _verify_consumers(conn, principal, policy)
    if len(ids) != len(policies) or _scope(conn, policies) != ids:
        raise CoreDeploymentError(_ERROR)
    # Do not allow lifecycle filtering to conceal stored grants or disabled owners.
    if conn.execute(
        "SELECT 1 FROM storage.module_lifecycle WHERE module=ANY(%s) LIMIT 1",
        (sorted({p.module for p in policies.values()}),),
    ).fetchone():
        raise CoreDeploymentError(_ERROR)
    dependency = authenticate(conn, token_digest(credentials["dependency"]["token"]))
    state = scheduler_state(conn, dependency)
    if not all(state["owners"][m]["can_receive"] for m in ("attribution", "extraction")):
        raise CoreDeploymentError(_ERROR)


def init_core_identities(dsn: str, output: Path) -> bool:
    """Issue once into a NEW private directory, or read-only verify the same file.

    Returns a non-secret created flag only. Existing incomplete directories are
    never resumed, and a missing file with existing principals never reissues.
    AuthOperator's individual commits are deliberately not described as atomic
    with file publication. Back up the whole bundle, not just the final JSON.
    """
    try:
        policies = core_identity_policies()
        with _Bundle(output) as bundle, _initialization(dsn) as conn:
            with writer(conn):
                fingerprint = _profile(conn)
            plan = _plan(fingerprint, policies)
            if not bundle.created:
                expected_names = {output.name, "intent.json", *(k + ".json" for k in policies)}
                if set(os.listdir(bundle.fd)) != expected_names:
                    raise CoreDeploymentError(_ERROR)
                intent = bundle.read("intent.json")
                if (
                    set(intent) != {"operation_id", "plan"}
                    or not is_id(intent["operation_id"])
                    or canonical_json(intent["plan"]) != canonical_json(plan)
                ):
                    raise CoreDeploymentError(_ERROR)
                final = bundle.read(output.name)
                credentials = {role: bundle.read(role + ".json") for role in policies}
                if canonical_json(final) != canonical_json({**intent, "credentials": credentials}):
                    raise CoreDeploymentError(_ERROR)
                with writer(conn):
                    _profile(conn)
                    _verify(conn, policies, credentials)
                bundle.check()
                return False
            if os.listdir(bundle.fd):
                raise CoreDeploymentError(_ERROR)
            with writer(conn):
                if _preflight(conn, policies) != fingerprint:
                    raise CoreDeploymentError(_ERROR)
            intent = {"operation_id": new_id(), "plan": plan}
            bundle.publish("intent.json", intent)
            operator = AuthOperator(dsn)
            credentials: dict[str, Any] = {}
            for role, policy in policies.items():
                bundle.check()
                issued = operator.issue_principal(
                    policy.module, read_types=policy.reads, subscribe_types=policy.subscriptions
                )
                entry = {"principal_id": issued.principal_id, "token": issued.token}
                # Never remove this receipt if reservation or final verification fails.
                bundle.publish(role + ".json", entry)
                credentials[role] = entry
                for name in policy.consumers:
                    bundle.check()
                    operator.reserve_consumer_identity(name, issued.principal_id)
            with writer(conn):
                _profile(conn)
                _verify(conn, policies, credentials)
                bundle.publish(output.name, {**intent, "credentials": credentials})
            bundle.check()
            return True
    except Exception:
        raise CoreDeploymentError(_ERROR) from None
