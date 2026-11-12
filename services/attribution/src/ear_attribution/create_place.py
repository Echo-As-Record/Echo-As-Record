"""Bounded explicit GPS selection; one fenced batch, never a range job."""

from __future__ import annotations

from typing import Any

from ear_contracts import RawRecord, SemanticRecord, VersionRef, is_id, new_id
from ear_storage_client import (
    Create,
    DependencyInput,
    Query,
    Reader,
    RegisterDependencies,
    Update,
    WriteOp,
    iter_query,
)

from ear_attribution.matching import coordinates
from ear_attribution.payloads import (
    DomainError,
    attribution_payload,
    current_attribution,
    entity,
    raw_subject,
    read_attribution,
    references,
)
from ear_attribution.state import Fence

MAX_GPS_IDS = 100


def plan_create_place(
    reader: Reader, feedback: RawRecord, command: dict, fence: Fence
) -> tuple[list[WriteOp], dict[str, Any]]:
    name, ids = command.get("name"), command.get("gps_ids")
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise DomainError("invalid_name")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= MAX_GPS_IDS
        or any(not isinstance(id, str) or not is_id(id) for id in ids)
    ):
        raise DomainError("invalid_gps_ids")
    if len(set(ids)) != len(ids):
        raise DomainError("duplicate_gps_ids")
    # There is no single existing object/version to guard for this multi-object command.
    # Accept ⑥'s normalized null, but never silently discard an explicit guard.
    if command.get("expected_version") is not None:
        raise DomainError("invalid_expected_version")

    selected: list[tuple[RawRecord, SemanticRecord | None]] = []
    for id in ids:
        subject = raw_subject(reader, id)
        if subject.data_type != "gps_point":
            raise DomainError("subject_type_mismatch")
        coordinates(subject)
        current = current_attribution(reader, id)
        if current is not None:
            view = read_attribution(reader, current)
            # A new place necessarily differs from the existing target. Unlike
            # correct_attribution, this command has no explicit previous-target consent.
            if view.protected is True:
                raise DomainError("protected_attribution_conflict")
            if view.protected is None:
                raise DomainError("legacy_protection_unknown")
            entity(reader, view.target_id, "place")
        selected.append((subject, current))

    ops: list[WriteOp] = list(fence.initial_ops)
    selected_ids = set(ids)
    owners: set[str] = set()
    for place in iter_query(reader, Query("place", order="id")):
        if not isinstance(place, SemanticRecord):
            raise DomainError("invalid_candidate")
        refs = references(place)
        flag = place.payload.get("is_unknown", False)
        if type(flag) is not bool or flag != (place.id == fence.unknown_id):
            raise DomainError("invalid_unknown_place")
        if flag and refs:
            raise DomainError("invalid_unknown_place")
        moving = selected_ids.intersection(refs)
        if not moving:
            continue
        if owners.intersection(moving):
            raise DomainError("reference_owner_conflict")
        owners.update(moving)
        # Remove BEFORE creating the new reference owner (storage enforces exclusivity).
        ops.append(
            Update(
                "place",
                place.id,
                place.version,
                {**place.payload, "references": [id for id in refs if id not in selected_ids]},
                skip_history=True,
            )
        )

    target_id = new_id()
    ops.append(
        Create(
            "place",
            target_id,
            {"name": name, "references": list(ids), "is_unknown": False},
            skip_history=True,
        )
    )
    dependency = DependencyInput(
        VersionRef(data_type="place", id=target_id, version=1), ("references", "is_unknown")
    )
    attribution_ids = []
    for subject, current in selected:
        payload = attribution_payload(
            subject,
            target_id,
            previous=current.payload if current is not None else None,
            feedback_id=feedback.id,
        )
        id = current.id if current is not None else new_id()
        attribution_ids.append(id)
        if current is None:
            ops.append(Create("attribution", id, payload))
        else:
            ops.append(Update("attribution", id, current.version, payload))
        # Explicit selection, not automatic matching: the new target is the complete
        # mutable computation input. Do not inherit old candidate/name dependencies.
        ops.append(RegisterDependencies("attribution", id, [dependency]))

    # All ③ candidate/reference writers share this CAS. AssertTypeRevision cannot
    # guard place/attribution here: it forbids changing that same type in the batch.
    # Advancing the fence also invalidates already planned automatic candidate matches.
    fence.changed = True
    return [fence.operation(), *ops], {
        "new_target_id": target_id,
        "subject_ids": list(ids),
        "attribution_ids": attribution_ids,
    }
