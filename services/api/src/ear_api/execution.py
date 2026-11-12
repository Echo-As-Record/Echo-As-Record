"""Trusted, explicitly injected external-action adapters; no real adapter is shipped.

ExecutionPump is the opt-in v2 two-phase ④/⑥ protocol. HTTP is NOT wired to it.
ExecutionService remains the legacy offline terminal-receipt compatibility slice;
it must not consume v2 journal rows or v2 suggestions. Never wire that legacy
process() method to an external worker. User inputs cannot supply external evidence.
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
from ear_extraction.domain import Action, ActionExecution, Command
from ear_extraction.suggestions import execution_key
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
            if not isinstance(value, ExternalResult) or value.__pydantic_extra__:
                raise ValueError
            # Construct/copy updates bypass validation; validating the instance itself
            # may also skip it. Validate raw fields strictly, not a model_dump that
            # could discard unexpected fields or serialize invalid values as valid.
            value = ExternalResult.model_validate(dict(vars(value)), strict=True)
            if (
                value.action_key != entry.action_key
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
            if (entry is not None and entry.protocol != "legacy") or current.payload.get(
                "execution"
            ) is not None:
                raise GatewayError(409, "execution_protocol_pump_required")
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
            elif evidence.outcome != "not_executed" or recovering:
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
                if evidence.outcome not in {"succeeded", "not_executed"}:
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
            if (entry is not None and entry.protocol != "legacy") or current.payload.get(
                "execution"
            ) is not None:
                raise GatewayError(409, "execution_protocol_pump_required")
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
        if entry.protocol != "legacy":
            raise GatewayError(409, "execution_protocol_pump_required")
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


class ExecutionPump(ExecutionService):
    """Opt-in single-owner pump. No HTTP wiring, leases or automatic risk retries.

    prepare() durably freezes intent and sends a start feedback, never executes.
    pump() waits for the exact owner version/receipt/identity before any effect.
    A crash after persisting executing only permits lookup. Keep the same journal
    across restarts; a lost journal cannot safely recover an already claimed action.
    All user-facing recovery calls require both versions and explicit acknowledgement.
    """

    def _entry(self, suggestion_id: str) -> ExecutionEntry:
        entry = self.journal.get(suggestion_id)
        if entry is None:
            raise GatewayError(409, "execution_not_started")
        if entry.protocol != "action-v2":
            raise GatewayError(409, "legacy_execution_requires_legacy_recovery")
        if entry.adapter != self.adapter.name:
            raise GatewayError(409, "execution_adapter_changed")
        return entry

    @staticmethod
    def _version(value: int) -> None:
        if type(value) is not int or value < 1:
            raise GatewayError(422, "invalid_input")

    def _matches_owner(self, entry: ExecutionEntry, current: SemanticRecord) -> bool:
        _, digest = approved_action(current)
        return (
            current.status == "active"
            and current.version == entry.owner_version
            and current.payload.get("state") == entry.owner_state
            and current.payload.get("receipt") == entry.owner_receipt
            and current.payload.get("execution") == entry.owner_execution
            and digest == entry.action_hash
        )

    def _blocked(self, entry: ExecutionEntry) -> ExecutionEntry:
        if entry.delivery == "blocked":
            return entry
        return self._save(entry, delivery="blocked", error_code="execution_owner_state_changed")

    def prepare(self, suggestion_id: str, expected_version: int) -> ExecutionEntry:
        """Freeze approval + queue start feedback. A 202/accepted is NOT execution permission."""
        self._version(expected_version)
        with self.journal.lock:
            current = self._current(suggestion_id)
            if current.version != expected_version:
                raise GatewayError(409, "suggestion_version_changed")
            if self.journal.get(suggestion_id) is not None:
                raise GatewayError(409, "execution_already_started")
            if (
                current.status != "active"
                or current.payload.get("state") != "approved"
                or current.payload.get("execution") is not None
            ):
                raise GatewayError(409, "suggestion_not_approved")
            action_json, action_hash = approved_action(current)
            key = execution_key(suggestion_id, current.version, action_hash)
            entry = ExecutionEntry(
                suggestion_id=suggestion_id,
                approved_version=current.version,
                action_json=action_json,
                action_hash=action_hash,
                action_key=key,
                adapter=self.adapter.name,
                feedback_id=new_id(),
                receipt="ear-api-action:" + key.removeprefix("ear_action_"),
                protocol="action-v2",
                owner_version=current.version,
                owner_state="approved",
                owner_receipt=current.payload.get("receipt"),
            )
            self.journal.save(entry)
            return self._transition(entry, "start", "executing")

    def _transition(
        self,
        entry: ExecutionEntry,
        operation: Literal["start", "result", "confirm_executed", "retry"],
        outcome: Literal["executing", "unknown", "executed", "failed"],
        **changes: object,
    ) -> ExecutionEntry:
        if entry.pending_command is not None:
            raise GatewayError(409, "execution_feedback_pending")
        attempt = entry.attempt + (operation == "retry")
        execution = ActionExecution(
            operation=operation,
            action_key=entry.action_key,
            action_hash=entry.action_hash,
            approved_version=entry.approved_version,
            attempt=attempt,
            acknowledge_duplicate_risk=operation == "retry",
            acknowledge_executed=operation == "confirm_executed",
        )
        receipt_hash = hashlib.sha256(
            f"{entry.action_key}:{entry.owner_version}:{attempt}:{operation}:{outcome}".encode()
        ).hexdigest()
        receipt = "ear-api-action:" + receipt_hash
        command = Command(
            kind="action_result",
            target_id=entry.suggestion_id,
            expected_version=entry.owner_version,
            outcome=outcome,
            receipt=receipt,
            execution=execution,
        )
        # Persist the entire feedback identity before sending. Lost replies replay
        # the same bytes/request id; no timestamp or command rebasing on retry.
        entry = self._save(
            entry,
            pending_command=command,
            feedback_id=new_id(),
            receipt=receipt,
            delivery="pending",
            attempt=attempt,
            **changes,
        )
        return self._sync(entry)

    def _sync(self, entry: ExecutionEntry) -> ExecutionEntry:
        current = self._current(entry.suggestion_id)
        command = entry.pending_command
        if entry.delivery == "blocked":
            return entry
        if command is None:
            return entry if self._matches_owner(entry, current) else self._blocked(entry)
        assert command.expected_version is not None and command.execution is not None
        _, digest = approved_action(current)
        if (
            current.status == "active"
            and current.version == command.expected_version + 1
            and current.payload.get("state") == command.outcome
            and current.payload.get("receipt") == command.receipt
            and current.payload.get("execution") == command.execution.model_dump(mode="json")
            and digest == entry.action_hash
        ):
            # This acknowledgement is itself durable before pump can execute.
            return self._save(
                entry,
                pending_command=None,
                owner_version=current.version,
                owner_state=command.outcome,
                owner_receipt=command.receipt,
                owner_execution=command.execution.model_dump(mode="json"),
                delivery="applied",
                error_code=None,
            )
        if not self._matches_owner(entry, current):
            return self._blocked(entry)
        if entry.delivery == "accepted":
            return entry  # Admission alone never advances the owner version.
        fid, when = feedback_identity("internal:execution-pump", entry.feedback_id)
        raw = NewRawRecord(
            id=fid,
            data_type="feedback",
            event_start=when,
            event_end=when,
            source=Source(device="api_executor", channel="api", user="default"),
            adapter_version="api-execution-v2",
            dedup_key="internal:execution-pump:" + entry.feedback_id,
            payload={"module": "extraction", "command": command.model_dump(mode="json")},
        )
        try:
            result = self.client.write(
                [AppendRaw(raw)], Context.feedback(fid), request_id=fid
            ).check()
            if len(result.results) != 1 or result[0].id != fid:
                raise ValueError
        except Exception:
            return self._save(entry, delivery="unknown", error_code="result_feedback_unknown")
        return self._save(entry, delivery="accepted", error_code=None)

    def process(self, suggestion_id: str) -> ExecutionEntry:
        """Inherited event dispatch also uses v2; it never falls through to legacy."""
        return self.pump(suggestion_id)

    def verify(self, suggestion_id: str, expected_revision: int) -> ExecutionEntry:
        # Old HTTP verify has no suggestion version. Do not silently loosen v2.
        raise GatewayError(409, "execution_recovery_requires_both_versions")

    def pump(self, suggestion_id: str) -> ExecutionEntry:
        """One bounded turn. The caller separately runs the actual ④ feedback consumer."""
        with self.journal.lock:
            entry = self._sync(self._entry(suggestion_id))
            if entry.pending_command is not None or entry.delivery == "blocked":
                return entry
            if entry.state in {"unknown", "succeeded", "failed"}:
                return entry
            if entry.state == "executing":
                # Only a crash can leave this state without a pending result.
                return self._record_evidence(entry, self._evidence(entry), recovery=True)
            if entry.owner_state == "approved":
                # Crash between initial journal insert and start-feedback staging.
                return self._transition(entry, "start", "executing")
            if entry.owner_state != "executing":
                return self._blocked(entry)
            evidence = self._evidence(entry)
            may_execute = evidence.outcome == "not_executed" or (
                evidence.outcome == "unknown" and entry.retry_acknowledged is True
            )
            if not may_execute:
                return self._record_evidence(entry, evidence)
            # Re-read after lookup; changed version, receipt, state or action blocks.
            if not self._matches_owner(entry, self._current(suggestion_id)):
                return self._blocked(entry)
            entry = self._save(entry, state="executing", error_code=None)
            evidence = self._evidence(entry, execute=True)
            return self._record_evidence(entry, evidence)

    def _record_evidence(
        self, entry: ExecutionEntry, evidence: ExternalResult, *, recovery: bool = False
    ) -> ExecutionEntry:
        if evidence.outcome == "succeeded":
            state, outcome = "succeeded", "executed"
        elif evidence.outcome == "not_executed" and not recovery:
            state, outcome = "failed", "failed"
        else:
            state, outcome = "unknown", "unknown"
        if entry.owner_state == "unknown" and outcome == "unknown":
            return entry
        return self._transition(
            entry,
            "result",
            outcome,
            state=state,
            external_ref=evidence.external_ref if state == "succeeded" else None,
            error_code="external_result_unknown" if state == "unknown" else None,
        )

    def _recovery_entry(
        self, suggestion_id: str, expected_version: int, expected_revision: int
    ) -> ExecutionEntry:
        self._version(expected_version)
        self._version(expected_revision)
        current = self._current(suggestion_id)
        entry = self._entry(suggestion_id)
        if entry.revision != expected_revision:
            raise GatewayError(409, "execution_revision_conflict")
        if current.version != expected_version:
            raise GatewayError(409, "suggestion_version_changed")
        if entry.pending_command is not None:
            raise GatewayError(409, "execution_feedback_pending")
        if entry.delivery == "blocked" or not self._matches_owner(entry, current):
            raise GatewayError(409, "execution_owner_state_changed")
        return entry

    def reconcile(
        self, suggestion_id: str, *, expected_version: int, expected_revision: int
    ) -> ExecutionEntry:
        """Only lookup. Authoritative absence is not new permission to execute."""
        with self.journal.lock:
            entry = self._recovery_entry(suggestion_id, expected_version, expected_revision)
            if entry.state not in {"executing", "unknown"}:
                raise GatewayError(409, "execution_not_uncertain")
            return self._record_evidence(entry, self._evidence(entry), recovery=True)

    def confirm_executed(
        self,
        suggestion_id: str,
        *,
        expected_version: int,
        expected_revision: int,
        acknowledge_executed: bool,
    ) -> ExecutionEntry:
        """User attestation, not adapter evidence; no external_ref input or fabrication."""
        if acknowledge_executed is not True:
            raise GatewayError(409, "execution_confirmation_required")
        with self.journal.lock:
            entry = self._recovery_entry(suggestion_id, expected_version, expected_revision)
            if entry.state != "unknown" or entry.owner_state != "unknown":
                raise GatewayError(409, "execution_not_uncertain")
            return self._transition(
                entry,
                "confirm_executed",
                "executed",
                state="succeeded",
                user_confirmed=True,
                external_ref=None,
                error_code=None,
            )

    def retry(
        self,
        suggestion_id: str,
        *,
        expected_version: int,
        expected_revision: int,
        acknowledge_duplicate_risk: bool,
    ) -> ExecutionEntry:
        """Stage another owner-approved attempt with the SAME key; never executes here."""
        if acknowledge_duplicate_risk is not True:
            raise GatewayError(409, "duplicate_risk_confirmation_required")
        with self.journal.lock:
            entry = self._recovery_entry(suggestion_id, expected_version, expected_revision)
            if entry.state not in {"unknown", "failed"} or entry.owner_state != entry.state:
                raise GatewayError(409, "execution_retry_not_allowed")
            return self._transition(
                entry,
                "retry",
                "executing",
                state="prepared",
                retry_acknowledged=True,
                error_code=None,
            )

    def recovery_request(self, suggestion_id: str) -> dict[str, object]:
        """Display-only request to check external state. No external references are exposed."""
        with self.journal.lock:
            current = self._current(suggestion_id)
            entry = self._entry(suggestion_id)
            settled = (
                entry.pending_command is None
                and entry.delivery != "blocked"
                and self._matches_owner(entry, current)
            )
            return {
                **self.status(suggestion_id),
                "suggestion_id": suggestion_id,
                "expected_version": current.version,
                "expected_revision": entry.revision,
                "suggestion_state": current.payload.get("state"),
                "requires_user_verification": entry.state == "unknown",
                "can_reconcile": settled and entry.state in {"executing", "unknown"},
                "can_confirm_executed": settled and entry.owner_state == "unknown",
                "can_retry": settled and entry.owner_state in {"unknown", "failed"},
                "acknowledge_executed": False,
                "acknowledge_duplicate_risk": False,
                "warning": "Check the external system; retry may duplicate the action.",
                "confirmation_source": (
                    "user" if entry.user_confirmed else "adapter" if entry.external_ref else None
                ),
                "http_execution_connected": False,
            }
