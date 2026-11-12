"""Single-writer candidate-set fence; never a substitute for registered inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

from ear_contracts import SystemRecord, is_id, new_id
from ear_storage_client import Create, PutSystemRecord, Reader, WriteOp

from ear_attribution.payloads import DomainError, entity, references


def derived_id(domain: str, anchor: str) -> str:
    """Domain-separated UUIDv7 retaining the anchor's timestamp, not UUIDv5."""
    raw = bytearray(hashlib.sha256(f"ear.attribution:{domain}:{anchor}".encode()).digest()[:16])
    raw[:6] = UUID(anchor).bytes[:6]
    raw[6] = (raw[6] & 15) | 0x70
    raw[8] = (raw[8] & 63) | 0x80
    return str(UUID(bytes=bytes(raw)))


STATE_ID = derived_id("state:v1", "019a7100-0000-7000-8000-000000000000")


@dataclass
class Fence:
    current: SystemRecord | None
    unknown_id: str
    next_person_number: int
    reference_revision: int
    changed: bool = False
    initial_ops: list[WriteOp] = field(default_factory=list)

    def allocate_person(self) -> int:
        number = self.next_person_number
        self.next_person_number += 1
        self.changed = True
        return number

    def operation(self) -> PutSystemRecord:
        payload = dict(self.current.payload) if self.current else {}
        payload.update(
            schema_version=1,
            unknown_place_id=self.unknown_id,
            next_person_number=self.next_person_number,
            reference_revision=self.reference_revision + int(self.changed),
        )
        return PutSystemRecord(
            "attribution_state",
            STATE_ID,
            payload,
            expected_revision=self.current.revision if self.current else None,
        )


def read_fence(reader: Reader) -> Fence:
    current = reader.get_system_record(STATE_ID)
    if current is None:
        unknown_id = new_id()
        return Fence(
            None,
            unknown_id,
            1,
            0,
            initial_ops=[
                Create(
                    "place",
                    unknown_id,
                    {"name": None, "references": [], "is_unknown": True},
                    dedup_key="attribution:unknown_place:v1",
                )
            ],
        )
    p = current.payload
    if (
        current.data_type != "attribution_state"
        or p.get("schema_version") != 1
        or not is_id(p.get("unknown_place_id", ""))
        or type(p.get("next_person_number")) is not int
        or p["next_person_number"] < 1
        or type(p.get("reference_revision")) is not int
        or p["reference_revision"] < 0
    ):
        raise DomainError("invalid_attribution_state")
    unknown = entity(reader, p["unknown_place_id"], "place")
    if unknown.payload.get("is_unknown") is not True or references(unknown):
        raise DomainError("invalid_unknown_place")
    return Fence(current, unknown.id, p["next_person_number"], p["reference_revision"])
