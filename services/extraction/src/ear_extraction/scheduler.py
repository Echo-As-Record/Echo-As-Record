"""Half-open interval subtraction. Successful ranges alone count as coverage."""

from ear_extraction.domain import Interval


def uncovered(interval: Interval, covered: list[Interval]) -> list[Interval]:
    cursor = interval.start
    result = []
    for item in sorted(covered, key=lambda x: x.start):
        if item.end <= cursor or item.start >= interval.end:
            continue
        if item.start > cursor:
            result.append(Interval(start=cursor, end=item.start))
        cursor = max(cursor, min(item.end, interval.end))
    if cursor < interval.end:
        result.append(Interval(start=cursor, end=interval.end))
    return result
