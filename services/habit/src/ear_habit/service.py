"""Explicit application facade for the first layered person-memory slice."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from ear_storage_client import Context, StorageClient, TimeRange, WriteResult, write_with_retry

from ear_habit.address_hint import AddressHint, address_hints
from ear_habit.conditional import ConditionalProfile, conditional_profile
from ear_habit.episodes import episode_ops
from ear_habit.patterns import ExpressionCandidate, expression_candidates
from ear_habit.rebuild import rebuild_day_ops
from ear_habit.recognition_baseline import RecognitionCandidate, confirmed_candidates
from ear_habit.recognition_context import RecognitionContext, recognition_context
from ear_habit.retrieval import EpisodeHit, Mode, search_episodes
from ear_habit.social import ActivityObservation, Cooccurrence, activity_observation, social_window
from ear_habit.store import MODULE, rollup_ops, scope_key


class PersonMemoryService:
    """No subscription, scheduler, history scan, model call, or attribution write.

    A single writer must serialize all day and rollup writes for this module.
    """

    def __init__(self, client: StorageClient, *, since: datetime | None = None) -> None:
        if client.module != MODULE:
            raise ValueError("person memory requires the habit storage identity")
        self.client = client
        scope_key(since)
        self.since = since

    def rebuild_day(self, period: str, context: Context) -> WriteResult:
        """Caller must serialize all writes/rebuilds for the same UTC day."""
        return write_with_retry(
            self.client, lambda r: rebuild_day_ops(r, period, since=self.since), context
        ).check()

    def rebuild_rollup(
        self, person_id: str, scale: Literal["week", "month"], period: str, context: Context
    ) -> WriteResult:
        return write_with_retry(
            self.client,
            lambda r: rollup_ops(r, person_id, scale, period, since=self.since),
            context,
        ).check()

    def activity(self, activity_id: str) -> ActivityObservation:
        with self.client.snapshot() as reader:
            return activity_observation(reader, activity_id)

    def rebuild_episode(self, activity_id: str, context: Context) -> WriteResult:
        return write_with_retry(
            self.client, lambda r: episode_ops(r, activity_id, since=self.since), context
        ).check()

    def search_episodes(
        self,
        query: str,
        *,
        mode: Mode = "hybrid",
        time: TimeRange | None = None,
        as_of: datetime | None = None,
        limit: int = 10,
        include_stale: bool = False,
    ) -> tuple[EpisodeHit, ...]:
        with self.client.snapshot() as reader:
            return search_episodes(
                reader,
                query,
                mode=mode,
                time=time,
                as_of=as_of,
                limit=limit,
                include_stale=include_stale,
            )

    def social_window(self, start: datetime, end: datetime) -> tuple[Cooccurrence, ...]:
        with self.client.snapshot() as reader:
            return social_window(reader, start, end)

    def conditional_profile(
        self,
        person_id: str,
        copresent_person_id: str,
        start: datetime,
        end: datetime,
        *,
        activity_ids: frozenset[str] | None = None,
    ) -> ConditionalProfile:
        with self.client.snapshot() as reader:
            return conditional_profile(
                reader, person_id, copresent_person_id, start, end, activity_ids=activity_ids
            )

    def expression_candidates(
        self,
        person_id: str,
        start: datetime,
        end: datetime,
        *,
        min_days: int,
        min_occurrences: int,
    ) -> tuple[ExpressionCandidate, ...]:
        with self.client.snapshot() as reader:
            return expression_candidates(
                reader, person_id, start, end, min_days=min_days, min_occurrences=min_occurrences
            )

    def recognition_context(
        self,
        target_id: str,
        *,
        lookback: timedelta,
        limit: int = 100,
        as_of: datetime | None = None,
    ) -> RecognitionContext:
        with self.client.snapshot() as reader:
            return recognition_context(
                reader, target_id, lookback=lookback, limit=limit, as_of=as_of
            )

    def address_hints(
        self,
        target_id: str,
        *,
        aliases: Mapping[str, str],
        as_of: datetime,
        max_gap: timedelta = timedelta(seconds=10),
    ) -> tuple[AddressHint, ...]:
        with self.client.snapshot() as reader:
            return address_hints(reader, target_id, aliases=aliases, as_of=as_of, max_gap=max_gap)

    def confirmed_candidates(
        self,
        target_id: str,
        *,
        as_of: datetime,
        lookback: timedelta,
        min_shared_terms: int = 2,
        limit: int = 10,
    ) -> tuple[RecognitionCandidate, ...]:
        with self.client.snapshot() as reader:
            return confirmed_candidates(
                reader,
                target_id,
                as_of=as_of,
                lookback=lookback,
                min_shared_terms=min_shared_terms,
                limit=limit,
            )
