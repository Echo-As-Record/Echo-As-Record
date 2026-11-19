"""Suggestion lifecycle; execution belongs to module ⑥."""

import hashlib
import json

from ear_contracts import SemanticRecord, new_id
from ear_storage_client import Create, Reader, Supersede, Update, WriteOp

from ear_extraction.domain import Action, Blocked, Command, Generated, Interval
from ear_extraction.persistence import records, semantic


def create_suggestion(
    artifact_id: str, version: int, result: Generated, interval: Interval
) -> list[WriteOp]:
    if result.action is None:
        return []
    return [
        Create(
            "action_suggestion",
            new_id(),
            {
                "artifact_id": artifact_id,
                "artifact_version": version,
                "action": result.action.model_dump(mode="json"),
                "state": "pending",
            },
            event_start=interval.start,
            event_end=interval.end,
        )
    ]


def pending_replacements(reader: Reader, artifact_ids: set[str]) -> list[WriteOp]:
    return [
        Supersede("action_suggestion", r.id, r.version)
        for r in records(reader, "action_suggestion")
        if r.payload.get("artifact_id") in artifact_ids and r.payload.get("state") == "pending"
    ]


def suggestion_operations(reader: Reader, command: Command) -> list[WriteOp]:
    r: SemanticRecord = semantic(reader, command.target_id, "action_suggestion")
    if r.version != command.expected_version:
        raise Blocked("suggestion_version_changed")
    state = r.payload.get("state")
    if command.kind == "review_suggestion":
        if state == command.decision:
            return []
        if state != "pending":
            raise Blocked("suggestion_already_reviewed")
        payload = {**r.payload, "state": command.decision}
    elif command.execution is not None:
        payload = execution_payload(r, command)
    else:
        if r.payload.get("execution") is not None:
            raise Blocked("execution_protocol_required")
        if state == command.outcome and r.payload.get("receipt") == command.receipt:
            return []
        if state != "approved":
            raise Blocked("suggestion_not_approved")
        payload = {**r.payload, "state": command.outcome, "receipt": command.receipt}
    return [Update("action_suggestion", r.id, r.version, payload)]


def execution_key(suggestion_id: str, version: int, action_hash: str) -> str:
    digest = hashlib.sha256(
        f"ear-api-execution-v2:{suggestion_id}:{version}:{action_hash}".encode()
    ).hexdigest()
    return "ear_action_" + digest


def execution_payload(record: SemanticRecord, command: Command) -> dict:
    """Validate an exact-version transition; no stale-version idempotence shortcut.

    The existing worker commits this Update and its successful run atomically. The
    executor waits for this exact payload/version, not feedback admission/run enqueue.
    Legacy terminal receipts remain valid only for suggestions without a v2 handoff.
    """
    e = command.execution
    assert e is not None
    action = Action.model_validate(record.payload.get("action"))
    text = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != e.action_hash or e.action_key != execution_key(
        record.id, e.approved_version, digest
    ):
        raise Blocked("execution_identity_mismatch")
    state = record.payload.get("state")
    old = record.payload.get("execution")
    if e.operation == "start":
        if (
            state != "approved"
            or old is not None
            or e.approved_version != record.version
            or e.attempt != 1
        ):
            raise Blocked("execution_start_state_mismatch")
    else:
        if not isinstance(old, dict) or any(
            old.get(key) != getattr(e, key)
            for key in ("action_key", "action_hash", "approved_version")
        ):
            raise Blocked("execution_identity_mismatch")
        old_attempt = old.get("attempt")
        if type(old_attempt) is not int:
            raise Blocked("execution_attempt_mismatch")
        if e.operation == "retry":
            valid = state in {"unknown", "failed"} and e.attempt == old_attempt + 1
        elif e.operation == "confirm_executed":
            valid = state == "unknown" and e.attempt == old_attempt
        else:
            valid = state in {"executing", "unknown"} and e.attempt == old_attempt
            # No duplicate unknown transition/version churn.
            valid = valid and not (state == "unknown" and command.outcome == "unknown")
        if not valid:
            raise Blocked("execution_transition_not_allowed")
    return {
        **record.payload,
        "state": command.outcome,
        "receipt": command.receipt,
        "execution": e.model_dump(mode="json"),
    }
