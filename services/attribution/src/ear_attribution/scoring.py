"""Pure voiceprint scoring; this module deliberately has no storage imports."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


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
    score_mode_used: Literal["raw", "normalized"]
    score_mode_reason: str | None


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
    return ((raw - _mean(own)) / (_std(own) + 1e-6)) - ((raw - _mean(other)) / (_std(other) + 1e-6))


def rank_identity_matches(
    query: Sequence[float],
    references: Sequence[Reference],
    *,
    aggregation: Literal["max", "top_k_mean"],
    top_k: int | None,
    support_bonus: float,
    support_min_score: float,
    score_mode: Literal["raw", "normalized"],
) -> tuple[IdentityScore, ...]:
    """Score references and aggregate them by identity, never by template."""
    if aggregation not in {"max", "top_k_mean"} or score_mode not in {"raw", "normalized"}:
        raise ValueError("invalid scoring mode")
    if aggregation == "top_k_mean" and (type(top_k) is not int or top_k < 1):
        raise ValueError("top_k must be a positive integer for top_k_mean")
    if aggregation == "max" and top_k is not None:
        raise ValueError("top_k is only valid for top_k_mean")
    if (
        type(support_bonus) not in (int, float)
        or not math.isfinite(support_bonus)
        or support_bonus < 0
    ):
        raise ValueError("support_bonus must be finite and nonnegative")
    if (
        type(support_min_score) not in (int, float)
        or not math.isfinite(support_min_score)
        or not -1 <= support_min_score <= 1
    ):
        raise ValueError("support_min_score must be finite in [-1, 1]")
    grouped: dict[str, list[tuple[Reference, float]]] = {}
    for reference in references:
        score = cosine(query, reference.vector)
        grouped.setdefault(reference.identity_id, []).append((reference, score))
    effective_mode, _ = score_mode_status(grouped, score_mode)
    result: list[IdentityScore] = []
    for identity_id, values in grouped.items():
        values.sort(key=lambda item: (-item[1], item[0].reference_id))
        selected = values if aggregation == "max" else values[:top_k]
        raw = selected[0][1] if aggregation == "max" else _mean([score for _, score in selected])
        support_count = sum(score >= support_min_score for _, score in values)
        adjusted_raw = min(1.0, raw + support_bonus * max(0, support_count - 1))
        own = [score for _, score in values]
        other = [
            score
            for other_id, scores in grouped.items()
            if other_id != identity_id
            for _, score in scores
        ]
        score = (
            _normalized(adjusted_raw, own, other)
            if effective_mode == "normalized"
            else adjusted_raw
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


def score_mode_status(
    grouped: Mapping[str, Sequence[object]], score_mode: Literal["raw", "normalized"]
) -> tuple[Literal["raw", "normalized"], str | None]:
    """Return one mode for the whole ranking, never a per-identity mixture."""
    if score_mode == "raw":
        return "raw", None
    if len(grouped) < 2:
        return "raw", "no_other_candidate"
    if any(len(values) < 2 for values in grouped.values()):
        return "raw", "candidate_has_fewer_than_two_references"
    return "normalized", None


def decide(
    ranked: Sequence[IdentityScore],
    *,
    named_threshold: float,
    named_margin: float,
    unnamed_threshold: float,
    unnamed_margin: float,
    safe_anonymous_reuse: bool,
    score_mode_used: Literal["raw", "normalized"],
    score_mode_reason: str | None,
) -> ScoreDiagnostic:
    if not ranked:
        return ScoreDiagnostic(
            (), None, None, None, False, "no_candidates", score_mode_used, score_mode_reason
        )
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = None if second is None else best.score - second.score
    threshold = named_threshold if best.named else unnamed_threshold
    minimum_margin = named_margin if best.named else unnamed_margin
    over = best.score >= threshold
    margin_ok = margin is None or margin >= minimum_margin
    if over and (margin_ok or (safe_anonymous_reuse and not best.named)):
        return ScoreDiagnostic(
            tuple(ranked),
            best,
            second,
            margin,
            True,
            "accepted",
            score_mode_used,
            score_mode_reason,
        )
    if over and not margin_ok and best.named:
        return ScoreDiagnostic(
            tuple(ranked),
            best,
            second,
            margin,
            False,
            "named_margin_insufficient",
            score_mode_used,
            score_mode_reason,
        )
    if over and not margin_ok:
        return ScoreDiagnostic(
            tuple(ranked),
            best,
            second,
            margin,
            False,
            "unnamed_margin_insufficient",
            score_mode_used,
            score_mode_reason,
        )
    return ScoreDiagnostic(
        tuple(ranked),
        best,
        second,
        margin,
        False,
        "below_threshold",
        score_mode_used,
        score_mode_reason,
    )
