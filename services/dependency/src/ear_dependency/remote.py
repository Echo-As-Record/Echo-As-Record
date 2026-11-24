"""Single-worker remote scheduler; explicit grants and durable identical replay.

Use one persistent journal directory per deployment/identity/consumer, shared by
all starts of that worker. Directory/DB/lock flock excludes local processes, NOT
workers on different hosts or different directories (no distributed lease).
Call subscribe() then alternate the client's poll_once() and tick(). Recovery
always precedes subscription AND each decision. No implicit historical scan.

TODO(button): implement the separately approved correction-feedback contract.
TODO(P5): wire explicit history assignments/catch-up; never auto-recover history.
TODO(deploy): provision identity, explicit grants and durable volume separately.
This is a remote scheduling slice, not full MVP or a production deployment.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from time import time
from typing import Any, Protocol, cast
from uuid import UUID

from ear_contracts import Event
from ear_storage_client import (
    CommitUnknown,
    ConflictError,
    Context,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Reader,
    StorageClient,
    WriteOp,
    WriteResult,
)
from ear_storage_client.remote_errors import AuthenticationError, SnapshotUnavailable
from ear_storage_client.wire import decode_request

from ear_dependency.authority import SchedulerBlocked, owner_reason, state
from ear_dependency.domain import Step
from ear_dependency.journal import CommitJournal
from ear_dependency.service import TERMINAL, DependencyService


class IdentifiedWriter(Protocol):
    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str
    ) -> WriteResult: ...


class RemoteDependencyService(DependencyService):
    """Remote authority is mandatory; full_visibility is deliberately not an option."""

    def __init__(
        self,
        client: StorageClient,
        *,
        journal: CommitJournal,
        consumer: str = "dependency.changes",
        max_nodes: int = 200,
        max_records: int = 5000,
        max_depth: int = 64,
        max_generations: int = 5,
        request_timeout: float = 600,
        clock: Callable[[], float] = time,
    ) -> None:
        journal.validate()
        self.journal = journal
        self._lock = threading.RLock()
        self._round: dict[str, Any] | None = None
        self._configure(
            client,
            consumer=consumer,
            max_nodes=max_nodes,
            max_records=max_records,
            max_depth=max_depth,
            max_generations=max_generations,
            request_timeout=request_timeout,
            clock=clock,
        )
        # Stable per consumer, non-secret identifier; valid UUIDv7 envelope.
        digest = hashlib.sha256(("dependency-runtime:" + consumer).encode()).hexdigest()
        self.status_id = str(UUID(digest[:12] + "7" + digest[13:16] + "8" + digest[17:32]))

    def _verify(self) -> dict[str, Any]:
        current = state(self.client)
        if self._round is not None and current != self._round:
            raise SchedulerBlocked("scheduler_state_changed")
        return current

    def _publish_guard(self, ops: Sequence[WriteOp]) -> None:
        if not any(isinstance(op, PublishReevaluation) for op in ops):
            return
        current = self._verify()
        steps = [
            Step.model_validate(op.payload)
            for op in ops
            if isinstance(op, PutSystemRecord) and op.payload.get("record_kind") == "step"
        ]
        if len(steps) != 1:
            raise SchedulerBlocked("publish_intent_invalid")
        step = steps[0]
        reason = owner_reason(current, step.owner)
        if reason:
            raise SchedulerBlocked(reason)
        if (
            current["coverage_revision"] != step.authority_revision
            or current["owners"][step.owner]["revision"] != step.owner_revision
        ):
            raise SchedulerBlocked("publish_authority_changed")
        spec = self.client.registry.data_types.get(step.target_type)
        if spec is None or spec.writer != step.owner or not spec.supports_reevaluation:
            raise SchedulerBlocked("owner_changed")
        if self._verify() != current:
            raise SchedulerBlocked("scheduler_state_changed")

    def _send(self, key: str) -> WriteResult:
        pending = self.journal.pending(key)
        assert pending is not None
        request_id, body = pending
        ops, context = decode_request(body)
        # Replaying a committed receipt is still subject to current server ACL.
        # If authority changed, keep evidence and block, not a new request ID.
        self._publish_guard(ops)
        self.journal.validate()  # after decoding/remote metadata, immediately before send
        try:
            result = cast(IdentifiedWriter, self.client).write(ops, context, request_id=request_id)
        except PermissionDenied:
            publishing = any(isinstance(op, PublishReevaluation) for op in ops)
            raise SchedulerBlocked("publish_denied" if publishing else "write_denied") from None
        self.journal.complete(key)  # only a definite response releases transport intent
        return result

    def recover_pending(self) -> int:
        """Exact original operations/context/ID, even if MarkProcessed suppresses delivery."""
        with self._lock:
            self.journal.validate()
            self._round = None
            keys = self.journal.pending_ids(limit=2)
            if not set(keys) <= {"runtime", "operation"}:
                raise SchedulerBlocked("journal_unknown_slot")
            for key in sorted(keys, reverse=True):  # diagnostic first, then original operation
                self._send(key).check()
            return len(keys)

    def _write_result(self, ops: list[WriteOp], context: Context) -> WriteResult:
        self._verify()
        self._publish_guard(ops)
        if self.journal.pending("operation") is not None:
            raise SchedulerBlocked("pending_recovery_required")
        intents = [
            op.payload.get("intent_id")
            for op in ops
            if isinstance(op, PutSystemRecord) and op.payload.get("state") == "requested"
        ]
        publishing = any(isinstance(op, PublishReevaluation) for op in ops)
        request_id = intents[0] if intents and publishing else None
        self.journal.prepare("operation", ops, context, request_id=request_id)
        return self._send("operation")

    def _status(self, reason: str | None) -> None:
        # Status is a systemRecord only; do not need graph authority to report denial.
        # A separate slot preserves an unresolved publication; never overwrite it.
        if self.journal.pending("runtime") is not None:
            self._send("runtime").check()
        current = self.client.get_system_record(self.status_id)
        payload = {
            "schema_tag": "dependency_runtime_v1",
            "consumer": self.consumer,
            "status": "blocked" if reason else "ready",
            "reason": reason,
            "pending_transport": self.journal.pending("operation") is not None,
        }
        if current is not None and current.payload == payload:
            return
        self.journal.prepare(
            "runtime",
            [
                PutSystemRecord(
                    "propagation_record",
                    self.status_id,
                    payload,
                    expected_revision=current.revision if current else None,
                )
            ],
            Context.system(),
        )
        self._send("runtime").check()

    def _execute(self, action: Callable[[], None]) -> None:
        with self._lock:
            try:
                self.recover_pending()
                self._round = state(self.client)
                action()
                self._verify()
                self._status(None)
            except CommitUnknown:
                self._status("commit_unknown")
                raise
            except (
                SchedulerBlocked,
                PermissionDenied,
                AuthenticationError,
                SnapshotUnavailable,
            ) as exc:
                reason = str(exc) if isinstance(exc, SchedulerBlocked) else "read_or_write_denied"
                # Do not ACK on denial. If the whole identity is revoked this write
                # can itself fail; surface that failure and keep journal evidence.
                self._status(reason)
                raise SchedulerBlocked(reason) from None
            finally:
                self._round = None

    def subscribe(self) -> None:
        self._execute(
            lambda: self.client.subscribe(self.consumer, self.handle, event_types=["data_changed"])
        )

    def _admission_reader(self) -> AbstractContextManager[Reader]:
        # Admission records the immutable event, not a graph decision. It reads
        # only current existence/type/version, the monotone processed marker,
        # immutable raw feedback and (for creation) immutable v1. A full database
        # snapshot per delivery adds unrelated graph/history cost and starves
        # the consumer. Every direct read still reauthenticates at storage;
        # _execute/_write_result still fence authority and atomically mark the
        # trigger. Expansion, causal checks and publication retain snapshots
        # and fresh authority checks, so this is not a cached/filtered graph.
        return nullcontext(self.client)

    @contextmanager
    def _records_reader(self) -> Iterator[Reader]:
        # Serialize public records() with this worker's writes, without hydrating
        # business history. Ordinary reads reauthenticate but may filter rows;
        # even outside _execute, require current strict authority before AND after
        # the bounded read. Never return an empty/partial list after revocation.
        # Keep the proof local: no cache or change to _execute's recovery/round.
        with self._lock:
            try:
                current = self._verify()
                yield self.client
                if self._verify() != current:
                    raise SchedulerBlocked("scheduler_state_changed")
            except SchedulerBlocked as exc:
                if str(exc) == "coverage_denied":
                    # Preserve the public read API's denial, including legacy.
                    raise PermissionDenied("access denied") from None
                raise

    def handle(self, event: Event) -> None:
        self._execute(lambda: super(RemoteDependencyService, self).handle(event))

    def tick(self) -> None:
        self._execute(super().tick)

    def _prepare(self, step: Step, upstreams: list[Step], direct_inputs: set[str]) -> None:
        super()._prepare(step, upstreams, direct_inputs)
        if step.state != "intent":
            return
        current = self._verify()
        reason = owner_reason(current, step.owner)
        if reason:
            step.state, step.reason = "blocked", reason
            return
        step.authority_revision = current["coverage_revision"]
        step.owner_revision = current["owners"][step.owner]["revision"]

    def _advance(self, step_id: str) -> None:
        current = self._verify()
        envelope = self.client.get_system_record(step_id)
        if envelope is None:
            raise SchedulerBlocked("step_missing")
        step = Step.model_validate(envelope.payload)
        revision = envelope.revision
        if step.state == "intent":
            # Crash after durable intent but before journal/send. Preserve both IDs.
            self._publish(step, revision)
            return
        if step.owner and step.state not in TERMINAL:
            reason = owner_reason(current, step.owner)
            finished = step.request_event_id and self.client.get_reevaluation_result(
                step.request_event_id
            )
            if reason and not finished:
                step.state, step.reason = "blocked", reason
                self._save(step, revision)
                return
        super()._advance(step_id)

    def _publish(self, step: Step, revision: int) -> None:
        current = self._verify()
        reason = owner_reason(current, step.owner)
        if not reason and (
            current["coverage_revision"] != step.authority_revision
            or current["owners"][step.owner]["revision"] != step.owner_revision
        ):
            reason = "publish_authority_changed"
        if reason:
            step.state, step.reason = "blocked", reason
            self._save(step, revision)
            return
        try:
            super()._publish(step, revision)
        except ConflictError as exc:
            # Definite rejection is not an unknown outcome. Keep the failed
            # intent visible, never retry a changed body under its request ID.
            step.state, step.reason = "blocked", "publish_conflict:" + exc.conflict.reason
            self._save(step, revision)
