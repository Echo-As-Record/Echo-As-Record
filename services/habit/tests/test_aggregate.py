"""L2 纯计算：周期归属、周期区间、上层桶由下层合并。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from ear_habit.aggregate import (
    bucket_key,
    parent_period,
    period_of,
    period_range,
    rollup,
)
from ear_habit.buckets import Utterance, stats_of


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_period_of_each_scale() -> None:
    moment = at("2026-10-29T06:30:00")  # 周四
    assert period_of(moment, "day") == "2026-10-29"
    assert period_of(moment, "week") == "2026-10-26"
    assert period_of(moment, "month") == "2026-10"


def test_week_starts_on_monday_and_sunday_belongs_to_same_week() -> None:
    assert period_of(at("2026-10-26T00:00:00"), "week") == "2026-10-26"
    assert period_of(at("2026-11-01T23:59:59"), "week") == "2026-10-26"
    assert period_of(at("2026-11-02T00:00:00"), "week") == "2026-11-02"


def test_week_can_span_months() -> None:
    assert parent_period("2026-11-01", "week") == "2026-10-26"
    assert parent_period("2026-11-01", "month") == "2026-11"


def test_non_utc_is_rejected() -> None:
    local = datetime(2026, 10, 29, 6, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(ValueError):
        period_of(local, "day")


def test_period_range_is_half_open_and_contiguous() -> None:
    start, end = period_range("2026-10-26", "week")
    assert (end - start) == timedelta(days=7)
    assert period_range("2026-10-29", "day") == (
        at("2026-10-29T00:00:00"),
        at("2026-10-30T00:00:00"),
    )
    assert period_range("2026-12", "month") == (
        at("2026-12-01T00:00:00"),
        at("2027-01-01T00:00:00"),
    )


def test_bucket_key_is_single_string() -> None:
    assert bucket_key("p1", "day", "2026-10-29") == "p1:day:2026-10-29"


def test_period_range_rejects_noncanonical_keys() -> None:
    for period, scale in [("20261029", "day"), ("2026-10-29", "week"), ("2026-1", "month")]:
        with pytest.raises(ValueError):
            period_range(period, scale)  # type: ignore[arg-type]


def test_rollup_equals_bulk_statistics() -> None:
    mon = [Utterance("我觉得就是这样", 2000)]
    tue = [Utterance("然后 hello hello", 1000)]
    assert rollup([stats_of(mon), stats_of(tue)]) == stats_of(mon + tue)
    assert rollup([]) == stats_of([])


def test_rollup_preserves_tokenizer_and_rejects_mixed_versions() -> None:
    from ear_habit.tokenize import CharTokenizer

    chars = stats_of([Utterance("我觉得这样")], CharTokenizer())
    assert rollup([chars])["tokenizer"] == CharTokenizer.identifier
    with pytest.raises(ValueError, match="incompatible"):
        rollup([chars, stats_of([Utterance("我觉得这样")])])
    old = dict(chars, schema=1)
    with pytest.raises(ValueError, match="incompatible"):
        rollup([old])
