"""Evidence assembly and deterministic checks around model-produced activity boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Query, Reader, TimeRange, iter_query

from ear_extraction.domain import Activity, Interval, Segmentation, Waiting
from ear_extraction.persistence import records, semantic

RAW_TYPES = ("transcript_segment", "ambient_sound", "gps_point", "phone_info", "local_speaker")
REFERENCES = re.compile(r"\[\[(person|place):([^\]]+)\]\]")


@dataclass
class Inputs:
    raw: list[RawRecord]
    attributions: list[SemanticRecord]
    entities: list[SemanticRecord]

    def public(self, *, anonymous: bool = False) -> dict:
        data = {"evidence": [r.model_dump(mode="json") for r in self.raw]}
        if not anonymous:
            data["attributions"] = [r.model_dump(mode="json") for r in self.attributions]
            # Only names are semantic entity inputs; references never reach the model.
            # Preserve absence versus null, and depend on name even when absent.
            data["entities"] = [
                {
                    "id": r.id,
                    "data_type": r.data_type,
                    "payload": {k: r.payload[k] for k in ("name",) if k in r.payload},
                }
                for r in self.entities
            ]
        return data


def raw_inputs(reader: Reader, interval: Interval) -> list[RawRecord]:
    time = TimeRange(interval.start, interval.end)
    for kind in ("voiceprint", "gps_point"):
        if reader.count(Query(kind, time=time, unreferenced_by=("attribution", "subject_id"))):
            raise Waiting("attributions_pending")
    raw = [
        r
        for kind in RAW_TYPES
        for r in iter_query(reader, Query(kind, time=time))
        if isinstance(r, RawRecord)
    ]
    raw.sort(key=lambda r: (r.event_start, r.id))
    return raw


def inputs(reader: Reader, interval: Interval) -> Inputs:
    raw = raw_inputs(reader, interval)
    attrs = records(reader, "attribution", interval)
    entities = {}
    for a in attrs:
        kind, id = a.payload.get("target_type"), a.payload.get("target_id")
        if kind not in {"person", "place"}:
            raise Waiting("attribution_target_pending")
        entity = semantic(reader, id, kind)
        entities[entity.id] = entity
    return Inputs(raw, attrs, list(entities.values()))


def check_refs(text: str, entities: list[SemanticRecord]) -> None:
    allowed = {(r.data_type, r.id) for r in entities}
    refs = REFERENCES.findall(text)
    if any(ref not in allowed for ref in refs):
        raise ValueError("unknown_entity_reference")
    if "[[" in REFERENCES.sub("", text):
        raise ValueError("invalid_reference_syntax")


def validate_activity(activity: Activity, interval: Interval, evidence: list[RawRecord]) -> None:
    if activity.start < interval.start or activity.end > interval.end:
        raise ValueError("activity_out_of_range")
    by_id = {r.id: r for r in evidence}
    for id in activity.evidence_ids:
        if id not in by_id:
            raise ValueError("unknown_evidence_reference")
        r = by_id[id]
        if not TimeRange(activity.start, activity.end).overlaps(r.event_start, r.event_end):
            raise ValueError("evidence_outside_activity")


def validate_segments(result: Segmentation, interval: Interval, evidence: list[RawRecord]) -> None:
    if not result.activities and not result.empty_reason.strip():
        raise ValueError("empty_segmentation_requires_reason")
    previous = interval.start
    for a in result.activities:
        validate_activity(a, interval, evidence)
        if a.start < previous:
            raise ValueError("overlapping_or_unsorted_activities")
        previous = a.end
