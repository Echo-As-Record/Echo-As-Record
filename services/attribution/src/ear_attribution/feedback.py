"""Stored ⑥ commands, not the closed public-input validator (null defaults are valid).

Bounded create_place is atomic; range jobs remain explicitly unsupported.
Rejected commands have durable receipts, never an unpersisted background job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ear_contracts import RawRecord, Record, SemanticRecord, format_utc, is_id
from ear_storage_client import (
    AssertTypeRevision,
    PutSystemRecord,
    Query,
    Reader,
    RegisterDependencies,
    Update,
    WriteOp,
    iter_query,
)

from ear_attribution.create_place import plan_create_place
from ear_attribution.matching import coordinates, reference_dependency, vector
from ear_attribution.payloads import (
    PAIRS,
    DomainError,
    attribution_payload,
    current_attribution,
    entity,
    raw_subject,
    read_attribution,
    references,
)
from ear_attribution.state import Fence, derived_id, read_fence

SUPPORTED = {
    "correct_attribution",
    "confirm_attribution",
    "add_reference",
    "remove_reference",
    "rename",
    "create_place",
}


def feedback_result_id(id: str) -> str:
    return derived_id("feedback_result:v1", id)


def _id(command: dict, field: str) -> str:
    value = command.get(field)
    if not isinstance(value, str) or not is_id(value):
        raise DomainError("invalid_command")
    return value


def _expected(command: dict, record: SemanticRecord) -> None:
    expected = command.get("expected_version")
    if expected is not None and (type(expected) is not int or expected < 1):
        raise DomainError("invalid_expected_version")
    if expected is not None and expected != record.version:
        raise DomainError("expected_version_conflict")


def _references(
    reader: Reader,
    record: SemanticRecord,
    subject: RawRecord,
    fence: Fence,
) -> list[str]:
    if record.id == fence.unknown_id or record.payload.get("is_unknown") is True:
        raise DomainError("unknown_place_cannot_be_reference_target")
    result = references(record)
    for id in result:
        if raw_subject(reader, id).data_type != subject.data_type:
            raise DomainError("reference_type_mismatch")
    return result


def _update_refs(record: SemanticRecord, refs: list[str], skip: bool) -> Update:
    return Update(
        record.data_type,
        record.id,
        record.version,
        {**record.payload, "references": refs},
        skip_history=skip,
    )


def _dependency_after(record: SemanticRecord, ops: list[WriteOp]):
    dep = reference_dependency(record)
    for op in ops:
        if isinstance(op, Update) and op.id == record.id and dict(op.payload) != record.payload:
            return type(dep)(dep.ref.model_copy(update={"version": record.version + 1}), dep.fields)
    return dep


def _plan(reader: Reader, raw: RawRecord, command: dict) -> tuple[list[WriteOp], dict[str, Any]]:
    kind = command.get("kind")
    if not isinstance(kind, str) or kind not in SUPPORTED:
        raise DomainError("unsupported_command")
    fence = read_fence(reader)
    if kind == "create_place":
        return plan_create_place(reader, raw, command, fence)
    ops: list[WriteOp] = list(fence.initial_ops)
    if kind == "rename":
        target = entity(reader, _id(command, "target_id"))
        _expected(command, target)
        name = command.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise DomainError("invalid_name")
        if target.id == fence.unknown_id:
            raise DomainError("unknown_place_cannot_be_renamed")
        ops.append(
            Update(target.data_type, target.id, target.version, {**target.payload, "name": name})
        )
        return [fence.operation(), *ops], {"new_target_id": target.id}

    subject = raw_subject(reader, _id(command, "subject_id"))
    # Never turn unusable raw inputs into references that poison later matching.
    if subject.data_type == "voiceprint":
        vector(subject)
    else:
        coordinates(subject)
    current = current_attribution(reader, subject.id)
    if current is None:
        raise DomainError("attribution_pending")
    view = read_attribution(reader, current)
    target_id = _id(command, "new_target_id" if kind == "correct_attribution" else "target_id")
    target = entity(reader, target_id, PAIRS[subject.data_type])
    # Guard refers to the object the command changes, not a coincidental raw version.
    _expected(
        command, current if kind in {"correct_attribution", "confirm_attribution"} else target
    )
    target_refs = _references(reader, target, subject, fence)
    previous_target = view.target_id
    if kind == "correct_attribution":
        if previous_target != _id(command, "previous_target_id"):
            raise DomainError("previous_target_conflict")
        if previous_target != target.id:
            previous = entity(reader, previous_target, target.data_type)
            previous_refs = references(previous)
            if subject.id in previous_refs:
                ops.append(
                    _update_refs(previous, [r for r in previous_refs if r != subject.id], True)
                )
                fence.changed = True
    elif kind in {"confirm_attribution", "add_reference"} and previous_target != target.id:
        raise DomainError("attribution_target_conflict")

    if kind in {"add_reference", "remove_reference"}:
        rejudge = command.get("rejudge_history", False)
        if type(rejudge) is not bool:
            raise DomainError("invalid_history_choice")
        skip = not rejudge
    else:
        skip = True
    if kind != "remove_reference":
        allowed_owners = {target.id}
        if kind == "correct_attribution":
            allowed_owners.add(previous_target)
        if any(
            owner.id not in allowed_owners
            for owner in iter_query(
                reader, Query(target.data_type, contains={"references": subject.id})
            )
        ):
            raise DomainError("reference_owner_conflict")
    updated_refs = list(target_refs)
    if kind == "remove_reference":
        updated_refs = [r for r in updated_refs if r != subject.id]
    elif subject.id not in updated_refs:
        updated_refs.append(subject.id)
    if updated_refs != target_refs:
        ops.append(_update_refs(target, updated_refs, skip))
        fence.changed = True
    result: dict[str, Any] = {
        "subject_id": subject.id,
        "previous_target_id": previous_target,
        "new_target_id": target.id,
        "attribution_id": current.id,
        "before_version": current.version,
        "after_version": current.version,
    }
    if kind in {"correct_attribution", "confirm_attribution"}:
        payload = attribution_payload(
            subject, target.id, previous=current.payload, feedback_id=raw.id
        )
        ops.append(Update("attribution", current.id, current.version, payload, skip_history=False))
        # A user instruction fixes the target; it did not run automatic candidate matching.
        # Its actual mutable input is the selected target's final reference field.
        ops.append(
            RegisterDependencies("attribution", current.id, [_dependency_after(target, ops)])
        )
        result["after_version"] += int(payload != current.payload)
    else:
        # Reference maintenance does not recompute the attribution or refresh stale
        # matching dependencies. A read-only type guard protects its observed target.
        ops.append(AssertTypeRevision("attribution", reader.type_revision("attribution")))
    return [fence.operation(), *ops], result


def plan_feedback(reader: Reader, record: Record) -> list[WriteOp] | None:
    if not isinstance(record, RawRecord) or record.data_type != "feedback":
        raise DomainError("invalid_feedback")
    if record.payload.get("module") != "attribution":
        return None
    result_id = feedback_result_id(record.id)
    existing = reader.get_system_record(result_id)
    if existing is not None:
        if (
            existing.data_type != "attribution_feedback_result"
            or existing.payload.get("feedback_id") != record.id
        ):
            raise DomainError("feedback_result_collision")
        return []
    command = record.payload.get("command")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "feedback_id": record.id,
        "command_kind": command.get("kind") if isinstance(command, dict) else None,
        "status": "succeeded",
        "error_code": None,
        "completed_at": format_utc(datetime.now(UTC)),
    }
    try:
        if not isinstance(command, dict):
            raise DomainError("invalid_command")
        ops, metadata = _plan(reader, record, command)
        payload.update(metadata)
    except DomainError as exc:
        ops = []
        payload.update(status="rejected", error_code=str(exc))
    return [*ops, PutSystemRecord("attribution_feedback_result", result_id, payload)]
