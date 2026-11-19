"""Deterministic matching of synthetic or authorized raw inputs; no model calls."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ear_contracts import RawRecord, SemanticRecord, VersionRef
from ear_storage_client import DependencyInput, Query, Reader, iter_query

from ear_attribution.payloads import PAIRS, DomainError, raw_subject, references


@dataclass(frozen=True)
class MatchConfig:
    cosine_threshold: float
    gps_threshold_m: float

    def __post_init__(self) -> None:
        if (
            type(self.cosine_threshold) not in (int, float)
            or not math.isfinite(self.cosine_threshold)
            or not -1 <= self.cosine_threshold <= 1
        ):
            raise ValueError("cosine_threshold must be finite in [-1, 1]")
        if (
            type(self.gps_threshold_m) not in (int, float)
            or not math.isfinite(self.gps_threshold_m)
            or self.gps_threshold_m < 0
        ):
            raise ValueError("gps_threshold_m must be finite and nonnegative")


def vector(record: RawRecord) -> tuple[str, tuple[float, ...]]:
    model = record.payload.get("model_version")
    embedding = record.payload.get("embedding")
    if not isinstance(model, str) or not model.strip() or not isinstance(embedding, dict):
        raise DomainError("invalid_embedding")
    values, shape = embedding.get("values"), embedding.get("shape")
    if (
        not isinstance(values, list)
        or not values
        or not isinstance(shape, list)
        or len(shape) != 1
        or type(shape[0]) is not int
        or shape[0] != len(values)
        or any(type(v) not in (int, float) for v in values)
    ):
        raise DomainError("invalid_embedding")
    try:
        v = tuple(float(x) for x in values)
    except (OverflowError, ValueError):
        raise DomainError("invalid_embedding") from None
    if not all(math.isfinite(x) for x in v) or max(map(abs, v)) == 0:
        raise DomainError("invalid_embedding")
    # Scale first: finite, very large/small vectors must not overflow/underflow.
    scale = max(map(abs, v))
    scaled = tuple(x / scale for x in v)
    norm = math.hypot(*scaled)
    return model, tuple(x / norm for x in scaled)


def coordinates(record: RawRecord) -> tuple[float, float]:
    lat, lon = record.payload.get("latitude"), record.payload.get("longitude")
    if (
        not isinstance(lat, int | float)
        or isinstance(lat, bool)
        or not isinstance(lon, int | float)
        or isinstance(lon, bool)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise DomainError("invalid_coordinates")
    return float(lat), float(lon)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine approximation on WGS84 coordinates, mean Earth radius in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2
    h += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def reference_dependency(record: SemanticRecord) -> DependencyInput:
    fields = ("references", "is_unknown") if record.data_type == "place" else ("references",)
    return DependencyInput(
        VersionRef(data_type=record.data_type, id=record.id, version=record.version), fields
    )


@dataclass(frozen=True)
class Match:
    target_id: str | None
    dependencies: tuple[DependencyInput, ...]


def match(reader: Reader, subject: RawRecord, config: MatchConfig, unknown_id: str) -> Match:
    kind = PAIRS[subject.data_type]
    query_vector = vector(subject) if kind == "person" else None
    query_point = coordinates(subject) if kind == "place" else None
    candidates: list[tuple[SemanticRecord, list[RawRecord]]] = []
    dependencies: list[DependencyInput] = []
    owners: dict[str, str] = {}
    for item in iter_query(reader, Query(kind, order="id")):
        if not isinstance(item, SemanticRecord):
            raise DomainError("invalid_candidate")
        refs = references(item)
        # Include even empty/mismatched candidates: their reference field was read.
        dependencies.append(reference_dependency(item))
        if kind == "place":
            flag = item.payload.get("is_unknown", False)
            if type(flag) is not bool or flag != (item.id == unknown_id):
                raise DomainError("invalid_unknown_place")
            if flag:
                if refs:
                    raise DomainError("unknown_place_has_references")
                continue
        raw_refs = []
        for id in refs:
            raw = raw_subject(reader, id)
            if raw.data_type != subject.data_type:
                raise DomainError("reference_type_mismatch")
            if id in owners:
                raise DomainError("reference_owner_conflict")
            owners[id] = item.id
            raw_refs.append(raw)
        candidates.append((item, raw_refs))
    # Being a reference takes precedence over distance/similarity.
    if subject.id in owners:
        return Match(owners[subject.id], tuple(dependencies))
    best: tuple[float, str] | None = None
    for item, refs in candidates:
        for raw in refs:
            if query_vector is not None:
                model, values = vector(raw)
                if model != query_vector[0] or len(values) != len(query_vector[1]):
                    continue
                similarity = math.fsum(a * b for a, b in zip(values, query_vector[1], strict=True))
                if similarity < config.cosine_threshold:
                    continue
                score = -similarity
            else:
                assert query_point is not None
                score = distance_m(query_point, coordinates(raw))
                if score > config.gps_threshold_m:
                    continue
            proposal = (score, item.id)
            if best is None or proposal < best:
                best = proposal
    return Match(best[1] if best is not None else None, tuple(dependencies))
