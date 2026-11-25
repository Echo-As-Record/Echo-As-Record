"""L2：多尺度聚合的纯计算。周桶、月桶只由下层桶合并而成，不重新扫描原文。

桶键是单字段（登记表的 unique_fields 不支持复合键）：<person_id>:<scale>:<period>。
时间一律 UTC；周从周一开始。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from ear_habit.buckets import Stats, empty_stats, merge

Scale = Literal["day", "week", "month"]


def _utc_date(moment: datetime) -> date:
    if moment.utcoffset() != timedelta(0):
        raise ValueError("时间必须是 UTC")
    return moment.astimezone(UTC).date()


def period_of(moment: datetime, scale: Scale) -> str:
    """这个时刻所属的桶周期：日=YYYY-MM-DD，周=周一的日期，月=YYYY-MM。"""
    day = _utc_date(moment)
    if scale == "day":
        return day.isoformat()
    if scale == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if scale == "month":
        return f"{day.year:04d}-{day.month:02d}"
    raise ValueError("unknown scale")


def parent_period(day_period: str, scale: Scale) -> str:
    """一个日周期所属的周或月周期。"""
    day = date.fromisoformat(day_period)
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return period_of(moment, scale)


def period_range(period: str, scale: Scale) -> tuple[datetime, datetime]:
    """周期对应的半开时间区间 [start, end)，用作桶的 event_start / event_end。"""
    if scale == "month":
        if len(period) != 7 or period[4] != "-":
            raise ValueError("month must be YYYY-MM")
        year, month = (int(part) for part in period.split("-"))
        start = datetime(year, month, 1, tzinfo=UTC)
        if period != f"{year:04d}-{month:02d}":
            raise ValueError("month must be YYYY-MM")
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
        return start, end
    if scale not in ("day", "week"):
        raise ValueError("unknown scale")
    first = date.fromisoformat(period)
    if first.isoformat() != period:
        raise ValueError("day must be YYYY-MM-DD")
    if scale == "week" and first.weekday() != 0:
        raise ValueError("week must start on Monday")
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    return start, start + timedelta(days=1 if scale == "day" else 7)


def bucket_key(person_id: str, scale: Scale, period: str) -> str:
    return f"{person_id}:{scale}:{period}"


def rollup(lower: Iterable[Stats]) -> Stats:
    """把下层桶的统计合并成上层桶。"""
    items = iter(lower)
    first = next(items, None)
    if first is None:
        return empty_stats()
    tokenizer = first.get("tokenizer")
    if not isinstance(tokenizer, str):
        raise ValueError("incompatible statistics schema or tokenizer")
    total = merge(empty_stats(tokenizer), first)
    for stats in items:
        total = merge(total, stats)
    return total
