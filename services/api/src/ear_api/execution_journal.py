"""Private ⑥ recovery facts, NOT authoritative ④ business states.

Linux/local filesystem only. Keep this journal and its protected directory across
restarts; never reset/delete it to recover an uncertain external call. One owner
holds an OS lock for its lifetime. No leases grant permission to repeat actions.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Literal

from ear_contracts.base import Id
from pydantic import BaseModel, ConfigDict, Field


class JournalUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("execution_journal_unavailable")


class ExecutionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    suggestion_id: Id
    approved_version: int = Field(strict=True, ge=1)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_json: str = Field(max_length=16384)
    action_key: str = Field(pattern=r"^ear_action_[0-9a-f]{64}$")
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    feedback_id: Id
    receipt: str = Field(pattern=r"^ear-api-action:[0-9a-f]{64}$")
    state: Literal["prepared", "executing", "unknown", "succeeded", "failed"] = "prepared"
    delivery: Literal["none", "pending", "unknown", "accepted", "applied", "blocked"] = "none"
    external_ref: str | None = Field(default=None, max_length=512)
    error_code: str | None = Field(default=None, pattern=r"^[a-z_]{1,80}$")
    revision: int = Field(default=1, strict=True, ge=1)


class ExecutionJournal:
    """Explicit path; no automatic directory creation, schema migration or reset."""

    def __init__(self, path: Path) -> None:
        self._fd: int | None = None
        self._db: sqlite3.Connection | None = None
        self.lock = threading.RLock()
        self._pid = os.getpid()
        try:
            if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
                raise ValueError
            parent = path.parent.stat()
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ValueError
            self._fd = os.open(
                path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600
            )
            info = os.fstat(self._fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._db = sqlite3.connect(path, timeout=1, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=EXTRA")
            self._db.execute("PRAGMA trusted_schema=OFF")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1} or (version == 0 and info.st_size != 0):
                raise ValueError
            if version == 0:
                with self._db:
                    self._db.execute(
                        "CREATE TABLE execution "
                        "(suggestion_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
                    )
                    self._db.execute("PRAGMA user_version=1")
            if self._db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            self.close()
            raise JournalUnavailable() from None

    def _connection(self) -> sqlite3.Connection:
        if self._db is None or self._pid != os.getpid():
            raise JournalUnavailable()
        return self._db

    def get(self, suggestion_id: str) -> ExecutionEntry | None:
        with self.lock:
            try:
                row = (
                    self._connection()
                    .execute("SELECT body FROM execution WHERE suggestion_id=?", (suggestion_id,))
                    .fetchone()
                )
                return ExecutionEntry.model_validate_json(row[0]) if row else None
            except Exception:
                raise JournalUnavailable() from None

    def save(self, entry: ExecutionEntry, *, previous: ExecutionEntry | None = None) -> None:
        with self.lock:
            try:
                db = self._connection()
                with db:
                    if previous is None:
                        if entry.revision != 1:
                            raise ValueError
                        db.execute(
                            "INSERT INTO execution(suggestion_id, body) VALUES (?, ?)",
                            (entry.suggestion_id, entry.model_dump_json()),
                        )
                    else:
                        if (
                            entry.suggestion_id != previous.suggestion_id
                            or entry.revision != previous.revision + 1
                        ):
                            raise ValueError
                        changed = db.execute(
                            "UPDATE execution SET body=? WHERE suggestion_id=? AND body=?",
                            (
                                entry.model_dump_json(),
                                entry.suggestion_id,
                                previous.model_dump_json(),
                            ),
                        ).rowcount
                        if changed != 1:
                            raise ValueError
            except Exception:
                raise JournalUnavailable() from None

    def close(self) -> None:
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> ExecutionJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
