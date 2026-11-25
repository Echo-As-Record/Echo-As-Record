import pytest

from ear_attribution import MatchConfig
from ear_attribution.scoring import Reference, decide, rank_identity_matches


def test_references_are_aggregated_by_identity():
    ranked = rank_identity_matches(
        (1.0, 0.0),
        [
            Reference("a", "a1", (0.99, 0.1)),
            Reference("a", "a2", (0.8, 0.6)),
            Reference("b", "b1", (0.95, 0.31)),
        ],
    )
    assert [item.identity_id for item in ranked] == ["a", "b"]
    assert ranked[0].supporting_references == 2


def test_margin_policy_distinguishes_named_and_anonymous():
    ranked = rank_identity_matches(
        (1.0, 0.0),
        [Reference("anonymous", "a", (1.0, 0.0)), Reference("named", "b", (0.99, 0.1), True)],
    )
    anonymous = decide(
        ranked,
        named_threshold=0.7,
        named_margin=0.2,
        unnamed_threshold=0.7,
        unnamed_margin=0.2,
    )
    assert anonymous.accepted and anonymous.reason == "accepted"
    named = decide(
        tuple(reversed(ranked)),
        named_threshold=0.7,
        named_margin=0.2,
        unnamed_threshold=0.7,
        unnamed_margin=0.2,
    )
    assert not named.accepted


def test_normalized_falls_back_for_single_reference():
    raw = rank_identity_matches(
        (1.0, 0.0), [Reference("a", "a", (1.0, 0.0)), Reference("b", "b", (0.0, 1.0))]
    )
    normalized = rank_identity_matches(
        (1.0, 0.0),
        [Reference("a", "a", (1.0, 0.0)), Reference("b", "b", (0.0, 1.0))],
        score_mode="normalized",
    )
    assert [item.score for item in normalized] == [item.raw_score for item in raw]


def test_match_config_rejects_invalid_options():
    with pytest.raises(ValueError):
        MatchConfig(0.7, 100, unnamed_margin=float("nan"))
    with pytest.raises(ValueError):
        MatchConfig(0.7, 100, aggregation="bad")
