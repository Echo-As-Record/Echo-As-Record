"""Deterministic matching of synthetic or authorized raw inputs; no model calls."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

from ear_contracts import RawRecord, SemanticRecord, VersionRef
from ear_storage_client import DependencyInput, Query, Reader, iter_query

from ear_attribution.payloads import PAIRS, DomainError, raw_subject, references
from ear_attribution.scoring import Reference, ScoreDiagnostic, decide, rank_identity_matches


@dataclass(frozen=True)
class MatchConfig:
    cosine_threshold: float
    gps_threshold_m: float
    unnamed_margin: float
    named_threshold: float
    named_margin: float
    aggregation: str
    top_k: int | None
    safe_anonymous_reuse: bool

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
        for name, value in (
            ("unnamed_margin", self.unnamed_margin),
            ("named_margin", self.named_margin),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not -1 <= self.named_threshold <= 1:
            raise ValueError("named_threshold must be finite in [-1, 1]")
        if self.aggregation not in {"max", "top_k_mean"}:
            raise ValueError("invalid voice aggregation")
        if (
            self.aggregation == "top_k_mean" and (type(self.top_k) is not int or self.top_k < 1)
        ) or (self.aggregation == "max" and self.top_k is not None):
            raise ValueError("top_k must match aggregation")
        if type(self.safe_anonymous_reuse) is not bool:
            raise ValueError("safe_anonymous_reuse must be bool")


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
    diagnostic: ScoreDiagnostic | None = None


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
    # Being a reference takes precedence over distance/similarity and over
    # mutual-exclusion reads.
    if subject.id in owners:
        return Match(owners[subject.id], tuple(dependencies))

    # A local speaker's already attributed sibling in the same audio and
    # diarization scope excludes that person.  Every field actually inspected
    # is included as a dependency so a later correction can rejudge safely.
    excluded: set[str] = set()
    if kind == "person":
        local_id = subject.payload.get("local_speaker_id")
        audio_id = subject.payload.get("audio_ref_id")
        if isinstance(local_id, str) and isinstance(audio_id, str):
            local = reader.get(local_id)
            if isinstance(local, RawRecord) and local.data_type == "local_speaker":
                upstream = local.payload.get("upstream")
                scope = upstream.get("diarization_scope_id") if isinstance(upstream, dict) else None
                dependencies.append(
                    DependencyInput(
                        VersionRef(data_type="local_speaker", id=local.id, version=local.version),
                        ("audio_ref_id", "upstream"),
                    )
                )
                if isinstance(scope, str):
                    for sibling in iter_query(
                        reader,
                        Query("local_speaker", where={"audio_ref_id": audio_id}, order="id"),
                    ):
                        if not isinstance(sibling, RawRecord) or sibling.id == local.id:
                            continue
                        sibling_upstream = sibling.payload.get("upstream")
                        sibling_scope = (
                            sibling_upstream.get("diarization_scope_id")
                            if isinstance(sibling_upstream, dict)
                            else None
                        )
                        dependencies.append(
                            DependencyInput(
                                VersionRef(
                                    data_type="local_speaker",
                                    id=sibling.id,
                                    version=sibling.version,
                                ),
                                ("audio_ref_id", "upstream"),
                            )
                        )
                        if sibling_scope != scope:
                            continue
                        for sibling_voice in iter_query(
                            reader,
                            Query("voiceprint", where={"local_speaker_id": sibling.id}, order="id"),
                        ):
                            if not isinstance(sibling_voice, RawRecord):
                                continue
                            dependencies.append(
                                DependencyInput(
                                    VersionRef(
                                        data_type="voiceprint",
                                        id=sibling_voice.id,
                                        version=sibling_voice.version,
                                    ),
                                    ("local_speaker_id", "audio_ref_id"),
                                )
                            )
                            for attribution in iter_query(
                                reader,
                                Query(
                                    "attribution",
                                    where={"subject_id": sibling_voice.id},
                                    order="id",
                                ),
                            ):
                                if not isinstance(attribution, SemanticRecord):
                                    continue
                                target = attribution.payload.get("target_id")
                                if isinstance(target, str):
                                    excluded.add(target)
                                    dependencies.append(
                                        DependencyInput(
                                            VersionRef(
                                                data_type="attribution",
                                                id=attribution.id,
                                                version=attribution.version,
                                            ),
                                            ("subject_id", "target_id"),
                                        )
                                    )

    if excluded:
        candidates = [(item, refs) for item, refs in candidates if item.id not in excluded]
    if not candidates and kind == "person":
        return Match(None, tuple(dependencies))
    if kind == "person":
        assert query_vector is not None
        model, query = query_vector
        scored: list[Reference] = []
        for item, refs in candidates:
            named = bool(item.payload.get("name"))
            for raw in refs:
                ref_model, values = vector(raw)
                if ref_model == model and len(values) == len(query):
                    scored.append(Reference(item.id, raw.id, values, named))
        ranked = rank_identity_matches(
            query,
            scored,
            aggregation=cast(Literal["max", "top_k_mean"], config.aggregation),
            top_k=config.top_k,
            support_bonus=0.0,  # production stays raw until offline calibration
            support_min_score=1.0,  # bonus is disabled; this is an explicit sentinel
            score_mode="raw",  # normalized/support bonus are offline-only
        )
        diagnostic = decide(
            ranked,
            named_threshold=config.named_threshold,
            named_margin=config.named_margin,
            unnamed_threshold=config.cosine_threshold,
            unnamed_margin=config.unnamed_margin,
            safe_anonymous_reuse=config.safe_anonymous_reuse,
            score_mode_used="raw",
            score_mode_reason="production_matching_uses_raw_only",
        )
        return Match(
            diagnostic.best.identity_id if diagnostic.accepted and diagnostic.best else None,
            tuple(dependencies),
            diagnostic,
        )
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
