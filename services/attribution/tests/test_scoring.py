from collections.abc import Sequence
from typing import Any, Literal

import pytest
from ear_attribution import MatchConfig
from ear_attribution.scoring import (
    IdentityScore,
    Reference,
    decide,
    rank_identity_matches,
    score_mode_status,
)


def ranked(
    *refs: Reference,
    aggregation: Literal["max", "top_k_mean"] = "max",
    top_k: int | None = None,
    support_bonus: float = 0.0,
    support_min_score: float = 1.0,
    score_mode: Literal["raw", "normalized"] = "raw",
):
    return rank_identity_matches(
        (1.0, 0.0),
        list(refs),
        aggregation=aggregation,
        top_k=top_k,
        support_bonus=support_bonus,
        support_min_score=support_min_score,
        score_mode=score_mode,
    )


def decision(
    values: Sequence[IdentityScore],
    *,
    safe: bool = True,
    named_threshold: float = 0.7,
    named_margin: float = 0.2,
    unnamed_threshold: float = 0.7,
    unnamed_margin: float = 0.2,
    mode: Literal["raw", "normalized"] = "raw",
    reason: str | None = None,
):
    return decide(
        values,
        named_threshold=named_threshold,
        named_margin=named_margin,
        unnamed_threshold=unnamed_threshold,
        unnamed_margin=unnamed_margin,
        safe_anonymous_reuse=safe,
        score_mode_used=mode,
        score_mode_reason=reason,
    )


def test_references_are_aggregated_by_identity_and_top_k_mean():
    values = ranked(
        Reference("a", "a1", (0.99, 0.1)),
        Reference("a", "a2", (0.8, 0.6)),
        Reference("b", "b1", (0.95, 0.31)),
    )
    assert [item.identity_id for item in values] == ["a", "b"]
    assert values[0].supporting_references == 2
    mean = ranked(
        Reference("a", "a1", (1.0, 0.0)),
        Reference("a", "a2", (0.8, 0.6)),
        aggregation="top_k_mean",
        top_k=2,
    )
    assert mean[0].raw_score == pytest.approx(0.9)


def test_margin_policy_safe_reuse_can_be_disabled():
    values = ranked(
        Reference("anonymous", "a", (1.0, 0.0)), Reference("named", "b", (0.99, 0.1), True)
    )
    assert decision(values).accepted
    refused = decision(values, safe=False)
    assert not refused.accepted and refused.reason == "unnamed_margin_insufficient"


def test_named_margin_refuses_and_single_candidate_needs_no_margin():
    values = ranked(
        Reference("named", "b", (1.0, 0.0), True), Reference("other", "c", (0.99, 0.1), True)
    )
    refused = decision(values, named_threshold=0.7, named_margin=0.2)
    assert not refused.accepted and refused.reason == "named_margin_insufficient"
    only = ranked(Reference("named", "b", (1.0, 0.0), True))
    assert decision(only, named_margin=1.0).accepted


def test_normalized_formula_and_whole_ranking_raw_fallback():
    refs = [
        Reference("a", "a1", (1.0, 0.0)),
        Reference("a", "a2", (0.8, 0.6)),
        Reference("b", "b1", (0.0, 1.0)),
        Reference("b", "b2", (0.1, 0.995)),
    ]
    values = ranked(*refs, aggregation="top_k_mean", top_k=2, score_mode="normalized")
    assert values[0].score != values[0].raw_score
    fallback = ranked(*refs[:3], aggregation="top_k_mean", top_k=2, score_mode="normalized")
    assert [v.score for v in fallback] == [
        v.raw_score for v in ranked(*refs[:3], aggregation="top_k_mean", top_k=2)
    ]
    assert score_mode_status({"a": refs[:2], "b": refs[2:]}, "normalized") == ("normalized", None)
    assert score_mode_status({"a": refs[:2]}, "normalized") == ("raw", "no_other_candidate")


def test_support_bonus_counts_only_references_over_explicit_floor():
    values = ranked(
        Reference("a", "a1", (1.0, 0.0)),
        Reference("a", "a2", (0.8, 0.6)),
        support_bonus=0.1,
        support_min_score=0.9,
    )
    assert values[0].score == pytest.approx(1.0)
    values = ranked(
        Reference("a", "a1", (0.95, 0.312249)),
        Reference("a", "a2", (0.8, 0.6)),
        support_bonus=0.1,
        support_min_score=0.99,
    )
    assert values[0].score == pytest.approx(values[0].raw_score)


def test_match_config_validates_explicit_options():
    MatchConfig(0.7, 100, 0.1, 0.8, 0.2, "max", None, True)
    MatchConfig(0.7, 100, 0.1, 0.8, 0.2, "top_k_mean", 2, True)
    with pytest.raises(ValueError):
        MatchConfig(0.7, 100, float("nan"), 0.8, 0.2, "max", None, True)
    with pytest.raises(ValueError):
        MatchConfig(0.7, 100, 0.1, 0.8, 0.2, "max", 2, True)


@pytest.mark.parametrize(
    "field",
    [
        "unnamed_margin",
        "named_threshold",
        "named_margin",
        "aggregation",
        "top_k",
        "safe_anonymous_reuse",
    ],
)
def test_match_config_rejects_partial_explicit_options(field):
    invalid: dict[str, Any] = {field: None}
    with pytest.raises(ValueError, match="all be explicitly supplied"):
        MatchConfig(0.7, 100, **invalid)


@pytest.mark.parametrize(
    "field",
    [
        "cosine_threshold",
        "gps_threshold_m",
        "unnamed_margin",
        "named_threshold",
        "named_margin",
        "aggregation",
        "safe_anonymous_reuse",
    ],
)
def test_match_config_rejects_null_even_when_all_options_supplied(field):
    options: dict[str, Any] = dict(
        cosine_threshold=0.7,
        gps_threshold_m=100,
        unnamed_margin=0.1,
        named_threshold=0.8,
        named_margin=0.2,
        aggregation="max",
        top_k=None,
        safe_anonymous_reuse=True,
    )
    options[field] = None
    with pytest.raises(ValueError):
        MatchConfig(**options)
