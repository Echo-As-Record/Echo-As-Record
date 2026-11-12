"""Durable-in-storage job orchestration. A deployment runs one extraction worker."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ear_contracts import (
    DataChanged,
    Event,
    RawRecord,
    ReevaluationRequested,
    ReevaluationResult,
    SystemRecord,
    format_utc,
)
from ear_storage_client import (
    Context,
    FinishReevaluation,
    MarkProcessed,
    PutSystemRecord,
    Query,
    StorageClient,
    WriteOp,
    iter_query,
)
from pydantic import BaseModel, ValidationError

from ear_extraction.call_safety import DurableCalls
from ear_extraction.domain import Blocked, Command, Interval, Waiting, now_utc
from ear_extraction.model_io import ModelGateway, invoke
from ear_extraction.planning import apply_plan, extract_plan, preview_reextract, rejudge_plan
from ear_extraction.rules import rule_operations
from ear_extraction.scheduler import uncovered
from ear_extraction.suggestions import suggestion_operations

logger = logging.getLogger(__name__)
CONSUMER = "extraction.jobs"
COMPLETION_CONSUMER = "extraction.completion"
MODEL_COMMANDS = {"extract", "reextract", "apply_rule", "extract_once", "reevaluate"}
TERMINAL = {"succeeded", "failed", "blocked", "superseded"}


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
        if calls[-1]["state"] == "sending":
            return "gateway_outcome_unknown"
        return "gateway_result_not_committed"
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
    ):
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
            if event.target_type != "artifact":
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
            payload.get("additional_causes", ()),
        )

    @staticmethod
    def _held_range(run: SystemRecord) -> bool:
        # These ranges are NOT successful coverage. Do not let a later, larger timer
        # interval create a different run ID and implicitly repeat an uncertain send.
        return run.payload["status"] in {"blocked", "failed"} and _retry_barrier(run) is not None

    def reconcile_run(self, run_id: str) -> dict[str, Any]:
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
        receipt = reconcile(calls[-1]["call_id"])
        if not isinstance(receipt, dict):
            raise ValueError("invalid_reconcile_receipt")
        return {
            "call_id": calls[-1]["call_id"],
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
        if self._refresh_scheduled(run):
            return
        payload = {
            **run.payload,
            "status": "running",
            "gateway_calls": run.payload.get("gateway_calls", []),
            "attempts": run.payload["attempts"] + 1,
            "lease_until": format_utc(self.clock() + timedelta(seconds=self.lease_seconds)),
        }
        claim = self.storage.write(
            [PutSystemRecord("extraction_run", run.id, payload, expected_revision=run.revision)],
            context,
        )
        if not claim.ok:
            return
        # The claim revision is the fencing token; another recovery cannot commit our results.
        tracker = DurableCalls(
            self.storage, self.gateway, run.id, payload, run.revision + 1, context
        )
        calls = []

        def ask(purpose: str, data: dict[str, Any], model: type[BaseModel]) -> BaseModel:
            output = invoke(
                tracker,
                model,
                purpose=purpose,
                data=data,
                run_id=run.id,
                root=context.root_feedback_id,
                depth=context.depth,
                request_event_id=context.request_event_id,
                additional_causes=context.additional_causes,
                max_input_chars=self.max_input_chars,
            )
            calls.append(
                {"purpose": purpose, "input": data, "output": output.model_dump(mode="json")}
            )
            return output

        try:
            command = Command.model_validate(payload["command"])
            with self.storage.snapshot() as reader:
                if command.kind in {"extract", "reextract"}:
                    ops, detail = extract_plan(reader, command, run.id, ask)
                elif command.kind in {"apply_rule", "extract_once"}:
                    ops, detail = apply_plan(reader, command, run.id, ask)
                elif command.kind == "reevaluate":
                    request_id = context.request_event_id
                    assert request_id is not None
                    registry = self.storage.registry
                    if not registry.data_types["artifact"].supports_reevaluation:
                        raise Blocked("artifact_reevaluation_not_enabled")
                    if reader.get_reevaluation_result(request_id) is not None:
                        raise Blocked("reevaluation_already_completed")
                    ops, detail, before, outcome = rejudge_plan(reader, command, ask)
                    ops.extend(
                        [
                            FinishReevaluation(
                                ReevaluationResult(
                                    request_event_id=request_id,
                                    target_type="artifact",
                                    target_id=detail["artifact_id"],
                                    outcome=outcome,
                                    before_version=before,
                                    after_version=before + (outcome == "changed"),
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
            final = {
                **tracker.payload,
                "status": "succeeded",
                "error": None,
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
        except Blocked as exc:
            status, error = "blocked", str(exc)
        except Exception as exc:
            # No exception bodies: model responses can contain private evidence or credentials.
            status = "failed" if isinstance(exc, (ValueError, ValidationError)) else "retry_wait"
            error = type(exc).__name__

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
        if barrier == "gateway_outcome_unknown" or (barrier and status == "retry_wait"):
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
        result = self.storage.write(
            [PutSystemRecord("extraction_run", run.id, final, expected_revision=run.revision)],
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
