"""Trusted deployment identity bootstrap; never exposed via HTTP.

Only deploy/.env or the dedicated container secret file is allowed. Tests use
synthetic deploy/.env directories. A crash between durable file publication and
DB commit fails closed on retry: retain the file for explicit operator recovery,
never silently overwrite/rotate it. Back up identities together with the database.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
from contextlib import suppress
from pathlib import Path

from ear_contracts import new_id

from ear_storage.auth import _replace_grants, authenticate, token_digest
from ear_storage.schema import check_schema, connection, writer

ADMIN = "EAR_ADMIN_TOKEN"
WORKER = "EAR_MANAGEMENT_WORKER_TOKEN"
FEEDBACK = "plugin_management_feedback"
RESULT = "plugin_management_result"


def read_identity_tokens(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(
            r"\s*(?:export\s+)?(EAR_ADMIN_TOKEN|EAR_MANAGEMENT_WORKER_TOKEN)\s*=", line
        )
        if match:
            key = match[1]
            value = line[match.end() :].strip()
            if key in found:
                raise ValueError("duplicate identity field")
            token_digest(value)
            found[key] = value
    if found and set(found) != {ADMIN, WORKER}:
        raise ValueError("incomplete identity file; explicit recovery required")
    return found


def _open_directory(parent: Path) -> int:
    """Open all ancestors without following symlinks; caller owns the final fd."""
    if ".." in parent.parts:
        raise ValueError("invalid output path")
    absolute = parent.absolute()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def _read_output(directory: int, name: str) -> tuple[str, os.stat_result | None]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return "", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("existing output must be a regular 0600 file")
        with os.fdopen(fd, "r", closefd=False) as stream:
            return stream.read(), info
    finally:
        os.close(fd)


def _publish(directory: int, name: str, old: str, info: os.stat_result | None, text: str) -> None:
    temporary = ".env." + secrets.token_hex(16)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        # Per-fd permissions do not change other threads' process-global umask.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", closefd=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(fd)
        if info is None:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        else:
            current, now = _read_output(directory, name)
            if (
                now is None
                or current != old
                or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise ValueError("output changed during initialization")
            # Cooperative writers share the directory lock. Rename does not
            # dereference the destination; ancestor swaps cannot redirect it.
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def init_identities(dsn: str, output: Path) -> bool:
    if not (output.name == ".env" and output.parent.name == "deploy") and output != Path(
        "/run/secrets/ear/identities.env"
    ):
        raise ValueError("output must be deploy/.env or the container deployment secret path")
    directory = None
    try:
        directory = _open_directory(output.parent)
        fcntl.flock(directory, fcntl.LOCK_EX)
        old, info = _read_output(directory, output.name)
        tokens = read_identity_tokens(old)
        requirements = (
            (ADMIN, "api", True, frozenset((FEEDBACK, RESULT)), frozenset()),
            (WORKER, "storage", False, frozenset((FEEDBACK,)), frozenset((FEEDBACK,))),
        )
        with connection(dsn) as conn, writer(conn):
            check_schema(conn)
            if tokens:
                for key, module, admin, reads, subscriptions in requirements:
                    principal = authenticate(conn, token_digest(tokens[key]))
                    # Effective permissions alone could hide extra stored grants.
                    grants = conn.execute(
                        "SELECT data_type,can_subscribe FROM storage.type_grants "
                        "WHERE principal_id=%s",
                        (principal.id,),
                    ).fetchall()
                    if (
                        principal.module != module
                        or principal.admin != admin
                        or principal.read_types != reads
                        or principal.subscribe_types != subscriptions
                        or frozenset(row[0] for row in grants) != reads
                        or frozenset(row[0] for row in grants if row[1]) != subscriptions
                    ):
                        raise ValueError("identity policy mismatch")
                return False
            if conn.execute(
                "SELECT 1 FROM storage.principals WHERE module IN ('api','storage') LIMIT 1"
            ).fetchone():
                raise ValueError("identities already exist; refusing automatic reissue")
            for key, module, admin, reads, subscriptions in requirements:
                token, pid = secrets.token_urlsafe(32), new_id()
                conn.execute(
                    "INSERT INTO storage.principals(id,module,token_hash,admin) "
                    "VALUES (%s,%s,%s,%s)",
                    (pid, module, token_digest(token), admin),
                )
                _replace_grants(conn, pid, reads, subscriptions)
                tokens[key] = token
            text = old + ("\n" if old and not old.endswith("\n") else "")
            text += "".join(f"{k}={v}\n" for k, v in tokens.items())
            _publish(directory, output.name, old, info, text)
        return True
    except Exception:
        # File publication and DB commit are not a distributed transaction.
        # Keep a published file on uncertain commit; a missing file with existing
        # principals also requires operator recovery, never automatic rotation.
        raise ValueError(
            "invalid identity configuration; operator recovery required; refusing automatic reissue"
        ) from None
    finally:
        if directory is not None:
            os.close(directory)
