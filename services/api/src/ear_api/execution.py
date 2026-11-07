"""Offline execution/recovery slice using the existing ④ terminal feedback protocol.

NOT wired to a production worker or a real calendar. ④ cannot yet represent
executing/unknown/user reconciliation: HTTP execution and risk retry stay closed.
HTTP verification is lookup-only, with current authorization and a journal revision.
Only trusted, typed, explicitly injected adapters can establish an external result;
clients cannot choose actions, URLs, outcomes, receipts or adapter implementations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Literal, Protocol

from ear_contracts import (
    DataChanged,
    Event,
    NewRawRecord,
    Record,
    SemanticRecord,
    Source,
    is_id,
    new_id,
)
from ear_extraction.domain import Action, Command
from ear_storage_client import AppendRaw, Context, WriteOp, WriteResult
from pydantic import BaseModel, ConfigDict, Field

from ear_api.business import feedback_identity
from ear_api.execution_journal import ExecutionEntry, ExecutionJournal
from ear_api.models import bound_json
from ear_api.upstream import GatewayError

PROTOCOL_BLOCKER = "extraction_execution_recovery_protocol_pending"


class ExternalResult(BaseModel):
    """Adapter evidence, never accepted from HTTP. not_executed must be conclusive.

    A missing search hit/timeout is UNKNOWN, not proof of non-execution. An adapter
    must use durable external idempotency and authoritative lookup for this key.
    No reference is ever interpreted as a URL or used as an executable callback.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    action_key: str = Field(pattern=r"^ear_action_[0-9a-f]{64}$")
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["succeeded", "not_executed", "unknown"]
    external_ref: str | None = Field(default=None, min_length=1, max_length=512)


class ExecutionStorage(Protocol):
    @property
    def module(self) -> str: ...

    def get(self, id: str) -> Record | None: ...

    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str
    ) -> WriteResult: ...


class ActionAdapter(Protocol):
    name: str
    reliable_idempotency: bool

    def lookup(self, action_key: str, action_hash: str) -> ExternalResult: ...

    def execute(self, action_key: str, action_hash: str, action: Action) -> ExternalResult: ...


def approved_action(record: SemanticRecord) -> tuple[str, str]:
    raw = record.payload.get("action")
    bound_json(raw, authority=False)
    if not isinstance(raw, dict):
        raise GatewayError(409, "invalid_suggestion_action")
    for name in ("start", "end", "due_at"):
        if raw.get(name) is not None and not isinstance(raw[name], str):
            raise GatewayError(409, "invalid_suggestion_action")
    action = Action.model_validate(raw)
    if len(action.title) > 512 or len(action.notes) > 8192 or not action.title.strip():
        raise GatewayError(409, "invalid_suggestion_action")
    text = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


