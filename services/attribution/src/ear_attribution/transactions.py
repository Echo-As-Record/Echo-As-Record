"""Snapshot retries plus optional durable identical-request replay for remote writes.

The journal is single-process (OS lock), private, bound to one storage deployment
by an operator-supplied namespace. No credentials or raw embeddings are stored.
Pending operations can contain business names/IDs and require private storage.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from ear_contracts import new_id
from ear_storage_client import Context, Reader, StorageClient, WriteOp, WriteResult
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.wire import decode_request, encode_request

from ear_attribution.secure_files import LOCK_NAME, JournalFiles
from ear_attribution.state import STATE_ID, derived_id


class IdentifiedWriter(Protocol):
    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str
    ) -> WriteResult: ...


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

    def prepare(self, event_id: str, ops: Sequence[WriteOp], context: Context) -> tuple[str, str]:
        current = self.pending(event_id)
        if current is not None:
            return current
        item = (new_id(), encode_request(ops, context))
        with self._transaction():
            self.db.execute("INSERT INTO pending VALUES (?, ?, ?)", (event_id, *item))
        return item

    def complete(self, event_id: str) -> None:
        with self._transaction():
            self.db.execute("DELETE FROM pending WHERE event_id=?", (event_id,))


class Transactions:
    """Keep the original framework's read/plan/commit loop and bounded CAS retries."""

    def __init__(self, client: StorageClient, journal: CommitJournal | None = None) -> None:
        if isinstance(client, RemoteStorageClient) and journal is None:
            raise ValueError("RemoteStorageClient requires a durable CommitJournal")
        self.client, self.journal = client, journal
        self.lock = threading.RLock()

    def recover_pending(self, *, limit: int = 1000) -> int:
        """Resolve saved receipts before subscribing, independently of redelivery.

        A committed MarkProcessed suppresses the handler at the server/SDK, so
        broker recovery alone cannot drain this journal. Replay exact saved
        identities, never re-plan here. A definite rejection releases the intent
        for normal event redelivery/replanning; unknown/auth errors keep it.
        The returned count is resolved receipts, NOT successful business tasks.
        """
        with self.lock:
            if self.journal is None:
                return 0
            resolved = 0
            for event_id in self.journal.pending_ids(limit=limit):
                pending = self.journal.pending(event_id)
                assert pending is not None
                request_id, body = pending
                ops, context = decode_request(body)
                self.journal.validate()
                cast(IdentifiedWriter, self.client).write(ops, context, request_id=request_id)
                self.journal.complete(event_id)
                resolved += 1
            return resolved

    def run(
        self,
        plan: Callable[[Reader], Sequence[WriteOp]],
        context: Context,
        event_id: str,
        attempts: int,
    ) -> WriteResult:
        with self.lock:
            return self._run(plan, context, event_id, attempts)

    def _run(self, plan, context: Context, event_id: str, attempts: int) -> WriteResult:
        result = WriteResult(batch_id=None)
        for _ in range(attempts):
            ops: list[WriteOp] = []
            pending = self.journal.pending(event_id) if self.journal else None
            if pending is None:
                with self.client.snapshot() as reader:
                    ops = list(plan(reader))
                if not ops:
                    return WriteResult(batch_id=None)
                if self.journal:
                    pending = self.journal.prepare(event_id, ops, context)
            if self.journal:
                assert pending is not None
                request_id, body = pending
                replay_ops, replay_context = decode_request(body)
                self.journal.validate()
                result = cast(IdentifiedWriter, self.client).write(
                    replay_ops, replay_context, request_id=request_id
                )
                # Unknown outcome throws before this point: leave the identical intent intact.
                self.journal.complete(event_id)
            else:
                result = self.client.write(ops, context)
            conflict = result.conflict
            if conflict is None:
                return result
            # Only the known singleton creation race is re-plannable id_exists.
            # Re-read/re-plan, never delete the guard, suppress it or reuse an orphan.
            singleton_race = (
                conflict.reason == "id_exists"
                and conflict.data_type == "attribution_state"
                and conflict.id == STATE_ID
            )
            feedback_race = (
                conflict.reason == "id_exists"
                and conflict.data_type == "attribution_feedback_result"
                and context.depth == 0
                and context.root_feedback_id != "system"
                and conflict.id == derived_id("feedback_result:v1", context.root_feedback_id)
            )
            if not conflict.retryable and not singleton_race and not feedback_race:
                return result
        return result
