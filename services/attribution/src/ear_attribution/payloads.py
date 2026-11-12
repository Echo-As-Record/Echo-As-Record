"""Additive business readers; legacy uncertainty is never an automatic unlock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ear_contracts import RawRecord, SemanticRecord, is_id
from ear_storage_client import Query, Reader, iter_query

PAIRS = {"voiceprint": "person", "gps_point": "place"}


class DomainError(ValueError):
    """A bounded, public error code, never raw personal input."""


def raw_subject(reader: Reader, id: str) -> RawRecord:
    if not is_id(id):
        raise DomainError("invalid_subject")
    record = reader.get(id)
    if not isinstance(record, RawRecord) or record.data_type not in PAIRS:
        raise DomainError("invalid_subject")
    return record


def entity(reader: Reader, id: str, kind: str | None = None) -> SemanticRecord:
    record = reader.get(id)
    if (
        not isinstance(record, SemanticRecord)
        or record.superseded
        or record.data_type not in {"person", "place"}
        or (kind is not None and record.data_type != kind)
    ):
        raise DomainError("target_unavailable")
    return record


def references(record: SemanticRecord) -> list[str]:
    values = record.payload.get("references", [])
    if (
        not isinstance(values, list)
        or any(not isinstance(v, str) or not is_id(v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise DomainError("invalid_references")
    return list(values)


def current_attribution(reader: Reader, subject_id: str) -> SemanticRecord | None:
    records = list(iter_query(reader, Query("attribution", where={"subject_id": subject_id})))
    if len(records) > 1:
        raise DomainError("duplicate_attribution")
    if not records:
        return None
    record = records[0]
    if not isinstance(record, SemanticRecord):
        raise DomainError("invalid_attribution")
    return record


@dataclass(frozen=True)
class Attribution:
    subject: RawRecord
    target_type: str
    target_id: str
    # None is legacy/unknown, NOT False.
    protected: bool | None


def read_attribution(reader: Reader, record: SemanticRecord) -> Attribution:
    payload = record.payload
    subject = raw_subject(reader, payload.get("subject_id", ""))
    kind = PAIRS[subject.data_type]
    if payload.get("subject_type", subject.data_type) != subject.data_type:
        raise DomainError("subject_type_mismatch")
    if payload.get("target_type") != kind or not is_id(payload.get("target_id", "")):
        raise DomainError("target_type_mismatch")
    protected = payload.get("user_protected")
    if protected is not None and type(protected) is not bool:
        raise DomainError("invalid_protection")
    return Attribution(subject, kind, payload["target_id"], protected)


def attribution_payload(
    subject: RawRecord,
    target_id: str,
    *,
    previous: Mapping[str, Any] | None = None,
    feedback_id: str | None = None,
) -> dict[str, Any]:
    payload = dict(previous or {})
    payload.update(
        subject_type=subject.data_type,
        subject_id=subject.id,
        target_type=PAIRS[subject.data_type],
        target_id=target_id,
    )
    if feedback_id is not None:
        payload["user_protected"] = True
        # Repeated confirmation must not manufacture a new business version.
        if not payload.get("protection_feedback_id"):
            payload["protection_feedback_id"] = feedback_id
    elif previous is None:
        payload.update(user_protected=False, protection_feedback_id=None)
    return payload
