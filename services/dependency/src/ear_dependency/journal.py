"""Private transport-intent journal, not a business-state store.

Adapted from the reviewed hardened file/rollback primitives; deliberately no
attribution or gateway dependency. Use a separate persistent 0700 directory and
an explicit deployment/identity/consumer namespace. Retain it across restarts.
Unknown outcomes are never erased. No credentials are persisted here.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ear_contracts import new_id
from ear_storage_client import Context, WriteOp
from ear_storage_client.wire import encode_request

from ear_dependency.secure_files import LOCK_NAME, JournalFiles


class CommitJournal:
    """Persist before sending; retain unknown outcomes, including across restart.

    The caller must reuse this same directory/namespace after restart. No automatic
    deletion/replanning on transport or protocol exceptions. SQLite FULL commits
    are used; the deployment is responsible for durable filesystem semantics.
    """

    def __init__(self, directory: Path, *, namespace: str) -> None:
        if not namespace:
            raise ValueError("journal deployment namespace required")
        self._closed = False
        self._files = JournalFiles(directory)
        self._lock = self._files.fds[LOCK_NAME]
        try:
            self.db = sqlite3.connect(self._files.uri, uri=True)
            self.validate()
            self.db.execute("PRAGMA trusted_schema=OFF")
            if self.db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise ValueError("journal requires rollback DELETE; explicit recovery required")
            # Reading the existing database may legitimately recover a hot journal.
            self._files.validate(journal_removed=True)
            self.db.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                self.db.execute("CREATE TABLE IF NOT EXISTS identity (namespace TEXT NOT NULL)")
                identity = self.db.execute("SELECT namespace FROM identity").fetchall()
                if identity and identity != [(namespace,)]:
                    raise ValueError("journal deployment namespace mismatch")
                if not identity:
                    self.db.execute("INSERT INTO identity VALUES (?)", (namespace,))
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS pending "
                    "(event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, body TEXT NOT NULL)"
                )
            assert self._files.directory is not None
            os.fsync(self._files.directory)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if hasattr(self, "db"):
                self.db.close()
        finally:
            self._files.close()

    def validate(self) -> None:
        """Fail closed before local access and immediately before remote sends."""
        try:
            self._files.validate()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _checked(self) -> Iterator[None]:
        try:
            self.validate()
            yield
            self.validate()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._checked():
            self.db.execute("BEGIN IMMEDIATE")
            self._files.validate(journal_removed=True)
            yield
            self.validate()
            self.db.commit()
            self._files.validate(journal_removed=True)

    def pending(self, event_id: str) -> tuple[str, str] | None:
        with self._checked():
            return self.db.execute(
                "SELECT request_id, body FROM pending WHERE event_id=?", (event_id,)
            ).fetchone()

    def pending_ids(self, *, limit: int = 1000) -> list[str]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid journal recovery bound")
        with self._checked():
            rows = self.db.execute(
                "SELECT event_id FROM pending ORDER BY rowid LIMIT ?", (limit + 1,)
            ).fetchall()
            if len(rows) > limit:
                raise ValueError("journal recovery bound exceeded; explicit recovery required")
            return [row[0] for row in rows]

    def prepare(
        self,
        event_id: str,
        ops: Sequence[WriteOp],
        context: Context,
        *,
        request_id: str | None = None,
    ) -> tuple[str, str]:
        current = self.pending(event_id)
        if current is not None:
            return current
        item = (request_id or new_id(), encode_request(ops, context))
        with self._transaction():
            self.db.execute("INSERT INTO pending VALUES (?, ?, ?)", (event_id, *item))
        return item

    def complete(self, event_id: str) -> None:
        with self._transaction():
            self.db.execute("DELETE FROM pending WHERE event_id=?", (event_id,))
