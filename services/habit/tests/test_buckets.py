"""L1 桶统计：可加可减、口头禅、语速、排序稳定。"""

from __future__ import annotations

import pytest
from ear_habit.buckets import (
    Utterance,
    empty_stats,
    merge,
    speech_rate,
    stats_of,
    subtract,
    top,
    utterance_stats,
)

A = [Utterance("我觉得就是这样 对吧", 3000), Utterance("hello world hello", 1500)]
B = [Utterance("然后我们走吧 嗯", 2000)]


def test_merge_then_subtract_roundtrip() -> None:
    a, b = stats_of(A), stats_of(B)
    assert subtract(merge(a, b), b) == a


def test_merge_is_commutative_and_matches_bulk() -> None:
    a, b = stats_of(A), stats_of(B)
    assert merge(a, b) == merge(b, a) == stats_of(A + B)


def test_empty_is_identity() -> None:
    a = stats_of(A)
    assert merge(a, empty_stats()) == a
    assert subtract(a, empty_stats()) == a


def test_subtract_more_than_present_raises() -> None:
    with pytest.raises(ValueError):
        subtract(stats_of(B), stats_of(A))


def test_zero_counts_are_dropped() -> None:
    a = stats_of(A)
    assert subtract(a, a) == empty_stats()


def test_fillers_counted_by_substring() -> None:
    stats = utterance_stats(Utterance("就是 就是 嗯"))
    assert stats["fillers"] == {"就是": 2, "嗯": 1}


def test_latin_words_are_lowercased_tokens() -> None:
    stats = utterance_stats(Utterance("Hello hello"))
    assert stats["unigrams"] == {"hello": 2}
    assert stats["bigrams"] == {"hello hello": 1}


def test_speech_rate() -> None:
    assert speech_rate(stats_of([Utterance("abcdefghij", 2000)])) == 5.0
    assert speech_rate(stats_of([Utterance("abc", 0)])) is None


def test_top_is_stable_for_ties() -> None:
    stats = stats_of([Utterance("b a c a b")])
    assert top(stats, "unigrams", 3) == [("a", 2), ("b", 2), ("c", 1)]
