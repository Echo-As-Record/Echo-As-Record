"""Suggestion lifecycle; execution belongs to module ⑥."""

from ear_contracts import SemanticRecord, new_id
from ear_storage_client import Create, Reader, Supersede, Update, WriteOp

from ear_extraction.domain import Blocked, Command, Generated, Interval
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
    else:
        if state == command.outcome and r.payload.get("receipt") == command.receipt:
            return []
        if state != "approved":
            raise Blocked("suggestion_not_approved")
        payload = {**r.payload, "state": command.outcome, "receipt": command.receipt}
    return [Update("action_suggestion", r.id, r.version, payload)]
