"""Durable-in-storage job orchestration. A deployment runs one extraction worker."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ear_contracts import (
    DataChanged,
    Event,
    RawRecord,
    ReevaluationRequested,
    SystemRecord,
    format_utc,
)
from ear_storage_client import (
    Context,
    MarkProcessed,
    PutSystemRecord,
    Query,
    StorageClient,
    WriteOp,
    iter_query,
)
from pydantic import BaseModel, ValidationError

from ear_extraction.domain import Blocked, Command, Interval, Waiting, now_utc
from ear_extraction.model_io import ModelGateway, invoke
from ear_extraction.planning import apply_plan, extract_plan, preview_reextract, rejudge_plan
from ear_extraction.rules import rule_operations
from ear_extraction.scheduler import uncovered
from ear_extraction.suggestions import suggestion_operations

logger = logging.getLogger(__name__)
CONSUMER = "extraction.jobs"
TERMINAL = {"succeeded", "failed", "blocked", "superseded"}


def schedule_id(interval: Interval) -> str:
    """Stable UUIDv7 using interval start time and a digest, for schedule admission dedup."""
    millis = int(interval.start.timestamp() * 1000)
    digest = int.from_bytes(
        hashlib.sha256(interval.model_dump_json().encode()).digest()[:10], "big"
    )
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 7 << 76 | ((digest >> 62) & 0xFFF) << 64 | 2 << 62 | (digest & ((1 << 62) - 1))
    return str(UUID(int=value))


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
        capture_model_content: bool = False,
    ):
        if storage.module != "extraction":
            raise ValueError("extraction_storage_client_required")
        if min(max_attempts, retry_seconds, wait_seconds, lease_seconds, max_input_chars) <= 0:
            raise ValueError("positive_limits_required")
        self.storage, self.gateway, self.clock = storage, gateway, clock
        self.capture_model_content = capture_model_content
        self.max_attempts, self.retry_seconds, self.wait_seconds = (
            max_attempts,
            retry_seconds,
            wait_seconds,
        )
        self.lease_seconds, self.max_input_chars = lease_seconds, max_input_chars

    def subscribe(self) -> None:
        self.storage.subscribe(CONSUMER, self.handle_event)

    def handle_event(self, event: Event) -> None:
        context = Context.of(event)
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
            raw = {
                "kind": "reevaluate",
                "target_id": event.target_id,
                "description": event.change_description,
                "cause": event.cause.model_dump(mode="json"),
            }
        else:
            return
        self._admit(event.event_id, raw, context, event_id=event.event_id)

    def _admit(
        self,
        id: str,
        raw: dict,
        context: Context,
        *,
        event_id: str | None = None,
        scheduled: bool = False,
    ) -> None:
        existing = self.storage.get_system_record(id)
        if existing:
            if existing.data_type != "extraction_run":
                raise ValueError("extraction_run_id_collision")
            return
        error = None
        try:
            command = Command.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            command, error = {}, "invalid_command"
        payload = {
            "command": command,
            "status": "failed" if error else "queued",
            "error": error,
            "root_feedback_id": context.root_feedback_id,
            "depth": context.depth,
            "scheduled": scheduled,
            "attempts": 0,
            "created_at": format_utc(self.clock()),
            "event_id": event_id,
            "model_calls": [],
        }
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
        if result.conflict and result.conflict.reason not in {"already_processed", "id_exists"}:
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
            if r.payload["status"] not in TERMINAL
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
        if run is None or run.payload["status"] not in {"failed", "blocked"}:
            raise ValueError("run_not_retryable")
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
        return Context(run.payload["root_feedback_id"], run.payload["depth"])

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
        coverage = [
            Interval.model_validate(r.payload["command"]["interval"])
            for r in self.runs()
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
        if run is None or run.payload["status"] in TERMINAL:
            return
        deadline = run.payload.get(
            "lease_until" if run.payload["status"] == "running" else "retry_at"
        )
        if deadline and datetime.fromisoformat(deadline) > self.clock():
            return
        if self._refresh_scheduled(run):
            return
        context = self._context(run)
        payload = {
            **run.payload,
            "status": "running",
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
        revision = run.revision + 1
        calls = list(payload.get("model_calls", []))

        def ask(purpose: str, data: dict[str, Any], model: type[BaseModel]) -> BaseModel:
            started = time.monotonic()
            entry: dict[str, Any] = {
                "purpose": purpose,
                "module": "extraction",
                "run_id": run.id,
                "root_feedback_id": context.root_feedback_id,
                "depth": context.depth,
                "attempt": payload["attempts"],
                "request_id": run.id if payload["command"].get("kind") == "reevaluate" else None,
            }
            try:
                output = invoke(
                    self.gateway,
                    model,
                    purpose=purpose,
                    data=data,
                    run_id=run.id,
                    root=context.root_feedback_id,
                    depth=context.depth,
                    max_input_chars=self.max_input_chars,
                    request_id=entry["request_id"],
                )
                entry["status"] = "succeeded"
                if self.capture_model_content:
                    entry.update(input=data, output=output.model_dump(mode="json"))
                return output
            except Exception as exc:
                entry.update(status="failed", error=type(exc).__name__)
                raise
            finally:
                entry["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                calls.append(entry)

        try:
            command = Command.model_validate(payload["command"])
            with self.storage.snapshot() as reader:
                if command.kind in {"extract", "reextract"}:
                    ops, detail = extract_plan(reader, command, run.id, ask)
                elif command.kind in {"apply_rule", "extract_once"}:
                    ops, detail = apply_plan(reader, command, run.id, ask)
                elif command.kind == "reevaluate":
                    ops, detail = rejudge_plan(reader, command, ask)
                elif command.kind.startswith("rule_"):
                    ops, detail = rule_operations(reader, command), {}
                else:
                    ops, detail = suggestion_operations(reader, command), {}
            final = {
                **payload,
                "status": "succeeded",
                "error": None,
                "detail": detail,
                "completed_at": format_utc(self.clock()),
                "model_calls": calls,
                "lease_until": None,
            }
            if payload.get("event_id"):
                ops.append(MarkProcessed("extraction.completed", payload["event_id"]))
            result = self.storage.write(
                [
                    *ops,
                    PutSystemRecord("extraction_run", run.id, final, expected_revision=revision),
                ],
                context,
            )
            if not result.ok:
                self._finish(run.id, revision, payload, calls, "retry_wait", "storage_conflict")
        except Waiting:
            expired = (self.clock() - datetime.fromisoformat(payload["created_at"])).total_seconds()
            status = "failed" if expired >= self.wait_seconds else "waiting_attribution"
            self._finish(run.id, revision, payload, calls, status, "attributions_pending")
        except Blocked as exc:
            self._finish(run.id, revision, payload, calls, "blocked", str(exc))
        except Exception as exc:
            # No exception bodies: model responses can contain private evidence or credentials.
            status = "failed" if isinstance(exc, (ValueError, ValidationError)) else "retry_wait"
            self._finish(run.id, revision, payload, calls, status, type(exc).__name__)

    def _finish(
        self, id: str, revision: int, payload: dict, calls: list, status: str, error: str
    ) -> None:
        if status == "retry_wait" and payload["attempts"] >= self.max_attempts:
            status = "failed"
        final = {
            **payload,
            "status": status,
            "error": error,
            "model_calls": calls,
            "lease_until": None,
            "retry_at": format_utc(self.clock() + timedelta(seconds=self.retry_seconds)),
            "completed_at": format_utc(self.clock()) if status in TERMINAL else None,
        }
        self.storage.write(
            [PutSystemRecord("extraction_run", id, final, expected_revision=revision)],
            Context(payload["root_feedback_id"], payload["depth"]),
        )
        logger.info(
            json.dumps(
                {
                    "module": "extraction",
                    "event_id": payload.get("event_id"),
                    "root_feedback_id": payload["root_feedback_id"],
                    "depth": payload["depth"],
                    "run_id": id,
                    "status": status,
                    "error": error,
                }
            )
        )
