"""Post-assembly admission: never change an activity's boundaries or citations."""

from collections.abc import Mapping
from datetime import timedelta

from ear_contracts import RawRecord

from ear_extraction.domain import Activity

DEFAULT_ACTIVITY_MIN_DURATION_MINUTES = 10
MAX_ACTIVITY_MIN_DURATION_MINUTES = 1440


def validate_activity_min_duration(minutes: int) -> None:
    if type(minutes) is not int or not 1 <= minutes <= MAX_ACTIVITY_MIN_DURATION_MINUTES:
        raise ValueError("invalid_activity_min_duration_minutes")


def meets_activity_floor(
    activity: Activity, raw_by_id: Mapping[str, RawRecord], minimum: timedelta
) -> bool:
    """Measure the span of the cited evidence within the final activity interval.

    Legacy output may extend beyond its evidence, and a cited record may itself
    extend beyond the activity. Neither extension earns duration. A point has
    zero duration; the synthetic interval tick used in session assembly does not count.
    """
    evidence = [raw_by_id[id] for id in activity.evidence_ids]
    start = max(activity.start, min(row.event_start for row in evidence))
    end = min(activity.end, max(row.event_end for row in evidence))
    return end - start >= minimum


def duration_audit(withheld: list[Activity]) -> dict[str, int]:
    if not withheld:
        return {}
    return {
        "below_min_duration_candidates": len(withheld),
        "below_min_duration_rows": len(
            {id for activity in withheld for id in activity.evidence_ids}
        ),
    }
