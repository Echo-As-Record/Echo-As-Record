"""Single-writer event consumer, with durable identical-request remote replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from ear_attribution.transactions import CommitJournal, Transactions
from ear_contracts import (
    DataChanged,
    Event,
    RawRecord,
    ReevaluationRequested,
    ReevaluationResult,
    SemanticRecord,
)
from ear_storage_client import (
    Context,
    FinishReevaluation,
    MarkProcessed,
    Query,
    Reader,
    StorageClient,
    Supersede,
    TimeRange,
    Update,
    WriteOp,
    iter_query,
)

from ear_habit.aggregate import Scale, parent_period, period_of
from ear_habit.episodes import episode_ops, find_episode
from ear_habit.rebuild import rebuild_day_ops
from ear_habit.store import BUCKET_TYPE, EPISODE_TYPE, MODULE, check_scope, rollup_ops, scope_key

MAX_OPS = 1800


class PersonMemoryWorker:
    """SDK consumer. The same persisted activation time and journal must be reused.

    One worker for this plugin/deployment is required. Historical backfill remains
    explicit; automatic computation includes only raw segments ingested since the
    supplied activation time. An incompatible existing bucket scope fails closed.
    """

    def __init__(
        self,
        client: StorageClient,
        *,
        active_since: datetime,
        journal: CommitJournal | None = None,
        attempts: int = 5,
    ) -> None:
        if client.module != MODULE or attempts < 1:
            raise ValueError("invalid worker identity or attempts")
        scope_key(active_since)
        self.client, self.since, self.attempts = client, active_since, attempts
        self.transactions = Transactions(client, journal)

    def subscribe(self) -> None:
        self.transactions.recover_pending()
        self.client.subscribe(MODULE, self.handle, from_beginning=False)

    def handle(self, event: Event) -> None:
        if isinstance(event, DataChanged):
            if event.data_type not in {
                "transcript_segment",
                "attribution",
                "activity",
                BUCKET_TYPE,
            }:
                return
            # A request owned by an upstream module cannot authorize plugin writes.
            context = Context(event.root_feedback_id, event.depth, None, event.additional_causes)
        elif event.target_type in {BUCKET_TYPE, EPISODE_TYPE}:
            context = Context.of(event)
        else:
            return

        def plan(reader: Reader) -> list[WriteOp]:
            if reader.is_processed(MODULE, event.event_id):
                return []
            if isinstance(event, DataChanged):
                ops = [] if event.occurred_at < self.since else self._data_changed(reader, event)
            else:
                ops = self._reevaluate(reader, event)
            if len(ops) + 1 > MAX_OPS:
                raise ValueError("memory event exceeds atomic batch limit")
            return [*ops, MarkProcessed(MODULE, event.event_id)]

        result = self.transactions.run(plan, context, event.event_id, self.attempts)
        if result.conflict is not None and result.conflict.reason != "already_processed":
            result.check()

    def _data_changed(self, reader: Reader, event: DataChanged) -> list[WriteOp]:
        record = reader.get(event.data_id)
        if record is None or record.data_type != event.data_type:
            raise ValueError("changed record unavailable")
        if event.data_type == BUCKET_TYPE:
            if not isinstance(record, SemanticRecord) or record.superseded:
                return []
            if record.payload.get("scale") != "day":
                return []
            check_scope(record, scope_key(self.since))
            person, period = record.payload.get("person_id"), record.payload.get("period")
            if not isinstance(person, str) or not isinstance(period, str):
                raise ValueError("invalid day bucket")
            ops: list[WriteOp] = []
            for scale in ("week", "month"):
                ops.extend(
                    rollup_ops(
                        reader,
                        person,
                        cast(Scale, scale),
                        parent_period(period, cast(Scale, scale)),
                        since=self.since,
                    )
                )
            return ops
        if event.data_type == "activity":
            if not isinstance(record, SemanticRecord):
                raise ValueError("invalid activity")
            if record.superseded:
                existing = find_episode(reader, record.id)
                if existing is None:
                    return []
                check_scope(existing, scope_key(self.since))
                return [Supersede(EPISODE_TYPE, existing.id, existing.version)]
            return episode_ops(reader, record.id, since=self.since)
        segments: list[RawRecord] = []
        if event.data_type == "transcript_segment":
            if not isinstance(record, RawRecord):
                raise ValueError("invalid transcript")
            segments = [record]
        elif isinstance(record, SemanticRecord):
            if record.payload.get("target_type") != "person":
                return []
            subject = record.payload.get("subject_id")
            if not isinstance(subject, str):
                raise ValueError("invalid attribution subject")
            for item in iter_query(
                reader, Query("transcript_segment", where={"voiceprint_id": subject})
            ):
                if not isinstance(item, RawRecord):
                    raise ValueError("invalid transcript")
                segments.append(item)
        else:
            raise ValueError("invalid attribution")
        segments = [s for s in segments if s.ingested_at >= self.since]
        days = sorted({period_of(s.event_start, "day") for s in segments})
        activities: set[str] = set()
        for segment in segments:
            end = max(segment.event_end, segment.event_start + timedelta(microseconds=1))
            for activity in iter_query(
                reader, Query("activity", time=TimeRange(segment.event_start, end))
            ):
                if not isinstance(activity, SemanticRecord):
                    raise ValueError("invalid activity")
                activities.add(activity.id)
        ops = []
        for day in days:
            ops.extend(rebuild_day_ops(reader, day, since=self.since))
        for activity_id in sorted(activities):
            ops.extend(episode_ops(reader, activity_id, since=self.since))
        return ops

    def _reevaluate(self, reader: Reader, event: ReevaluationRequested) -> list[WriteOp]:
        record = reader.get(event.target_id)
        if not isinstance(record, SemanticRecord) or record.data_type != event.target_type:
            raise ValueError("memory target unavailable")
        if record.superseded:
            return [
                self._finish(
                    event,
                    record,
                    "not_applicable",
                    record.version,
                    reason="target_terminated",
                    disposition="terminated",
                )
            ]
        check_scope(record, scope_key(self.since))
        if record.data_type == EPISODE_TYPE:
            activity_id = record.payload.get("activity_id")
            if not isinstance(activity_id, str):
                raise ValueError("invalid episode")
            activity = reader.get(activity_id)
            if not isinstance(activity, SemanticRecord) or activity.data_type != "activity":
                raise ValueError("episode source unavailable")
            if activity.superseded:
                return [
                    Supersede(EPISODE_TYPE, record.id, record.version),
                    self._finish(
                        event,
                        record,
                        "not_applicable",
                        record.version + 1,
                        reason="source_terminated",
                        disposition="terminated",
                    ),
                ]
            ops = episode_ops(reader, activity_id, since=self.since)
        else:
            scale, period, person = (
                record.payload.get(k) for k in ("scale", "period", "person_id")
            )
            if (
                scale not in {"day", "week", "month"}
                or not isinstance(period, str)
                or not isinstance(person, str)
            ):
                raise ValueError("invalid bucket")
            ops = (
                rebuild_day_ops(reader, period, since=self.since)
                if scale == "day"
                else rollup_ops(reader, person, cast(Scale, scale), period, since=self.since)
            )
            if not ops:
                return [
                    Supersede(BUCKET_TYPE, record.id, record.version),
                    self._finish(
                        event,
                        record,
                        "not_applicable",
                        record.version + 1,
                        reason="source_unavailable",
                        disposition="terminated",
                    ),
                ]
        update = next((x for x in ops if isinstance(x, Update) and x.id == record.id), None)
        if update is None:
            raise ValueError("target not included in recomputation")
        changed = (
            dict(update.payload) != record.payload
            or update.event_start != record.event_start
            or update.event_end != record.event_end
        )
        return [
            *ops,
            self._finish(
                event, record, "changed" if changed else "unchanged", record.version + int(changed)
            ),
        ]

    @staticmethod
    def _finish(
        event: ReevaluationRequested,
        record: SemanticRecord,
        outcome: Literal["changed", "unchanged", "not_applicable"],
        after: int,
        *,
        reason: str | None = None,
        disposition: Literal["terminated"] | None = None,
    ) -> FinishReevaluation:
        return FinishReevaluation(
            ReevaluationResult(
                request_event_id=event.event_id,
                target_type=event.target_type,
                target_id=event.target_id,
                outcome=outcome,
                before_version=record.version,
                after_version=after,
                root_feedback_id=event.root_feedback_id,
                depth=event.depth,
                additional_causes=event.additional_causes,
                completed_at=max(datetime.now(UTC), event.occurred_at),
                reason_code=reason,
                disposition=disposition,
            )
        )
