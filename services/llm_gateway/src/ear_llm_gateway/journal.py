"""Single-process durable safety fence; NOT provider exactly-once.

One service owns one durable journal (exclusive flock, uvicorn workers=1).
Retain it across restarts: losing/replacing it loses pre-send deduplication.
No automatic expiry, deletion, replay of model calls or cached reply bodies.
After a crash, active calls become outcome-unknown audit records, never re-sent.
A pending record is replayed to ① with the SAME storage request_id and payload.
No provider/API credentials or request bodies are stored unless body policy opts in.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from ear_contracts import LLMCallRecord, new_id

from ear_llm_gateway.models import GatewayError


class Journal:
    def __init__(self, path: str):
        self._fd: int | None = None
        self._directory: int | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._sidecar_identities: dict[str, tuple[int, int]] = {}
        self._sidecars_active = False
        self._pid = os.getpid()
        self.path = path
        try:
            target = Path(path)
            if not target.is_absolute() or ".." in target.parts:
                raise ValueError("unsafe_journal_path")
            # Traverse without following any symlinks; create private directories.
            directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self._directory = directory
            for part in target.parent.parts[1:]:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = self._directory = child
                info = os.fstat(directory)
                if info.st_uid not in {0, os.geteuid()} or (
                    info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
                ):
                    raise ValueError("unsafe_journal_directory")
            parent = os.fstat(directory)
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ValueError("unsafe_journal_directory")
            self._parent_identity = (parent.st_dev, parent.st_ino)
            self._fd = os.open(
                target.name,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            info = os.fstat(self._fd)
            self._private_file(info)
            self._identity = (info.st_dev, info.st_ino)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("journal_already_owned") from None
            self._validate_files()
            # Keep the directory and actual DB inode pinned for the owner's lifetime.
            self._conn = sqlite3.connect(
                f"/proc/self/fd/{directory}/{target.name}", check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._validate_files()
            self._conn.execute("PRAGMA trusted_schema=OFF")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, module TEXT NOT NULL, phase TEXT NOT NULL,
                    request_id TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS buckets (name TEXT PRIMARY KEY, at REAL, count INTEGER);
            """)
            # SQLite has now opened/created both active WAL sidecars. Bind once
            # for this connection lifetime; reopening legitimately binds new files.
            self._sidecars_active = True
            self._validate_files(bind_sidecars=True)
            with self._transaction() as conn:
                for row in conn.execute("SELECT * FROM calls WHERE phase='active'").fetchall():
                    record = LLMCallRecord.model_validate_json(row["record"])
                    end = max(datetime.now(UTC), record.started_at)
                    record = record.model_copy(
                        update={
                            "ended_at": end,
                            "latency_ms": None,
                            "error_code": "provider_outcome_unknown",
                            "status": "failed",
                        }
                    )
                    changed = conn.execute(
                        "UPDATE calls SET phase='pending',record=? WHERE id=? AND phase='active'",
                        (record.model_dump_json(), row["id"]),
                    ).rowcount
                    self._changed(changed)
            os.fsync(directory)
        except BaseException as exc:
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ValueError) and str(exc) == "journal_already_owned":
                raise
            raise ValueError("journal_unavailable") from None

    @staticmethod
    def _private_file(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("unsafe_journal_file")

    def _validate_files(self, *, bind_sidecars=False):
        if self._pid != os.getpid() or self._fd is None or self._directory is None:
            raise GatewayError(503, "journal_unavailable")
        target = Path(self.path)
        if target.resolve() != target:
            raise ValueError("unsafe_journal_path")
        parent = target.parent.stat()
        if (
            (parent.st_dev, parent.st_ino) != self._parent_identity
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("unsafe_journal_directory")
        info = os.fstat(self._fd)
        self._private_file(info)
        current = os.stat(target.name, dir_fd=self._directory, follow_symlinks=False)
        self._private_file(current)
        if (current.st_dev, current.st_ino) != self._identity:
            raise ValueError("journal_identity_changed")
        # Validate before SQLite opens/replays a pre-existing WAL or rollback journal.
        # Never repair permissions, unlink sidecars, or discard recovery evidence.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                fd = os.open(
                    target.name + suffix,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=self._directory,
                )
            except FileNotFoundError:
                if self._sidecars_active and suffix in ("-wal", "-shm"):
                    raise ValueError("journal_sidecar_missing") from None
                continue
            try:
                info = os.fstat(fd)
                self._private_file(info)
                current = os.stat(
                    target.name + suffix, dir_fd=self._directory, follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("journal_sidecar_identity_changed")
                if self._sidecars_active and suffix in ("-wal", "-shm"):
                    identity = (info.st_dev, info.st_ino)
                    if bind_sidecars:
                        self._sidecar_identities[suffix] = identity
                    elif self._sidecar_identities.get(suffix) != identity:
                        raise ValueError("journal_sidecar_identity_changed")
            finally:
                os.close(fd)

    @staticmethod
    def _changed(count):
        if count != 1:
            raise GatewayError(503, "journal_state_conflict")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._validate_files()
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._validate_files()
                self._conn.commit()
                self._validate_files()
            except BaseException:
                self._conn.rollback()
                raise

    def reserve(self, call_id: str, record: LLMCallRecord, *, global_rpm: int, caller_rpm: int):
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone():
                raise GatewayError(409, "duplicate_call_id")
            now = time.time()
            for name, limit in (
                ("global", global_rpm),
                ("caller:" + record.caller_module, caller_rpm),
            ):
                row = conn.execute("SELECT at,count FROM buckets WHERE name=?", (name,)).fetchone()
                # Backwards wall-clock jumps do not reset the quota.
                at, count = (now, 0) if row is None or now - row[0] >= 60 else tuple(row)
                if count >= limit:
                    raise GatewayError(429, "rate_limited")
                conn.execute("INSERT OR REPLACE INTO buckets VALUES (?,?,?)", (name, at, count + 1))
            conn.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?)",
                (
                    call_id,
                    record.caller_module,
                    "active",
                    new_id(),
                    record.model_dump_json(),
                ),
            )

    def pending(self, call_id: str, record: LLMCallRecord):
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE calls SET phase='pending',record=? WHERE id=? AND phase='active'",
                (record.model_dump_json(), call_id),
            ).rowcount
            self._changed(changed)

    def get(self, call_id: str, module: str):
        with self._lock:
            self._validate_files()
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM calls WHERE id=? AND module=?", (call_id, module)
            ).fetchone()
            return dict(row) if row else None

    def pending_ids(self, *, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_recovery_limit")
        with self._lock:
            self._validate_files()
            assert self._conn is not None
            return [
                tuple(row)
                for row in self._conn.execute(
                    "SELECT id,module FROM calls WHERE phase='pending' ORDER BY id LIMIT ?",
                    (limit,),
                )
            ]

    def saved(self, call_id: str):
        with self._transaction() as conn:
            self._changed(
                conn.execute(
                    "UPDATE calls SET phase='saved' WHERE id=? AND phase='pending'", (call_id,)
                ).rowcount
            )

    def quarantine(self, call_id: str):
        """Permanent ID collision: retain the SAME request ID and complete audit forever.

        Not saved, not automatically retried, never authorizes another provider call.
        Resolution requires operator investigation; no automatic rekey/delete is safe.
        """
        with self._transaction() as conn:
            self._changed(
                conn.execute(
                    "UPDATE calls SET phase='quarantined' WHERE id=? AND phase='pending'",
                    (call_id,),
                ).rowcount
            )

    def quarantined_count(self):
        with self._lock:
            self._validate_files()
            assert self._conn is not None
            return self._conn.execute(
                "SELECT count(*) FROM calls WHERE phase='quarantined'"
            ).fetchone()[0]

    def close(self):
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            finally:
                self._conn = None
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                if self._directory is not None:
                    os.close(self._directory)
                    self._directory = None
