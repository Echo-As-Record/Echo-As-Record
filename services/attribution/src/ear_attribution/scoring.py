"""Pure voiceprint scoring; this module deliberately has no storage imports."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence


@dataclass(frozen=True, slots=True)
class Reference:
    """One normalized reference vector owned by an identity."""

    identity_id: str
    reference_id: str
    vector: tuple[float, ...]
    named: bool = False


@dataclass(frozen=True, slots=True)
class IdentityScore:
    identity_id: str
    named: bool
    raw_score: float
    score: float
    supporting_references: int
    reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreDiagnostic:
    ranked: tuple[IdentityScore, ...]
    best: IdentityScore | None
    second: IdentityScore | None
    margin: float | None
    accepted: bool
    reason: str


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimensions must match")
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    nl = math.sqrt(math.fsum(a * a for a in left))
    nr = math.sqrt(math.fsum(b * b for b in right))
    if not nl or not nr:
        raise ValueError("embedding must not be zero")
    return dot / (nl * nr)


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / len(values))


def _normalized(raw: float, own: Sequence[float], other: Sequence[float]) -> float:
    """Difference of genuine and impostor z scores.

    ``(raw - mean(own)) / (std(own)+1e-6) -
    (raw - mean(other)) / (std(other)+1e-6)``.  A person with fewer than two
    references cannot provide a genuine distribution and is kept on raw score.
    """
    if len(own) < 2 or not other:
        return raw
    return ((raw - _mean(own)) / (_std(own) + 1e-6)) - (
        (raw - _mean(other)) / (_std(other) + 1e-6)
    )


def rank_identity_matches(
    query: Sequence[float],
    references: Sequence[Reference],
    *,
    aggregation: Literal["max", "top_k_mean"] = "max",
    top_k: int = 2,
    support_bonus: float = 0.0,
    score_mode: Literal["raw", "normalized"] = "raw",
) -> tuple[IdentityScore, ...]:
    """Score references and aggregate them by identity, never by template."""
    if aggregation not in {"max", "top_k_mean"} or score_mode not in {"raw", "normalized"}:
        raise ValueError("invalid scoring mode")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if (
        type(support_bonus) not in (int, float)
        or not math.isfinite(support_bonus)
        or support_bonus < 0
    ):
        raise ValueError("support_bonus must be finite and nonnegative")
    grouped: dict[str, list[tuple[Reference, float]]] = {}
    for reference in references:
        score = cosine(query, reference.vector)
        grouped.setdefault(reference.identity_id, []).append((reference, score))
    result: list[IdentityScore] = []
    for identity_id, values in grouped.items():
        values.sort(key=lambda item: (-item[1], item[0].reference_id))
        selected = values[:top_k]
        raw = selected[0][1] if aggregation == "max" else _mean([score for _, score in selected])
        vectors = [item.vector for item, _ in values]
        own = [
            cosine(vectors[i], vectors[j])
            for i in range(len(vectors))
            for j in range(i + 1, len(vectors))
        ]
        other = [
            score
            for other_id, scores in grouped.items()
            if other_id != identity_id
            for _, score in scores
        ]
        score = _normalized(raw, own, other) if score_mode == "normalized" else raw
        score = (
            min(1.0, raw + support_bonus * max(0, len(values) - 1))
            if score_mode == "raw"
            else score
        )
        result.append(
            IdentityScore(
                identity_id,
                values[0][0].named,
                raw,
                score,
                len(values),
                tuple(v[0].reference_id for v in values),
            )
        )
    return tuple(sorted(result, key=lambda item: (-item.score, -item.raw_score, item.identity_id)))


def decide(
    ranked: Sequence[IdentityScore],
    *,
    named_threshold: float,
    named_margin: float,
    unnamed_threshold: float,
    unnamed_margin: float,
    safe_anonymous_reuse: bool = True,
) -> ScoreDiagnostic:
    if not ranked:
        return ScoreDiagnostic((), None, None, None, False, "no_candidates")
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = None if second is None else best.score - second.score
    threshold = named_threshold if best.named else unnamed_threshold
    minimum_margin = named_margin if best.named else unnamed_margin
    over = best.score >= threshold
    margin_ok = margin is None or margin >= minimum_margin
    if over and (margin_ok or (safe_anonymous_reuse and not best.named)):
        return ScoreDiagnostic(tuple(ranked), best, second, margin, True, "accepted")
    if over and not margin_ok and best.named:
        return ScoreDiagnostic(
            tuple(ranked), best, second, margin, False, "named_margin_insufficient"
        )
    if over and not margin_ok:
        return ScoreDiagnostic(tuple(ranked), best, second, margin, True, "safe_anonymous_reuse")
    return ScoreDiagnostic(tuple(ranked), best, second, margin, False, "below_threshold")
