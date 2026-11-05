"""Internal periodic trigger; no additional user-facing API and no event-bus command shortcut."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event

from ear_extraction.domain import Interval
from ear_extraction.orchestration import ExtractionService


@dataclass(frozen=True)
class Schedule:
    start: datetime
    every: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        Interval(start=self.start, end=self.start + self.every)


def tick(service: ExtractionService, schedule: Schedule | None = None) -> None:
    """Schedule completed periods, subtracting successful and admitted coverage."""
    if schedule and service.clock() > schedule.start:
        periods = (service.clock() - schedule.start) // schedule.every
        if periods:
            service.schedule(
                Interval(start=schedule.start, end=schedule.start + periods * schedule.every)
            )
    service.drain()


def serve(
    service: ExtractionService,
    stop: Event,
    *,
    schedule: Schedule | None = None,
    poll_seconds: float = 1,
) -> None:
    """The host injects clients and owns shutdown. Run one worker per deployment."""
    if poll_seconds <= 0:
        raise ValueError("positive_poll_interval_required")
    service.subscribe()
    while not stop.is_set():
        tick(service, schedule)
        stop.wait(poll_seconds)
