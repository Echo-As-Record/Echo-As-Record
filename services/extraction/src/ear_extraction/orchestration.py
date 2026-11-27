"""Durable-in-storage job orchestration. A deployment runs one extraction worker."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import ExitStack, suppress
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import UUID

from ear_contracts import (
    Cause,
    DataChanged,
    Event,
    RawRecord,
    ReevaluationRequested,
    ReevaluationResult,
    SemanticRecord,
    SystemRecord,
    format_utc,
    is_id,
)
from ear_llm_gateway import GatewayClient
from ear_storage_client import (
    AssertTypeRevision,
    Context,
    Create,
    FinishReevaluation,
    MarkProcessed,
    PutSystemRecord,
    Query,
    Reader,
    StorageClient,
    Supersede,
    TimeRange,
    WriteOp,
    iter_query,
)
from pydantic import BaseModel, ValidationError

from ear_extraction.activity_duration import (
    DEFAULT_ACTIVITY_MIN_DURATION_MINUTES,
    validate_activity_min_duration,
)
from ear_extraction.automatic_timeline import (
    DEFAULT_WINDOW_REVIEW_CONCURRENCY,
    INVALID_WINDOW_REVIEW,
    LEGACY_WINDOW_REVIEW_PURPOSE,
    MAX_WINDOW_REVIEW_CONCURRENCY,
    AutomaticPredecessorPin,
    AutomaticTimeline,
    EmptySuccessRunPin,
    InvalidHostedWindowReview,
    LegacyWindowReviewRunPin,
    prewrite_run_metadata_sha256,
)
from ear_extraction.automatic_timeline import (
    PURPOSE as WINDOW_REVIEW_PURPOSE,
)
from ear_extraction.call_safety import DurableCalls, TranscriptCallLimitExceeded
from ear_extraction.course_reextract import (
    MAX_COURSE_DRAFT_SPANS,
    MAX_COURSE_REEXTRACT_PREDECESSORS,
    MAX_SCHEMA_DISCARD_ATTESTATIONS,
    CourseDraftGuard,
    CourseDraftSpan,
    ReviewedTimelineDraft,
    SchemaDiscardAttestation,
    ScopedTranscriptGuard,
    course_replacement_targets,
    is_sha256,
    validate_timeline_windows,
    within_capture_coverage,
)
from ear_extraction.domain import (
    Blocked,
    Call,
    Command,
    Interval,
    SessionCoverageBlocked,
    Waiting,
    now_utc,
)
from ear_extraction.model_io import ModelGateway, build_call
from ear_extraction.planning import (
    apply_plan,
    extract_plan,
    preview_reextract,
    rejudge_plan,
)
from ear_extraction.reevaluation import discover_rules
from ear_extraction.reextract_preflight import ExactInterval, PreflightInput
from ear_extraction.rules import rule_operations
from ear_extraction.scheduler import uncovered
from ear_extraction.selection import reselect_plan, validate_summary_rule_config
from ear_extraction.sessions import validate_session_windows
from ear_extraction.suggestions import suggestion_operations
from ear_extraction.timeline_preview import HostedWindowReview

logger = logging.getLogger(__name__)
CONSUMER = "extraction.jobs"
COMPLETION_CONSUMER = "extraction.completion"
DEFAULT_TRANSCRIPT_MAX_CALLS = 5
COURSE_MAX_CALLS_PER_WINDOW = 5  # At most 120 across the validated 24-window allowlist.
TRANSCRIPT_LEASE_OVERHEAD_SECONDS = 250
# Match the existing hosted gateway's default ingress body limit, in UTF-8 bytes.
COURSE_MAX_REQUEST_BYTES = 256 * 1024
MODEL_COMMANDS = {"extract", "reextract", "apply_rule", "extract_once", "reevaluate"}
TRANSCRIPT_ONLY_COMMANDS = {"extract", "reextract", "rule_create", "rule_update", "rule_delete"}
TERMINAL = {"succeeded", "failed", "blocked", "superseded"}
_LEGACY_WINDOW_TRIAL = "automatic_timeline_v1_predecessor_run_id"


def schedule_id(interval: Interval) -> str:
    """Stable UUIDv7 using interval start time and a digest, for schedule admission dedup."""
    millis = int(interval.start.timestamp() * 1000)
    digest = int.from_bytes(
        hashlib.sha256(interval.model_dump_json().encode()).digest()[:10], "big"
    )
    return _uuid7(millis, digest)


def _uuid7(millis: int, digest: int) -> str:
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 7 << 76 | ((digest >> 62) & 0xFFF) << 64 | 2 << 62 | (digest & ((1 << 62) - 1))
    return str(UUID(int=value))


def reevaluation_run_id(request_event_id: str) -> str:
    """Stable separate identity; Finish reserves the original event ID."""
    source = UUID(request_event_id)
    digest = int.from_bytes(
        hashlib.sha256(b"extraction.reevaluation_run\0" + source.bytes).digest()[:10], "big"
    )
    derived = _uuid7(source.int >> 80, digest)
    return str(UUID(int=source.int ^ 1)) if derived == request_event_id else derived


def _retry_barrier(run: SystemRecord) -> str | None:
    payload = run.payload
    kind = payload["command"].get("kind")
    if kind == "reevaluate":
        if run.id == payload.get("request_event_id") or (
            "request_event_id" not in payload and payload.get("event_id") == run.id
        ):
            return "legacy_completion_id_collision"
        if not payload.get("request_event_id"):
            return "reevaluation_request_required"
    if calls := payload.get("gateway_calls"):
        if any(call.get("state") != "returned" for call in calls):
            return "gateway_outcome_unknown"
        return "gateway_result_not_committed"
    if _LEGACY_WINDOW_TRIAL in payload:
        return "automatic_timeline_legacy_trial_already_used"
    if (
        "gateway_calls" not in payload
        and kind in MODEL_COMMANDS
        and (
            payload["attempts"] > 0
            or payload["status"] == "running"
            # A pre-upgrade retry could reset attempts to zero and clear the error.
            or "retry_at" in payload
            or "lease_until" in payload
        )
    ):
        # A previous version could have sent a call without recording an intent.
        return "legacy_gateway_outcome_untracked"
    return None


_COURSE_INTENT_PURPOSES = {
    "segmentation",
    "merge_activities",
    "selection",
    "generation",
    "consolidation",
    "course_blocks",
    "session_fragments",
    "course_boundary",
}


def _predecessor_intents(
    run: SystemRecord, *, allow_sending: bool, purposes: set[str] = _COURSE_INTENT_PURPOSES
) -> list[dict[str, Any]]:
    calls = run.payload.get("gateway_calls")
    if not isinstance(calls, list) or not calls:
        return []  # Untracked legacy work is not auditable by this mechanism.
    ids = set()
    for i, call in enumerate(calls):
        if (
            not isinstance(call, dict)
            or not is_id(call.get("call_id"))
            or call["call_id"] in ids
            or not is_sha256(call.get("request_fingerprint"))
            or not isinstance(call.get("purpose"), str)
            or call["purpose"] not in purposes
            or (
                call.get("state") != "returned"
                and not (allow_sending and i == len(calls) - 1 and call.get("state") == "sending")
            )
        ):
            return []
        ids.add(call["call_id"])
    return calls


def validate_course_reextract_config(
    course_blocks: bool,
    enabled: bool,
    draft: str | None,
    predecessors: tuple[str, ...],
    spans: tuple[CourseDraftSpan, ...] | None,
    attestations: tuple[SchemaDiscardAttestation, ...],
    timeline: ReviewedTimelineDraft | None = None,
) -> None:
    if type(enabled) is not bool:
        raise ValueError("transcript_course_reextract_must_be_bool")
    if timeline is not None and (
        not isinstance(timeline, ReviewedTimelineDraft)
        or not enabled
        or draft is not None
        or spans is not None
        or attestations
    ):
        raise ValueError("invalid_course_reextract_timeline_settings")
    if enabled:
        if not course_blocks:
            raise ValueError("transcript_course_reextract_requires_course_blocks")
        if timeline is None and not is_sha256(draft):
            raise ValueError("transcript_course_reextract_requires_draft_sha256")
    elif draft is not None or predecessors or spans is not None or attestations:
        raise ValueError("unexpected_course_reextract_settings")
    if (
        len(predecessors) > MAX_COURSE_REEXTRACT_PREDECESSORS
        or not all(is_id(id) for id in predecessors)
        or len(set(predecessors)) != len(predecessors)
    ):
        raise ValueError("invalid_course_reextract_reconciled_run_ids")
    if spans is not None and not (
        1 <= len(spans) <= MAX_COURSE_DRAFT_SPANS
        and all(isinstance(span, CourseDraftSpan) for span in spans)
    ):
        raise ValueError("invalid_course_reextract_draft_spans")
    if (
        len(attestations) > MAX_SCHEMA_DISCARD_ATTESTATIONS
        or not all(
            isinstance(a, SchemaDiscardAttestation) and a.run_id in predecessors
            for a in attestations
        )
        or len({a.call_id for a in attestations}) != len(attestations)
        or len({a.run_id for a in attestations}) != len(attestations)
    ):
        raise ValueError("invalid_course_reextract_schema_discard_attestations")
    if attestations and spans is None:
        raise ValueError("course_reextract_schema_discard_requires_draft_spans")


class ExtractionService:
    def __init__(
        self,
        storage: StorageClient,
        gateway: ModelGateway,
        *,
        clock: Callable[[], datetime] = now_utc,
        max_attempts: int = 3,
        retry_seconds: int = 30,
        wait_seconds: int = 1800,
        lease_seconds: int = 900,
        max_input_chars: int = 100_000,
        activity_min_duration_minutes: int = DEFAULT_ACTIVITY_MIN_DURATION_MINUTES,
        transcript_only: bool = False,
        transcript_summary_rule_id: str | None = None,
        transcript_max_calls: int = DEFAULT_TRANSCRIPT_MAX_CALLS,
        transcript_allowed_windows: tuple[Interval, ...] = (),
        transcript_course_sessions: bool = False,
        course_blocks: bool = False,
        course_call_seconds: int = 900,
        transcript_course_reextract: bool = False,
        transcript_course_reextract_draft_sha256: str | None = None,
        transcript_course_reextract_reconciled_run_ids: tuple[str, ...] = (),
        transcript_course_reextract_draft_spans: tuple[CourseDraftSpan, ...] | None = None,
        transcript_course_reextract_schema_discard_attestations: tuple[
            SchemaDiscardAttestation, ...
        ] = (),
        transcript_course_reextract_timeline: ReviewedTimelineDraft | None = None,
        transcript_automatic_timeline: PreflightInput | None = None,
        transcript_window_review_concurrency: int = DEFAULT_WINDOW_REVIEW_CONCURRENCY,
        transcript_automatic_timeline_prewrite_pins: tuple[AutomaticPredecessorPin, ...] = (),
        transcript_automatic_timeline_gaps: tuple[ExactInterval, ...] = (),
        transcript_automatic_timeline_asr_gaps: tuple[ExactInterval, ...] = (),
    ):
        if type(transcript_only) is not bool:
            raise ValueError("transcript_only_must_be_bool")
        validate_summary_rule_config(transcript_only, transcript_summary_rule_id)
        if type(transcript_max_calls) is not int or transcript_max_calls <= 0:
            raise ValueError("transcript_max_calls_must_be_positive_int")
        if type(transcript_course_sessions) is not bool:
            raise ValueError("transcript_course_sessions_must_be_bool")
        if type(course_blocks) is not bool or (course_blocks and not transcript_course_sessions):
            raise ValueError("course_blocks_require_course_sessions")
        validate_course_reextract_config(
            course_blocks,
            transcript_course_reextract,
            transcript_course_reextract_draft_sha256,
            transcript_course_reextract_reconciled_run_ids,
            transcript_course_reextract_draft_spans,
            transcript_course_reextract_schema_discard_attestations,
            transcript_course_reextract_timeline,
        )
        if transcript_course_reextract_timeline is not None:
            validate_timeline_windows(transcript_allowed_windows)
        if transcript_course_sessions:
            if not transcript_only or transcript_summary_rule_id is None:
                raise ValueError("transcript_course_sessions_requires_fixed_transcript_rule")
            validate_session_windows(transcript_allowed_windows)
            if type(course_call_seconds) is not int or course_call_seconds <= 0:
                raise ValueError("positive_course_call_seconds_required")
        if storage.module != "extraction":
            raise ValueError("extraction_storage_client_required")
        if min(max_attempts, retry_seconds, wait_seconds, lease_seconds, max_input_chars) <= 0:
            raise ValueError("positive_limits_required")
        self.storage, self.gateway, self.clock = storage, gateway, clock
        self.max_attempts, self.retry_seconds, self.wait_seconds = (
            max_attempts,
            retry_seconds,
            wait_seconds,
        )
        self.lease_seconds, self.max_input_chars = lease_seconds, max_input_chars
        validate_activity_min_duration(activity_min_duration_minutes)
        self.activity_min_duration_minutes = activity_min_duration_minutes
        self.transcript_only = transcript_only
        self.transcript_summary_rule_id = transcript_summary_rule_id
        self.transcript_max_calls = transcript_max_calls
        self.transcript_allowed_windows = transcript_allowed_windows
        self.transcript_course_sessions = transcript_course_sessions
        self.course_blocks = course_blocks
        self.course_call_seconds = course_call_seconds
        if transcript_automatic_timeline is None and transcript_automatic_timeline_prewrite_pins:
            raise ValueError("automatic_timeline_prewrite_pins_require_scope")
        if transcript_automatic_timeline is None and transcript_automatic_timeline_gaps:
            raise ValueError("automatic_timeline_gaps_require_scope")
        if transcript_automatic_timeline is None and transcript_automatic_timeline_asr_gaps:
            raise ValueError("automatic_timeline_asr_gaps_require_scope")
        if (
            type(transcript_window_review_concurrency) is not int
            or not 1 <= transcript_window_review_concurrency <= MAX_WINDOW_REVIEW_CONCURRENCY
        ):
            raise ValueError("invalid_transcript_window_review_concurrency")
        # The injected ModelGateway protocol does not promise thread safety.
        self.transcript_window_review_concurrency = (
            transcript_window_review_concurrency if isinstance(gateway, GatewayClient) else 1
        )
        self.automatic_timeline = (
            AutomaticTimeline.configure(
                transcript_automatic_timeline,
                transcript_allowed_windows,
                course_blocks=course_blocks,
                course_reextract=transcript_course_reextract,
                summary_rule_id=transcript_summary_rule_id,
                prewrite_pins=transcript_automatic_timeline_prewrite_pins,
                gaps=transcript_automatic_timeline_gaps,
                asr_gaps=transcript_automatic_timeline_asr_gaps,
                activity_min_duration_minutes=activity_min_duration_minutes,
            )
            if transcript_automatic_timeline is not None
            else None
        )
        self.course_timeline = transcript_course_reextract_timeline
        self.course_reextract_draft = (
            self.course_timeline.draft_sha256
            if self.course_timeline is not None
            else transcript_course_reextract_draft_sha256
        )
        self.course_reextract_predecessors = frozenset(
            transcript_course_reextract_reconciled_run_ids
        )
        self.course_draft_spans = transcript_course_reextract_draft_spans
        self.course_schema_discards = transcript_course_reextract_schema_discard_attestations

    def _course_reextract_windows(self, interval: Interval) -> tuple[Interval, ...]:
        if self.automatic_timeline is not None:
            if (
                not self.automatic_timeline.replacing
                or self.automatic_timeline.interval != interval
            ):
                raise Blocked("automatic_timeline_requires_exact_reextract_scope")
            return self.transcript_allowed_windows
        if self.course_reextract_draft is None:
            raise Blocked("transcript_course_sessions_manual_extract_only")
        windows = self.transcript_allowed_windows
        if interval.start != windows[0].start or interval.end != windows[-1].end:
            raise Blocked("course_reextract_requires_full_window_list")
        return windows

    def _course_windows(self, interval: Interval) -> tuple[Interval, ...]:
        # No synthetic windows: match the inclusive endpoints in the original list
        # and keep all configured entries between them, even if they do not touch.
        windows = self.transcript_allowed_windows
        for i, first in enumerate(windows):
            if first.start == interval.start:
                for j in range(i, len(windows)):
                    if windows[j].end == interval.end:
                        return windows[i : j + 1]
                break
        raise Blocked("transcript_interval_not_allowed")

    @staticmethod
    def _refuse_course_overlaps(reader: Reader, windows: tuple[Interval, ...]) -> None:
        for window in windows:
            time = TimeRange(window.start, window.end)
            if reader.query(Query("activity", time=time, limit=1)).items:
                raise Blocked("session_activity_overlap")

    @staticmethod
    def _bridged_course_gaps(
        windows: tuple[Interval, ...], ops: list[WriteOp]
    ) -> tuple[Interval, ...]:
        # Actual spans can cross short capture gaps. Guard those extra portions
        # against existing activities without reading transcript there.
        intervals = [
            (op.event_start, op.event_end)
            for op in ops
            if isinstance(op, Create) and op.data_type == "activity"
        ]
        return tuple(
            Interval(start=a.end, end=b.start)
            for a, b in pairwise(windows)
            if a.end < b.start
            and any(
                start is not None and end is not None and start < b.start and a.end < end
                for start, end in intervals
            )
        )

    def _refuse_course_replay(
        self, reader: Reader, windows: tuple[Interval, ...], run_id: str
    ) -> None:
        # Creating new feedback must not silently replay an unresolved overlapping
        # send. Also respect another running claimant (including an expired one
        # with intents) before the local host has recovered it.
        for window in windows:
            time = TimeRange(window.start, window.end)
            for other in iter_query(reader, Query("extraction_run", time=time)):
                if not isinstance(other, SystemRecord) or other.id == run_id:
                    continue
                if other.payload["command"].get("kind") not in {"extract", "reextract"}:
                    continue
                if other.payload["status"] == "running" or (
                    self._held_range(other) and not self._legacy_budget_ended_before_write(other)
                ):
                    raise Blocked("overlapping_unresolved_model_call")

    def _refuse_automatic_timeline_replay(
        self, scope: AutomaticTimeline, current: SystemRecord
    ) -> None:
        pins = {pin.run_id: pin for pin in scope.prewrite_pins}
        legacy = scope.legacy_review_pin
        strict_holds = scope.strict_replacement or bool(scope.asr_gaps)
        call_ids: set[str] = set()
        for other in iter_query(
            self.storage,
            Query("extraction_run", time=TimeRange(scope.interval.start, scope.interval.end)),
        ):
            if not isinstance(other, SystemRecord) or other.id == current.id:
                continue
            p = other.payload
            if legacy is not None and p.get(_LEGACY_WINDOW_TRIAL) == legacy.run_id:
                raise Blocked("automatic_timeline_legacy_trial_already_used")
            if strict_holds and (
                p["status"] not in TERMINAL
                or (p["status"] == "superseded" and _retry_barrier(other) is not None)
                or any(c.get("state") != "returned" for c in p.get("gateway_calls", ()))
                or p.get("error")
                in {
                    "CommitUnknown",
                    "commit_outcome_unknown",
                    "gateway_outcome_unknown",
                    "gateway_result_not_committed",
                    "legacy_gateway_outcome_untracked",
                }
            ):
                # Hard holds anywhere in the envelope, including declared gaps,
                # are never cleared by a pin.
                raise Blocked("overlapping_unresolved_model_call")
            is_extraction = p["command"].get("kind") in {"extract", "reextract"}
            if is_extraction and (pin := pins.pop(other.id, None)):
                proven = self._automatic_prewrite_call_ids(other, current, pin, scope)
                if not proven:
                    raise Blocked("overlapping_unresolved_model_call")
                if call_ids.intersection(proven):
                    raise Blocked("automatic_timeline_prewrite_duplicate_call")
                call_ids.update(proven)
                continue
            # Returned intents still hold a failed range by default. Only a
            # fully validated pin above can clear this hold for a fresh command.
            if strict_holds and self._held_range(other):
                raise Blocked("overlapping_unresolved_model_call")
            if not is_extraction:
                continue
            calls = p.get("gateway_calls", ())
            status = p["status"]
            # Leave the no-pin path's treatment of fresh queued jobs alone.
            # A pinned exception requires every other overlap to be settled.
            if (
                (
                    status not in TERMINAL
                    and (scope.prewrite_pins or status != "queued" or p["attempts"] or calls)
                )
                or self._held_range(other)
                or any(c.get("state") != "returned" for c in calls)
                or (
                    scope.prewrite_pins
                    and (
                        (calls and status != "succeeded")
                        or p.get("error") in {"CommitUnknown", "commit_outcome_unknown"}
                    )
                )
                or p.get("error")
                in {
                    "gateway_outcome_unknown",
                    "gateway_result_not_committed",
                    "legacy_gateway_outcome_untracked",
                }
            ):
                raise Blocked("overlapping_unresolved_model_call")
            if status == "succeeded" and (
                not scope.replacing
                or p.get("detail", {}).get("course_replacement") is True
                # This exception is for failure only, never a prior successful reextract
                # (including older jobs without the course_replacement marker).
                or (scope.prewrite_replacement and p["command"].get("kind") == "reextract")
            ):
                raise Blocked("automatic_timeline_overlapping_successful_run")
        if pins:
            raise Blocked("automatic_timeline_unused_prewrite_pin")

    def _automatic_prewrite_call_ids(
        self,
        run: SystemRecord,
        current: SystemRecord,
        pin: AutomaticPredecessorPin,
        scope: AutomaticTimeline,
    ) -> tuple[str, ...]:
        p = run.payload
        try:
            previous = Interval.model_validate(p["command"]["interval"])
        except (KeyError, ValidationError):
            return ()
        if (
            run.writer != "extraction"
            or p.get("scheduled") is not False
            or p.get("request_event_id") is not None
            or p.get("event_id") != run.id
            or not is_id(p.get("root_feedback_id"))
            or p.get("depth") != 0
            or type(p.get("attempts")) is not int
            or p["attempts"] != 1
            or p.get("lease_until") is not None
            or run.event_start != previous.start
            or run.event_end != previous.end
            or not (previous.start < pin.scope.end and pin.scope.start < previous.end)
        ):
            return ()
        if isinstance(pin, LegacyWindowReviewRunPin):
            command = current.payload["command"]
            if (
                scope.legacy_scope_sha256 is None
                or p.get("automatic_timeline_scope_sha256") != scope.legacy_scope_sha256
                or previous != scope.interval
                or p["command"].get("kind") != command.get("kind")
                or p["command"].get("preview") != command.get("preview")
                or p["root_feedback_id"] == current.payload.get("root_feedback_id")
                or p.get("status") != "blocked"
                or p.get("error") != INVALID_WINDOW_REVIEW
                or p.get("model_calls") != []
                or _LEGACY_WINDOW_TRIAL in p
            ):
                return ()
            calls = _predecessor_intents(
                run, allow_sending=False, purposes={LEGACY_WINDOW_REVIEW_PURPOSE}
            )
        else:
            if p.get("automatic_timeline_scope_sha256") is not None:
                return ()
            if scope.prewrite_replacement and (
                p["command"].get("kind") != "extract"
                or p["root_feedback_id"] == current.payload.get("root_feedback_id")
                or _LEGACY_WINDOW_TRIAL in p
            ):
                return ()
            calls = _predecessor_intents(run, allow_sending=False)
        if not calls:
            return ()
        if isinstance(pin, EmptySuccessRunPin):
            detail = p.get("detail")
            if (
                p["command"].get("kind") != "extract"
                or p["status"] != "succeeded"
                or p.get("error") is not None
                or p.get("retry_at") is not None
                or p["root_feedback_id"] == current.payload.get("root_feedback_id")
                or previous.start > pin.scope.start
                or previous.end < pin.scope.end
                or (previous.start, previous.end) == (pin.scope.start, pin.scope.end)
                or not isinstance(detail, dict)
                or "automatic_timeline" in detail
                or "course_replacement" in detail
                or type(detail.get("activities")) is not int
                or detail["activities"] != len(pin.outside_activity_ids)
            ):
                return ()
        elif not isinstance(pin, LegacyWindowReviewRunPin) and not (
            self._course_failed_before_write(run)
            or (
                p["status"] == "blocked"
                and p.get("error") == "session_all_rows_unclassified"
                and all(
                    c["purpose"] in {"course_blocks", "session_fragments", "course_boundary"}
                    for c in calls
                )
            )
        ):
            return ()
        if pin.metadata_sha256 != prewrite_run_metadata_sha256(run):
            raise Blocked("automatic_timeline_prewrite_pin_changed")
        if run.updated_at >= current.created_at:
            return ()  # Must already have been terminal when the new feedback was admitted.
        ids = tuple(c["call_id"] for c in calls)
        if ids != tuple(audit.call_id for audit in pin.calls):
            raise Blocked("automatic_timeline_prewrite_audit_mismatch")
        if isinstance(pin, EmptySuccessRunPin):
            self._check_empty_success_outputs(run, previous, pin)
        return ids

    def _check_empty_success_outputs(
        self, run: SystemRecord, previous: Interval, pin: EmptySuccessRunPin
    ) -> None:
        # Corroborate the private evidence against existing version-1 outputs in
        # the old atomic success batch. No old bodies are used in the new plan.
        scope = TimeRange(pin.scope.start, pin.scope.end)
        outputs = self.storage.get_many(pin.outside_activity_ids)
        for id in pin.outside_activity_ids:
            record = outputs.get(id)
            # Later scoped work elsewhere may have superseded the witnessed
            # output. It still has an immutable original version; complete
            # history verification separately checks that none overlap scope.
            if isinstance(record, SemanticRecord) and record.version > 1:
                record = self.storage.get_version(id, 1)
            if (
                not isinstance(record, SemanticRecord)
                or record.data_type != "activity"
                or record.id != id
                or record.version != 1
                or record.superseded
                or record.writer != "extraction"
                or record.root_feedback_id != run.payload["root_feedback_id"]
                or record.depth != 0
                or record.request_event_id is not None
                or record.created_at != run.updated_at
                or record.event_start is None
                or record.event_end is None
                or not previous.start <= record.event_start < record.event_end <= previous.end
                or scope.overlaps(record.event_start, record.event_end)
            ):
                raise Blocked("automatic_timeline_empty_success_outputs_unverified")

    def _check_course_reextract_predecessors(
        self, interval: Interval, windows: tuple[Interval, ...], current: SystemRecord
    ) -> None:
        # The legacy request might be for the entire envelope or a smaller set of
        # selected windows. Read only job metadata; never read or retry old inputs.
        unreconciled = set(self.course_reextract_predecessors)
        unused_attestations = {a.call_id: a for a in self.course_schema_discards}
        for other in iter_query(
            self.storage, Query("extraction_run", time=TimeRange(interval.start, interval.end))
        ):
            if not isinstance(other, SystemRecord) or other.id == current.id:
                continue
            p = other.payload
            kind = p["command"].get("kind")
            if kind not in {"extract", "reextract"}:
                continue
            if (
                kind == "reextract"
                and p["status"] == "succeeded"
                and p.get("detail", {}).get("course_replacement") is True
                and Interval.model_validate(p["command"]["interval"]) == interval
            ):
                raise Blocked("course_reextract_already_completed")
            if p["status"] not in TERMINAL:
                raise Blocked("overlapping_unresolved_model_call")
            if p["status"] in {"succeeded", "superseded"} and any(
                call.get("state") != "returned" for call in p.get("gateway_calls", ())
            ):
                raise Blocked("overlapping_unresolved_model_call")
            if not self._held_range(other):
                if p["status"] in {"failed", "blocked"} and p.get("error") in {
                    "gateway_outcome_unknown",
                    "gateway_result_not_committed",
                    "legacy_gateway_outcome_untracked",
                }:
                    raise Blocked("overlapping_unresolved_model_call")
                continue
            if other.id not in unreconciled or not self._prove_course_predecessor(
                other, interval, windows, current.created_at, unused_attestations
            ):
                raise Blocked("overlapping_unresolved_model_call")
            unreconciled.remove(other.id)
        if unreconciled:
            raise Blocked("course_reextract_unverified_predecessor_id")
        if unused_attestations:
            raise Blocked("course_reextract_unused_schema_discard_attestation")

    def _prove_course_predecessor(
        self,
        run: SystemRecord,
        interval: Interval,
        windows: tuple[Interval, ...],
        admitted_at: datetime,
        unused_attestations: dict[str, SchemaDiscardAttestation],
    ) -> bool:
        p = run.payload
        try:
            previous = Interval.model_validate(p["command"]["interval"])
        except (KeyError, ValidationError):
            return False
        if (
            p.get("scheduled") is not False
            or p.get("request_event_id") is not None
            or run.event_start != previous.start
            or run.event_end != previous.end
            or not (previous == interval or within_capture_coverage(previous, windows))
        ):
            return False
        calls = _predecessor_intents(run, allow_sending=True)
        if not calls:
            return False
        if calls[-1]["state"] == "returned":
            return self._course_failed_before_write(run)
        # Reviewed mode cannot clear any sending intent. The definitive local
        # pre-write cases above still require an explicitly listed predecessor.
        if self.course_timeline is not None:
            return False
        if p["status"] != "blocked" or p.get("error") != "gateway_outcome_unknown":
            return False
        reconcile = getattr(self.gateway, "reconcile", None)
        if not callable(reconcile):
            return False
        call_id = calls[-1]["call_id"]
        try:
            receipt = reconcile(call_id)  # Authenticated audit-only; no provider call or body.
        except Exception:
            return False
        if not (
            isinstance(receipt, dict)
            and set(receipt)
            == {"call_id", "record_saved", "status", "error_code", "response_recoverable"}
            and receipt.get("call_id") == call_id
            and receipt.get("record_saved") is True
            and receipt.get("response_recoverable") is False
        ):
            return False
        if receipt["status"] == "success":
            return receipt["error_code"] is None
        if receipt["status"] != "failed":
            return False
        if receipt["error_code"] == "provider_rejected":
            return True

        # The gateway intentionally conflates local schema discards with unknown
        # delivery. Only the host's independent per-call log proof can narrow that
        # ambiguity. A sole segmentation send cannot have reached business write.
        attestation = unused_attestations.get(call_id)
        if (
            receipt["error_code"] == "provider_outcome_unknown"
            and attestation is not None
            and attestation.run_id == run.id
            and attestation.request_fingerprint == calls[-1]["request_fingerprint"]
            and run.updated_at < admitted_at
            and p["command"].get("kind") == "extract"
            and previous in windows
            and len(calls) == 1
            and calls[0]["state"] == "sending"
            and calls[0]["purpose"] == "segmentation"
        ):
            del unused_attestations[call_id]
            return True
        return False

    def _course_failed_before_write(self, run: SystemRecord) -> bool:
        # Shared only after validating the complete returned-intent list. These
        # are local failures before business write, not write or provider errors.
        p = run.payload
        return p["status"] == "failed" and (
            p.get("error") in {"ValueError", "ValidationError", "InputBudgetExceeded"}
            or self._legacy_budget_ended_before_write(run)
        )

    @staticmethod
    def _legacy_budget_ended_before_write(run: SystemRecord) -> bool:
        # Only the old five-call extraction limit can be positively identified
        # here. Its exception was raised in ask(), before constructing the
        # business batch. This permits a new course-specific plan, not a retry
        # of the old run or a bypass for a lost response/local write outcome.
        p = run.payload
        calls = p.get("gateway_calls", [])
        return (
            p["status"] == "failed"
            and p.get("error") == "transcript_max_calls_exceeded"
            and p["command"].get("kind") == "extract"
            and len(calls) == DEFAULT_TRANSCRIPT_MAX_CALLS
            and all(
                c.get("state") == "returned"
                and c.get("purpose")
                in {"segmentation", "merge_activities", "selection", "generation", "consolidation"}
                for c in calls
            )
        )

    def subscribe(self) -> None:
        self.storage.subscribe(CONSUMER, self.handle_event)

    def handle_event(self, event: Event) -> None:
        run_id = event.event_id
        if isinstance(event, DataChanged):
            if event.data_type != "feedback" or event.operation != "created":
                return
            feedback = self.storage.get(event.data_id)
            if (
                not isinstance(feedback, RawRecord)
                or feedback.payload.get("module") != "extraction"
            ):
                return
            raw = feedback.payload.get("command", {})
            # Feedback is the root, independent of a caller's self-reported envelope.
            context = Context.feedback(feedback.id)
        elif isinstance(event, ReevaluationRequested):
            if event.target_type not in {"artifact", "rule_selection"}:
                return
            context = Context.of(event)
            if self.storage.get_reevaluation_result(event.event_id) is not None:
                return
            legacy = self.storage.get_system_record(event.event_id)
            if legacy is not None and legacy.data_type == "extraction_run":
                if legacy.payload.get("event_id") != event.event_id or (
                    legacy.payload["command"].get("kind") != "reevaluate"
                ):
                    raise Blocked("reevaluation_request_id_occupied")
                if legacy.payload["status"] not in TERMINAL:
                    self._finish(legacy, context, "blocked", "legacy_completion_id_collision")
                return
            run_id = reevaluation_run_id(event.event_id)
            raw = {
                "kind": "reevaluate",
                "target_id": event.target_id,
                "target_type": event.target_type,
                "description": event.change_description,
            }
        else:
            return
        self._admit(
            run_id,
            raw,
            context,
            event_id=event.event_id,
            request_time=event.occurred_at if isinstance(event, ReevaluationRequested) else None,
            request_cause=event.cause if isinstance(event, ReevaluationRequested) else None,
        )

    def _admit(
        self,
        id: str,
        raw: dict,
        context: Context,
        *,
        event_id: str | None = None,
        scheduled: bool = False,
        request_time: datetime | None = None,
        request_cause: Cause | None = None,
    ) -> None:
        def matches(run: SystemRecord) -> bool:
            return (
                run.data_type == "extraction_run"
                and run.payload.get("event_id") == event_id
                and run.payload.get("request_event_id") == context.request_event_id
                and run.payload.get("scheduled") == scheduled
            )

        existing = self.storage.get_system_record(id)
        if existing:
            if matches(existing):
                return
            raise Blocked("extraction_run_id_occupied")
        error = None
        try:
            command = Command.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            command, error = {}, "invalid_command"
        if command.get("kind") == "reevaluate" and context.request_event_id is None:
            error = "reevaluation_request_required"
        payload = {
            "command": command,
            "status": "failed" if error else "queued",
            "error": error,
            "root_feedback_id": context.root_feedback_id,
            "depth": context.depth,
            "request_event_id": context.request_event_id,
            "additional_causes": [
                trace.model_dump(mode="json") for trace in context.additional_causes
            ],
            "scheduled": scheduled,
            "attempts": 0,
            "created_at": format_utc(self.clock()),
            "event_id": event_id,
            "model_calls": [],
            "gateway_calls": [],
        }
        if self.automatic_timeline is not None and command.get("kind") in {"extract", "reextract"}:
            payload["automatic_timeline_scope_sha256"] = self.automatic_timeline.scope_sha256
        if request_cause is not None:
            payload["request_cause"] = request_cause.model_dump(mode="json")
        if request_time is not None:
            payload["request_occurred_at"] = format_utc(request_time)
        interval = command.get("interval")
        bounds = Interval.model_validate(interval) if interval and not error else None
        ops: list[WriteOp] = [
            PutSystemRecord(
                "extraction_run",
                id,
                payload,
                event_start=bounds.start if bounds else None,
                event_end=bounds.end if bounds else None,
            )
        ]
        if event_id:
            ops.append(MarkProcessed(CONSUMER, event_id))
        result = self.storage.write(ops, context)
        if not result.ok:
            existing = self.storage.get_system_record(id)
            if existing is None or not matches(existing):
                result.check()

    def runs(self) -> list[SystemRecord]:
        return [
            r
            for r in iter_query(self.storage, Query("extraction_run"))
            if isinstance(r, SystemRecord)
        ]

    def schedule(self, interval: Interval) -> list[str]:
        if self.transcript_course_sessions:
            raise Blocked("transcript_course_sessions_manual_extract_only")
        runs = self.runs()
        coverage = [
            Interval.model_validate(r.payload["command"]["interval"])
            for r in runs
            if r.payload["status"] == "succeeded"
            and r.payload["command"].get("kind") in {"extract", "reextract"}
        ]
        # Reserve admitted ranges so repeated ticks do not add overlapping queued jobs.
        coverage.extend(
            Interval.model_validate(r.payload["command"]["interval"])
            for r in runs
            if (r.payload["status"] not in TERMINAL or self._held_range(r))
            and r.payload["command"].get("kind") in {"extract", "reextract"}
        )
        ids = []
        for item in uncovered(interval, coverage):
            id = schedule_id(item)
            existing = self.storage.get_system_record(id)
            if existing and existing.payload["status"] in {"failed", "blocked"}:
                # Explicit retry is required; failed ranges never become coverage.
                ids.append(id)
                continue
            self._admit(
                id,
                Command(kind="extract", interval=item).model_dump(mode="json"),
                Context.system(),
                scheduled=True,
            )
            ids.append(id)
        return ids

    def preview_reextract(self, interval: Interval) -> dict:
        if self.transcript_course_sessions:
            windows = self._course_reextract_windows(interval)
            targets = (
                self.automatic_timeline.live_targets(self.storage, windows)
                if self.automatic_timeline is not None
                else course_replacement_targets(self.storage, interval, windows)
            )
            return {"versions": targets.preview_versions, "modified_activities": 0}
        with self.storage.snapshot() as reader:
            return preview_reextract(reader, interval)

    def retry(self, run_id: str) -> None:
        run = self.storage.get_system_record(run_id)
        if (
            run is None
            or run.data_type != "extraction_run"
            or run.payload["status"] not in {"failed", "blocked"}
        ):
            raise ValueError("run_not_retryable")
        if (
            request_id := run.payload.get("request_event_id")
        ) and self.storage.get_reevaluation_result(request_id) is not None:
            raise ValueError("terminal_request_requires_new_request")
        if reason := _retry_barrier(run):
            raise ValueError(reason)
        self.storage.write(
            [
                PutSystemRecord(
                    "extraction_run",
                    run.id,
                    {
                        **run.payload,
                        "status": "queued",
                        "attempts": 0,
                        "error": None,
                        "created_at": format_utc(self.clock()),
                        "retry_at": None,
                    },
                    expected_revision=run.revision,
                )
            ],
            self._context(run),
        ).check()

    @staticmethod
    def _context(run: SystemRecord) -> Context:
        payload = run.payload
        return Context(
            payload["root_feedback_id"],
            payload["depth"],
            payload.get("request_event_id"),
            payload.get("execution_additional_causes", payload.get("additional_causes", ())),
        )

    @staticmethod
    def _held_range(run: SystemRecord) -> bool:
        # These ranges are NOT successful coverage. Do not let a later, larger timer
        # interval create a different run ID and implicitly repeat an uncertain send.
        return run.payload["status"] in {"blocked", "failed"} and _retry_barrier(run) is not None

    def reconcile_run(self, run_id: str, *, call_id: str | None = None) -> dict[str, Any]:
        """Audit-only. Never return a model output or change task retry eligibility."""
        run = self.storage.get_system_record(run_id)
        reconcile = getattr(self.gateway, "reconcile", None)
        if (
            run is None
            or run.data_type != "extraction_run"
            or run.payload["status"] not in {"blocked", "failed"}
            or not (calls := run.payload.get("gateway_calls"))
            or not callable(reconcile)
        ):
            raise ValueError("run_not_reconcilable")
        if call_id is None:
            # The last intent may already be returned while an earlier one is unknown.
            call_id = next(
                (c["call_id"] for c in reversed(calls) if c["state"] == "sending"),
                calls[-1]["call_id"],
            )
        elif not any(c["call_id"] == call_id for c in calls):
            raise ValueError("call_not_in_run")
        receipt = reconcile(call_id)
        if not isinstance(receipt, dict):
            raise ValueError("invalid_reconcile_receipt")
        return {
            "call_id": call_id,
            "record_saved": receipt.get("record_saved") is True,
            "status": receipt.get("status"),
            "error_code": receipt.get("error_code"),
            "response_recoverable": False,
        }

    def drain(self) -> None:
        for run in self.runs():
            status = run.payload["status"]
            if status in TERMINAL:
                continue
            deadline = run.payload.get("lease_until" if status == "running" else "retry_at")
            if deadline and datetime.fromisoformat(deadline) > self.clock():
                continue
            self.process(run.id)

    def _refresh_scheduled(self, run: SystemRecord) -> bool:
        if not run.payload.get("scheduled"):
            return False
        interval = Interval.model_validate(run.payload["command"]["interval"])
        runs = self.runs()
        for other in runs:
            if other.id == run.id or not self._held_range(other):
                continue
            if other.payload["command"].get("kind") not in {"extract", "reextract"}:
                continue
            held = Interval.model_validate(other.payload["command"]["interval"])
            if held.start < interval.end and interval.start < held.end:
                self._finish(
                    run, self._context(run), "blocked", "overlapping_unresolved_model_call"
                )
                return True
        coverage = [
            Interval.model_validate(r.payload["command"]["interval"])
            for r in runs
            if r.id != run.id
            and r.payload["status"] == "succeeded"
            and r.payload["command"].get("kind") in {"extract", "reextract"}
        ]
        remaining = uncovered(interval, coverage)
        if remaining == [interval]:
            return False
        # A manual run may have completed after this timer job was admitted.
        # Retire it first. A later tick can always re-admit the uncovered ranges after a crash.
        result = self.storage.write(
            [
                PutSystemRecord(
                    "extraction_run",
                    run.id,
                    {
                        **run.payload,
                        "status": "superseded",
                        "detail": {"reason": "coverage_changed"},
                    },
                    expected_revision=run.revision,
                )
            ],
            self._context(run),
        )
        if result.ok:
            for item in remaining:
                self._admit(
                    schedule_id(item),
                    Command(kind="extract", interval=item).model_dump(mode="json"),
                    Context.system(),
                    scheduled=True,
                )
        return True

    def process(self, run_id: str) -> None:
        run = self.storage.get_system_record(run_id)
        if run is None or run.data_type != "extraction_run" or run.payload["status"] in TERMINAL:
            return
        deadline = run.payload.get(
            "lease_until" if run.payload["status"] == "running" else "retry_at"
        )
        if deadline and datetime.fromisoformat(deadline) > self.clock():
            return
        context = self._context(run)
        if reason := _retry_barrier(run):
            self._finish(run, context, "blocked", reason)
            return
        if (
            self.transcript_only
            and run.payload["command"].get("kind") not in TRANSCRIPT_ONLY_COMMANDS
        ):
            # Old activities/artifacts and reevaluation descriptions may contain
            # context from the default (multimodal) path. Never send them.
            self._finish(run, context, "failed", "transcript_only_command_unsupported")
            return
        session_windows: tuple[Interval, ...] = ()
        course_max_calls = None
        course_draft_guard = None
        scoped_transcript_guard = None
        automatic_timeline = None
        if run.payload["command"].get("kind") in {"extract", "reextract"}:
            automatic_timeline = self.automatic_timeline
            expected_scope = automatic_timeline.scope_sha256 if automatic_timeline else None
            if run.payload.get("automatic_timeline_scope_sha256") != expected_scope:
                self._finish(run, context, "blocked", "automatic_timeline_scope_changed")
                return
        lease_seconds = self.lease_seconds
        if self.transcript_course_sessions:
            try:
                command = Command.model_validate(run.payload["command"])
                if run.payload.get("scheduled"):
                    raise Blocked("transcript_course_sessions_manual_extract_only")
                if command.kind in {"extract", "reextract"}:
                    assert command.interval is not None
                    if automatic_timeline is not None:
                        if (
                            command.interval != automatic_timeline.interval
                            or (command.kind == "reextract") != automatic_timeline.replacing
                            or (command.kind == "extract" and command.preview is not None)
                        ):
                            raise Blocked("automatic_timeline_requires_exact_command_scope")
                        if (
                            run.payload.get("event_id") != run.id
                            or not is_id(run.payload.get("root_feedback_id"))
                            or run.payload.get("request_event_id") is not None
                        ):
                            raise Blocked("automatic_timeline_requires_manual_feedback")
                        session_windows = self.transcript_allowed_windows
                        scoped_transcript_guard = ScopedTranscriptGuard()
                    elif command.kind == "reextract":
                        session_windows = self._course_reextract_windows(command.interval)
                        if (
                            run.payload.get("event_id") != run.id
                            or not is_id(run.payload.get("root_feedback_id"))
                            or run.payload.get("request_event_id") is not None
                        ):
                            raise Blocked("course_reextract_requires_manual_feedback")
                        assert self.course_reextract_draft is not None
                        course_draft_guard = CourseDraftGuard(
                            self.course_reextract_draft,
                            self.course_draft_spans,
                            timeline=self.course_timeline,
                        )
                        scoped_transcript_guard = ScopedTranscriptGuard()
                    else:
                        session_windows = self._course_windows(command.interval)
                    course_max_calls = COURSE_MAX_CALLS_PER_WINDOW * len(session_windows)
                    lease_seconds = max(
                        lease_seconds,
                        course_max_calls * self.course_call_seconds
                        + TRANSCRIPT_LEASE_OVERHEAD_SECONDS,
                    )
            except Blocked as exc:
                self._finish(run, context, "blocked", str(exc))
                return
        if self._refresh_scheduled(run):
            return
        lease_until = self.clock() + timedelta(seconds=lease_seconds)
        payload = {
            **run.payload,
            "status": "running",
            "gateway_calls": run.payload.get("gateway_calls", []),
            "attempts": run.payload["attempts"] + 1,
            "lease_until": format_utc(lease_until),
        }
        claim = self.storage.write(
            [PutSystemRecord("extraction_run", run.id, payload, expected_revision=run.revision)],
            context,
        )
        if not claim.ok:
            return

        def check_lease() -> None:
            if self.clock() >= lease_until:
                raise Blocked("session_run_lease_expired")

        # The claim revision is the fencing token; another recovery cannot commit our results.
        tracker = DurableCalls(
            self.storage,
            self.gateway,
            run.id,
            payload,
            run.revision + 1,
            context,
            max_calls=course_max_calls,
            max_request_bytes=COURSE_MAX_REQUEST_BYTES if session_windows else None,
            check_lease=check_lease if session_windows else None,
        )
        calls = []

        def guard_send() -> None:
            if automatic_timeline is not None and (
                automatic_timeline.prewrite_pins
                or automatic_timeline.gaps
                or automatic_timeline.asr_gaps
            ):
                # Keep the existing guards on each admission, not just once
                # per wave. This remains on the singleton writer's thread.
                self._refuse_automatic_timeline_replay(automatic_timeline, run)
                automatic_timeline.live_targets(self.storage, session_windows)
                automatic_timeline.empty_success_history_guard(self.storage)
                if automatic_timeline.gaps or automatic_timeline.asr_gaps:
                    assert scoped_transcript_guard is not None
                    scoped_transcript_guard.live_raw_assertion(self.storage, session_windows)
                if legacy := automatic_timeline.legacy_review_pin:
                    # Same single-writer fence as the intent. Persist the trial
                    # marker before the first send, including if its outcome is unknown.
                    tracker.payload = {**tracker.payload, _LEGACY_WINDOW_TRIAL: legacy.run_id}
            if (
                self.transcript_only
                and not session_windows
                and len(tracker.payload["gateway_calls"]) >= self.transcript_max_calls
            ):
                raise TranscriptCallLimitExceeded

        def make_call(purpose: str, data: dict[str, Any], model: type[BaseModel]) -> Call:
            return build_call(
                model,
                purpose=purpose,
                data=data,
                run_id=run.id,
                root=tracker.context.root_feedback_id,
                depth=tracker.context.depth,
                request_event_id=tracker.context.request_event_id,
                additional_causes=tracker.context.additional_causes,
                max_input_chars=self.max_input_chars,
            )

        def ask(purpose: str, data: dict[str, Any], model: type[BaseModel]) -> BaseModel:
            guard_send()
            if (
                automatic_timeline is not None
                and (automatic_timeline.gaps or automatic_timeline.asr_gaps)
                and purpose in {"generation", "consolidation"}
            ):
                data = {
                    **data,
                    "supplemental_instructions": (
                        data["supplemental_instructions"]
                        + " Summarize only this observed fragment. Do not treat its "
                        "edges as an activity's start or end or infer continuity "
                        "beyond the supplied evidence."
                    ),
                }
            if session_windows and purpose == "session_fragments" and not data["evidence"]:
                # Feed the compiler its empty result locally. Empty/blank windows
                # still block the whole plan; sending them cannot recover evidence.
                return model.model_validate({"fragments": []})
            output = model.model_validate(tracker.complete(make_call(purpose, data, model)))
            if not self.transcript_only:
                calls.append(
                    {"purpose": purpose, "input": data, "output": output.model_dump(mode="json")}
                )
            return output

        def review_wave(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            def call_at(index: int) -> Call:
                guard_send()
                return make_call(WINDOW_REVIEW_PURPOSE, inputs[index], HostedWindowReview)

            return tracker.complete_wave(call_at, width=len(inputs))

        business_ready = False
        try:
            command = Command.model_validate(payload["command"])
            if automatic_timeline is not None:
                self._refuse_automatic_timeline_replay(automatic_timeline, run)
                automatic_timeline.empty_success_history_guard(self.storage, scan=True)
            elif course_draft_guard is not None:
                assert command.interval is not None
                # Gateway audit reconciliation can take time, so do not hold a
                # snapshot lease across it. This is the singleton ④ writer.
                self._check_course_reextract_predecessors(command.interval, session_windows, run)
            with ExitStack() as snapshots:
                # The transcript snapshot profile still materializes all raw rows
                # visible to the identity in ①. Guarded paths can instead
                # use per-window queries and revision fencing, with ④ singleton.
                reader = (
                    self.storage
                    if scoped_transcript_guard is not None
                    else snapshots.enter_context(self.storage.snapshot())
                )
                if command.kind in {"extract", "reextract"}:
                    if (
                        self.transcript_only
                        and not session_windows
                        and self.transcript_allowed_windows
                        and (
                            command.kind != "extract"
                            or command.interval not in self.transcript_allowed_windows
                        )
                    ):
                        raise Blocked("transcript_interval_not_allowed")
                    spec = self.storage.registry.data_types.get("rule_selection")
                    if (
                        spec is None
                        or spec.kind != "semantic"
                        or spec.unique_fields != ("activity_id",)
                        or spec.writer != "extraction"
                        or not spec.requires_dependencies
                        or not spec.supports_reevaluation
                    ):
                        raise Blocked("rule_selection_registration_required")
                    if session_windows and scoped_transcript_guard is None:
                        self._refuse_course_overlaps(reader, session_windows)
                        self._refuse_course_replay(reader, session_windows, run.id)
                    ops, detail = extract_plan(
                        reader,
                        command,
                        run.id,
                        ask,
                        activity_min_duration_minutes=self.activity_min_duration_minutes,
                        transcript_only=self.transcript_only,
                        transcript_summary_rule_id=self.transcript_summary_rule_id,
                        release_snapshot=snapshots.close
                        if scoped_transcript_guard is None
                        else None,
                        session_windows=session_windows,
                        course_blocks=self.course_blocks,
                        course_draft_guard=course_draft_guard,
                        scoped_transcript_guard=scoped_transcript_guard,
                        automatic_timeline=automatic_timeline,
                        timeline_max_calls=course_max_calls,
                        timeline_review_wave=review_wave,
                        timeline_review_concurrency=self.transcript_window_review_concurrency,
                    )
                elif command.kind in {"apply_rule", "extract_once"}:
                    ops, detail = apply_plan(reader, command, run.id, ask)
                elif command.kind == "reevaluate":
                    request_id = context.request_event_id
                    assert request_id is not None and command.target_id is not None
                    spec = self.storage.registry.data_types.get(command.target_type)
                    if spec is None or not spec.supports_reevaluation:
                        raise Blocked(command.target_type + "_reevaluation_not_enabled")
                    if reader.get_reevaluation_result(request_id) is not None:
                        raise Blocked("reevaluation_already_completed")
                    discovered = discover_rules(reader, command)
                    # Keep admission's original additional_causes intact; discoveries
                    # are independent execution context, persisted before any model call.
                    context = Context(
                        context.root_feedback_id,
                        context.depth,
                        request_id,
                        (
                            *Context(
                                payload["root_feedback_id"],
                                payload["depth"],
                                request_id,
                                payload.get("additional_causes", ()),
                            ).additional_causes,
                            *discovered.traces,
                        ),
                    )
                    tracker.context = context
                    if discovered.causes or tracker.payload.get("extra_causes"):
                        tracker.save(
                            {
                                **tracker.payload,
                                "extra_causes": [
                                    c.model_dump(mode="json") for c in discovered.causes
                                ],
                                "execution_additional_causes": [
                                    t.model_dump(mode="json") for t in context.additional_causes
                                ],
                            }
                        )
                    if command.target_type == "rule_selection":
                        assert command.target_id is not None
                        plan = reselect_plan(reader, command.target_id, run.id, ask)
                    else:
                        plan = rejudge_plan(reader, command, ask)
                    ops, detail = plan.ops, plan.detail
                    ops.extend(
                        [
                            FinishReevaluation(
                                ReevaluationResult(
                                    request_event_id=request_id,
                                    target_type=command.target_type,
                                    target_id=command.target_id,
                                    outcome=plan.outcome,
                                    before_version=plan.before,
                                    after_version=plan.after,
                                    extra_causes=tuple(discovered.causes),
                                    reason_code=plan.reason_code,
                                    disposition=plan.disposition,
                                    root_feedback_id=context.root_feedback_id,
                                    depth=context.depth,
                                    additional_causes=context.additional_causes,
                                    completed_at=max(
                                        self.clock(),
                                        datetime.fromisoformat(payload["request_occurred_at"]),
                                    ),
                                )
                            ),
                            MarkProcessed(COMPLETION_CONSUMER, request_id),
                        ]
                    )
                elif command.kind.startswith("rule_"):
                    ops, detail = rule_operations(reader, command), {}
                else:
                    ops, detail = suggestion_operations(reader, command), {}
            business_ready = True
            if session_windows:
                # Storage's collection guards must also match AFTER the batch,
                # so it cannot assert an activity revision and create activities
                # together. Recheck live, under the single-worker host lock;
                # the atomic batch still fences the run and checks rule/deps.
                if scoped_transcript_guard is not None:
                    ops.append(
                        scoped_transcript_guard.live_raw_assertion(self.storage, session_windows)
                    )
                if automatic_timeline is not None:
                    self._refuse_automatic_timeline_replay(automatic_timeline, run)
                    if automatic_timeline.strict_replacement:
                        # Capture before checking family protection. A change at any
                        # later point makes the no-action atomic batch conflict.
                        ops.append(
                            AssertTypeRevision(
                                "action_suggestion", self.storage.type_revision("action_suggestion")
                            )
                        )
                    live = automatic_timeline.live_targets(self.storage, session_windows)
                    automatic_timeline.empty_success_history_guard(self.storage)
                    if automatic_timeline.replacing:
                        if live.preview_versions != command.preview:
                            raise Blocked("reextract_preview_changed")
                        detail["course_replacement"] = True
                elif course_draft_guard is not None:
                    assert command.interval is not None
                    expected = {
                        (op.data_type, op.id): op.expected_version
                        for op in ops
                        if isinstance(op, Supersede)
                    }
                    live = course_replacement_targets(
                        self.storage, command.interval, session_windows
                    )
                    if course_draft_guard.timeline is not None:
                        course_draft_guard.timeline.check_pins(live)
                        detail["reviewed_timeline"] = True
                    if live.versions != expected or live.preview_versions != command.preview:
                        raise Blocked("reextract_preview_changed")
                    detail["course_replacement"] = True
                else:
                    coverage = session_windows
                    if self.course_blocks:
                        coverage += self._bridged_course_gaps(session_windows, ops)
                    self._refuse_course_overlaps(self.storage, coverage)
                check_lease()
            final = {
                **tracker.payload,
                "status": "succeeded",
                "error": None,
                "reevaluation_outcome": detail.get("outcome")
                if command.kind == "reevaluate"
                else None,
                "detail": detail,
                "model_calls": calls,
                "lease_until": None,
            }
            result = self.storage.write(
                [
                    *ops,
                    PutSystemRecord(
                        "extraction_run", run.id, final, expected_revision=tracker.revision
                    ),
                ],
                context,
            )
            if not result.ok:
                status, error = "retry_wait", "storage_conflict"
            else:
                return
        except Waiting:
            expired = (self.clock() - datetime.fromisoformat(payload["created_at"])).total_seconds()
            status = "failed" if expired >= self.wait_seconds else "waiting_attribution"
            error = "attributions_pending"
        except InvalidHostedWindowReview as exc:
            status, error = "blocked", str(exc)
            tracker.payload = {
                **tracker.payload,
                "detail": {"automatic_timeline_review_failure": exc.detail},
            }
        except SessionCoverageBlocked as exc:
            status, error = "blocked", str(exc)
            tracker.payload = {
                **tracker.payload,
                "detail": {**exc.detail, "unclassified_rows": exc.unclassified_rows},
            }
        except Blocked as exc:
            status, error = "blocked", str(exc)
        except TranscriptCallLimitExceeded:
            status, error = "failed", "transcript_max_calls_exceeded"
        except Exception as exc:
            # No exception bodies: model responses can contain private evidence or credentials.
            status = "failed" if isinstance(exc, (ValueError, ValidationError)) else "retry_wait"
            error = type(exc).__name__
            if business_ready and tracker.payload.get("gateway_calls"):
                # Even a known write rejection must not convert valid returned output
                # into a failed Finish or authorize recomputation with a fresh call ID.
                status, error = "blocked", "gateway_result_not_committed"

        current = run.model_copy(
            update={
                "revision": tracker.revision,
                "payload": {**tracker.payload, "model_calls": calls},
            }
        )
        self._finish(current, context, status, error)

    def _finish(self, run: SystemRecord, context: Context, status: str, error: str) -> None:
        payload = run.payload
        barrier = _retry_barrier(run)
        if barrier == "gateway_outcome_unknown" or (
            barrier and status in {"retry_wait", "waiting_attribution"}
        ):
            status, error = "blocked", barrier
        if status == "retry_wait" and payload["attempts"] >= self.max_attempts:
            status = "failed"
        final = {
            **payload,
            "status": status,
            "error": error,
            "lease_until": None,
            "retry_at": format_utc(self.clock() + timedelta(seconds=self.retry_seconds)),
        }
        ops: list[WriteOp] = []
        request_id = context.request_event_id
        if (
            status == "failed"
            and request_id
            and barrier
            not in {
                "gateway_outcome_unknown",
                "legacy_completion_id_collision",
                "legacy_gateway_outcome_untracked",
                "reevaluation_request_required",
            }
        ):
            command = Command.model_validate(payload["command"])
            target = self.storage.get(command.target_id) if command.target_id else None
            version = target.version if isinstance(target, SemanticRecord) else None
            assert command.target_id is not None
            ops.extend(
                [
                    FinishReevaluation(
                        ReevaluationResult(
                            request_event_id=request_id,
                            target_type=command.target_type,
                            target_id=command.target_id,
                            outcome="failed",
                            before_version=version,
                            after_version=version,
                            error_code="extraction_failed",
                            extra_causes=tuple(
                                Cause.model_validate(c) for c in payload.get("extra_causes", [])
                            ),
                            root_feedback_id=context.root_feedback_id,
                            depth=context.depth,
                            additional_causes=context.additional_causes,
                            completed_at=max(
                                self.clock(), datetime.fromisoformat(payload["request_occurred_at"])
                            ),
                        )
                    ),
                    MarkProcessed(COMPLETION_CONSUMER, request_id),
                ]
            )
            final["reevaluation_outcome"] = "failed"
        result = self.storage.write(
            [
                *ops,
                PutSystemRecord("extraction_run", run.id, final, expected_revision=run.revision),
            ],
            context,
        )
        if not result.ok:
            return
        if barrier == "gateway_outcome_unknown":
            # Best-effort audit repair; a 404 or another transport error still leaves
            # the task blocked. Operators may repeat audit-only reconcile_run later.
            with suppress(Exception):
                self.reconcile_run(run.id)
        logger.info(
            json.dumps(
                {
                    "module": "extraction",
                    "event_id": payload.get("event_id"),
                    "root_feedback_id": payload["root_feedback_id"],
                    "depth": payload["depth"],
                    "run_id": run.id,
                    "status": status,
                    "error": error,
                }
            )
        )