class ExecutionService:
    """One locked journal; synthetic/offline callers only until ④ collaboration lands.

    Duplicate events and feedback-delivery retries never imply another execute().
    Recovery of executing/unknown only performs lookup. Even conclusive absence on
    recovery does not automatically repeat: user-risk/retry protocol is still pending.
    """

    def __init__(
        self, client: ExecutionStorage, journal: ExecutionJournal, adapter: ActionAdapter
    ) -> None:
        if (
            client.module != "api"
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", adapter.name) is None
            or adapter.reliable_idempotency is not True
        ):
            raise ValueError("trusted_safe_execution_configuration_required")
        self.client, self.journal, self.adapter = client, journal, adapter

    def _save(self, entry: ExecutionEntry, **changes: object) -> ExecutionEntry:
        data = {**entry.model_dump(), **changes, "revision": entry.revision + 1}
        updated = ExecutionEntry.model_validate(data)
        self.journal.save(updated, previous=entry)
        return updated

    def handle_event(self, event: Event) -> None:
        if isinstance(event, DataChanged) and event.data_type == "action_suggestion":
            # Event versions/payload are never a substitute for reading current state.
            self.process(event.data_id)

    def _current(self, suggestion_id: str) -> SemanticRecord:
        if not is_id(suggestion_id):
            raise GatewayError(422, "invalid_input")
        record = self.client.get(suggestion_id)
        if not isinstance(record, SemanticRecord) or record.data_type != "action_suggestion":
            raise GatewayError(404, "not_found")
        return record

    def _evidence(self, entry: ExecutionEntry, *, execute: bool = False) -> ExternalResult:
        try:
            value = (
                self.adapter.execute(
                    entry.action_key,
                    entry.action_hash,
                    Action.model_validate_json(entry.action_json),
                )
                if execute
                else self.adapter.lookup(entry.action_key, entry.action_hash)
            )
            if (
                not isinstance(value, ExternalResult)
                or value.action_key != entry.action_key
                or value.action_hash != entry.action_hash
                or (value.outcome == "succeeded" and not value.external_ref)
            ):
                raise ValueError
            return value
        except Exception:
            # Timeout/ordinary exceptions/malformed evidence do not prove failure.
            return ExternalResult(
                action_key=entry.action_key, action_hash=entry.action_hash, outcome="unknown"
            )

    def process(self, suggestion_id: str) -> ExecutionEntry | None:
        with self.journal.lock:
            current = self._current(suggestion_id)
            entry = self.journal.get(suggestion_id)
            if entry is None:
                if current.status != "active" or current.payload.get("state") != "approved":
                    return None
                action_json, action_hash = approved_action(current)
                digest = hashlib.sha256(
                    f"ear-api-execution-v1:{suggestion_id}:{current.version}:{action_hash}".encode()
                ).hexdigest()
                entry = ExecutionEntry(
                    suggestion_id=suggestion_id,
                    approved_version=current.version,
                    action_json=action_json,
                    action_hash=action_hash,
                    action_key="ear_action_" + digest,
                    adapter=self.adapter.name,
                    feedback_id=new_id(),
                    receipt="ear-api-action:" + digest,
                )
                self.journal.save(entry)
            if entry.adapter != self.adapter.name:
                raise GatewayError(409, "execution_adapter_changed")
            _, digest = approved_action(current)
            if digest != entry.action_hash:
                return self._save(entry, delivery="blocked", error_code="approved_action_changed")
            if entry.state in {"succeeded", "failed"}:
                return self._deliver(entry, current)
            if (
                current.status != "active"
                or current.version != entry.approved_version
                or current.payload.get("state") != "approved"
            ):
                return self._save(entry, delivery="blocked", error_code="suggestion_changed")
            recovering = entry.state in {"executing", "unknown"}
            evidence = self._evidence(entry)
            if evidence.outcome == "succeeded":
                entry = self._save(
                    entry,
                    state="succeeded",
                    external_ref=evidence.external_ref,
                    delivery="pending",
                    error_code=None,
                )
            elif evidence.outcome == "unknown" or recovering:
                return self._save(
                    entry,
                    state="unknown",
                    error_code="external_result_unknown",
                )
            else:
                # Durable intent BEFORE the call. Process death (BaseException) leaves
                # executing for lookup-only recovery, never a lease-based re-execution.
                latest = self._current(suggestion_id)
                _, latest_hash = approved_action(latest)
                if (
                    latest.status != "active"
                    or latest.version != entry.approved_version
                    or latest.payload.get("state") != "approved"
                    or latest_hash != entry.action_hash
                ):
                    return self._save(entry, delivery="blocked", error_code="suggestion_changed")
                entry = self._save(entry, state="executing", error_code=None)
                evidence = self._evidence(entry, execute=True)
                if evidence.outcome == "unknown":
                    return self._save(entry, state="unknown", error_code="external_result_unknown")
                entry = self._save(
                    entry,
                    state="succeeded" if evidence.outcome == "succeeded" else "failed",
                    external_ref=evidence.external_ref,
                    delivery="pending",
                )
            return self._deliver(entry, self._current(suggestion_id))

    def verify(self, suggestion_id: str, expected_revision: int) -> ExecutionEntry:
        """Reconcile existing intent only. This method can NEVER call execute()."""
        with self.journal.lock:
            current = self._current(suggestion_id)
            entry = self.journal.get(suggestion_id)
            if entry is None:
                raise GatewayError(409, "execution_not_started")
            if entry.revision != expected_revision:
                raise GatewayError(409, "execution_revision_conflict")
            if entry.adapter != self.adapter.name:
                raise GatewayError(409, "execution_adapter_changed")
            _, digest = approved_action(current)
            if digest != entry.action_hash:
                return self._save(entry, delivery="blocked", error_code="approved_action_changed")
            if entry.state in {"succeeded", "failed"}:
                return self._deliver(entry, current)
            if (
                current.status != "active"
                or current.version != entry.approved_version
                or current.payload.get("state") != "approved"
            ):
                return self._save(entry, delivery="blocked", error_code="suggestion_changed")
            evidence = self._evidence(entry)
            if evidence.outcome != "succeeded":
                # Even authoritative absence is NOT permission for risk retry.
                return self._save(entry, state="unknown", error_code="external_result_unknown")
            entry = self._save(
                entry,
                state="succeeded",
                external_ref=evidence.external_ref,
                delivery="pending",
                error_code=None,
            )
            return self._deliver(entry, self._current(suggestion_id))

    def _deliver(self, entry: ExecutionEntry, current: SemanticRecord) -> ExecutionEntry:
        if entry.state not in {"succeeded", "failed"}:
            raise GatewayError(409, "external_result_unconfirmed")
        outcome = "executed" if entry.state == "succeeded" else "failed"
        if (
            current.status == "active"
            and current.payload.get("state") == outcome
            and current.payload.get("receipt") == entry.receipt
        ):
            if entry.delivery == "applied":
                return entry
            return self._save(entry, delivery="applied", error_code=None)
        if (
            current.status != "active"
            or current.version != entry.approved_version
            or current.payload.get("state") != "approved"
        ):
            return self._save(
                entry, delivery="blocked", error_code="result_feedback_version_changed"
            )
        if entry.delivery in {"accepted", "applied"}:
            return entry
        command = Command(
            kind="action_result",
            target_id=entry.suggestion_id,
            expected_version=entry.approved_version,
            outcome=outcome,
            receipt=entry.receipt,
        )
        # Stable full envelope, no timestamps from retries, no client-provided outcome.
        fid, when = feedback_identity("internal:executor", entry.feedback_id)
        raw = NewRawRecord(
            id=fid,
            data_type="feedback",
            event_start=when,
            event_end=when,
            source=Source(device="api_executor", channel="api", user="default"),
            adapter_version="api-execution-v1",
            dedup_key="internal:executor:" + entry.feedback_id,
            payload={"module": "extraction", "command": command.model_dump(mode="json")},
        )
        try:
            result = self.client.write(
                [AppendRaw(raw)], Context.feedback(fid), request_id=fid
            ).check()
            if len(result.results) != 1 or result[0].id != fid:
                raise ValueError
        except Exception:
            # Only the SAME feedback is replayed; the external call is never repeated.
            return self._save(entry, delivery="unknown", error_code="result_feedback_unknown")
        return self._save(entry, delivery="accepted", error_code=None)

    def status(self, suggestion_id: str) -> dict[str, object]:
        return journal_status(self.journal, suggestion_id)


def journal_status(journal: ExecutionJournal, suggestion_id: str) -> dict[str, object]:
    """Local facts only; caller must separately check current read authorization."""
    with journal.lock:
        entry = journal.get(suggestion_id)
        if entry is None:
            return {"executor_state": "not_started", "journal_revision": None}
        return {
            "executor_state": entry.state,
            "journal_revision": entry.revision,
            "approved_version": entry.approved_version,
            "feedback_delivery_state": entry.delivery,
            "error_code": entry.error_code,
            "requires_verification": entry.state in {"unknown", "executing"},
            "automatic_retry_allowed": False,
        }
