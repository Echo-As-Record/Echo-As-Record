"""Call execution and recovery. The only writer of llm_call is gateway's ① client.

Admission/retry and result retrieval reauthenticate the caller, without caching.
Revocation is checked immediately before each provider attempt and before release
of the response. It cannot cancel a request already accepted by an external model.
Journal and ① are NOT one transaction; failures surface record_pending and can be
reconciled without a second model call. Replies are not durably cached by default.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ear_contracts import LLMCallRecord, LLMUsage, TracedCause
from ear_storage_client import (
    AuthenticationError,
    ConflictError,
    Context,
    InvalidRequest,
    LLMCallPreflight,
    PermissionDenied,
    PutSystemRecord,
    Query,
)
from ear_storage_client.model_body import ModelBodyPolicy
from ear_storage_client.protocol import StorageClient, iter_query
from ear_storage_client.remote import RemoteStorageClient

from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import (
    CallRequest,
    GatewayError,
    Provider,
    ProviderFailure,
    ProviderResult,
    RequestVerifier,
)
from ear_llm_gateway.output_json import OutputSchema


@dataclass(frozen=True)
class RecoveryBatch:
    processed: int
    saved: int
    quarantined: int


ClientFactory = Callable[[str], AbstractContextManager[StorageClient]]


def text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def bounded_utf8(value: str, budget: int) -> str:
    return value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


class Gateway:
    def __init__(
        self,
        *,
        callers: ClientFactory,
        writer: StorageClient,
        provider: Provider,
        journal: Journal,
        aliases: dict[str, str],
        verifier: RequestVerifier | None = None,
        body_policy: ModelBodyPolicy | None = None,
        global_rpm: int = 60,
        caller_rpm: int = 20,
        concurrency: int = 4,
        attempts: int = 2,
        timeout: float = 60,
        max_response_bytes: int = 1024 * 1024,
    ):
        if not aliases or any(not k or not v for k, v in aliases.items()):
            raise ValueError("invalid_aliases")
        if (
            any(
                type(n) is not int or n < 1
                for n in (
                    global_rpm,
                    caller_rpm,
                    concurrency,
                    max_response_bytes,
                )
            )
            or type(attempts) is not int
            or not 1 <= attempts <= 3
        ):
            raise ValueError("invalid_limits")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("invalid_timeout")
        for model in aliases.values():
            # Validate provider model identifiers without accepting arbitrary caller targets.
            LLMCallRecord(
                caller_module="llm_gateway",
                purpose="configuration",
                model=model,
                root_feedback_id="system",
                depth=0,
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                status="success",
            )
        if writer.module != "llm_gateway":
            raise ValueError("writer_must_be_llm_gateway")
        self.callers, self.writer, self.provider, self.journal = callers, writer, provider, journal
        self.aliases, self.verifier = dict(aliases), verifier
        self.body_policy = body_policy or ModelBodyPolicy()
        self.global_rpm, self.caller_rpm = global_rpm, caller_rpm
        self.attempts, self.timeout, self.max_response_bytes = attempts, timeout, max_response_bytes
        self._slots = threading.BoundedSemaphore(concurrency)
        self._persist_lock = threading.Lock()

    def _check(
        self, caller: StorageClient, call: CallRequest, started_at: datetime | None = None
    ) -> tuple[TracedCause, ...]:
        started_at = started_at or datetime.now(UTC)
        if caller.module != call.module:
            raise GatewayError(403, "caller_mismatch")
        traces = list(call.additional_causes)
        if call.request_event_id is not None:
            if self.verifier is None:
                preflight = getattr(caller, "preflight_llm_call", None)
                if not callable(preflight):
                    raise GatewayError(503, "request_preflight_unavailable")
                try:
                    canonical = preflight(
                        LLMCallPreflight(
                            caller_module=call.module,
                            request_event_id=call.request_event_id,
                            root_feedback_id=call.root_feedback_id,
                            depth=call.depth,
                            started_at=started_at,
                            additional_causes=call.additional_causes,
                        )
                    )
                except InvalidRequest:
                    raise GatewayError(403, "invalid_call_context") from None
                if not isinstance(canonical, LLMCallPreflight):
                    raise GatewayError(503, "invalid_storage_response")
                return canonical.additional_causes
            request = self.verifier.verify(caller, call)
            if (request.event_id, request.root_feedback_id, request.depth) != (
                call.request_event_id,
                call.root_feedback_id,
                call.depth,
            ) or any(t not in traces for t in request.additional_causes):
                raise GatewayError(403, "invalid_call_context")
            if request.occurred_at > started_at:
                raise GatewayError(403, "invalid_call_context")
            if caller.registry.get(request.target_type).writer != call.module:
                raise GatewayError(403, "invalid_call_context")
            target = caller.get(request.target_id)
            if target is None or target.data_type != request.target_type:
                raise GatewayError(403, "invalid_call_context")
            primary = TracedCause(
                cause=request.cause, root_feedback_id=request.root_feedback_id, depth=request.depth
            )
            if primary not in traces:
                traces.append(primary)
        elif call.depth != 0:
            raise GatewayError(403, "request_context_required")
        roots = {call.root_feedback_id, *(t.root_feedback_id for t in traces)} - {"system"}
        for root in roots:
            record = caller.get(root)
            if record is None or record.data_type != "feedback":
                raise GatewayError(403, "invalid_call_context")
        for trace in traces:
            for version in {trace.cause.old_version, trace.cause.new_version}:
                if version is None:
                    continue
                record = caller.get_version(trace.cause.data_id, version)
                if record is None or record.data_type != trace.cause.data_type:
                    raise GatewayError(403, "invalid_call_context")
        return tuple(traces)

    def _persist(self, call_id: str, module: str):
        with self._persist_lock:
            row = self.journal.get(call_id, module)
            if row is None:
                raise GatewayError(404, "call_not_found")
            if row["phase"] == "active":
                raise GatewayError(409, "call_in_progress")
            if row["phase"] == "saved":
                return
            if row["phase"] == "quarantined":
                raise GatewayError(409, "record_conflict")
            if row["phase"] != "pending":
                raise GatewayError(503, "journal_state_conflict")
            record = LLMCallRecord.model_validate_json(row["record"])
            # No re-plan/recompute. Replay exactly one immutable metadata record.
            # Gateway is NOT the reevaluation owner: the request ID belongs in
            # LLMCallRecord, NOT the business-owner write Context. ① validates
            # this association via its dedicated llm_call path.
            context = Context(record.root_feedback_id, record.depth)
            try:
                existing = self.writer.get_system_record(call_id)
                if existing is not None:
                    if existing.data_type != "llm_call" or (
                        LLMCallRecord.model_validate(existing.payload) != record
                    ):
                        raise GatewayError(409, "record_conflict")
                else:
                    ops = [PutSystemRecord("llm_call", call_id, record.model_dump(mode="json"))]
                    if isinstance(self.writer, RemoteStorageClient):
                        self.writer.write(ops, context, request_id=row["request_id"]).check()
                    else:
                        self.writer.write(ops, context).check()
                self.journal.saved(call_id)
            except (ConflictError, GatewayError, InvalidRequest, PermissionDenied) as exc:
                permanent = (
                    isinstance(exc, ConflictError) and exc.conflict.reason == "id_exists"
                ) or (isinstance(exc, GatewayError) and exc.code == "record_conflict")
                if isinstance(exc, (InvalidRequest, PermissionDenied)):
                    # An unreadable competing llm_call can fail immutable validation
                    # rather than return id_exists. Never infer absence from read ACLs.
                    # Occupied but unverifiable audits need operator resolution, NOT
                    # a successful receipt or an endless startup retry.
                    try:
                        check_id = getattr(self.writer, "call_id_occupied", None)
                        permanent = callable(check_id) and check_id(call_id) is True
                    except Exception:
                        permanent = False
                if permanent:
                    self.journal.quarantine(call_id)
                    logging.getLogger(__name__).error(
                        text(
                            {
                                "module": "llm_gateway",
                                "event_id": call_id,
                                "root_feedback_id": record.root_feedback_id,
                                "depth": record.depth,
                                "code": "audit_quarantined",
                                "reason": "record_conflict",
                            }
                        )
                    )
                    raise GatewayError(409, "record_conflict") from None
                raise GatewayError(503, "record_pending") from None
            except Exception:
                raise GatewayError(503, "record_pending") from None

    def complete(self, token: str, call: CallRequest) -> dict[str, Any]:
        model = self.aliases.get(call.model)
        if model is None:
            raise GatewayError(422, "unknown_model_alias")
        started = datetime.now(UTC)
        with self.callers(token) as caller:
            traces = self._check(caller, call, started)
            # Deterministic local refusal must not consume an ID, quota or attempt.
            try:
                OutputSchema(call.output_schema)
            except (ValueError, TypeError, RecursionError, OverflowError):
                raise GatewayError(422, "invalid_schema") from None
            admit = getattr(self.provider, "admit", None)
            if callable(admit):
                admit(call, model=model, timeout=self.timeout)
            # Do not expose cross-caller records, even if gateway can read them.
            if self.journal.get(call.call_id, call.module) is not None:
                raise GatewayError(409, "duplicate_call_id")
            # Ordinary reads may hide occupied IDs behind ACLs. Never fall back to
            # get/get_system_record when this trusted capability is absent/unavailable.
            check_id = getattr(self.writer, "call_id_occupied", None)
            if not callable(check_id):
                raise GatewayError(503, "call_id_check_unavailable")
            try:
                occupied = check_id(call.call_id)
                if type(occupied) is not bool:
                    raise ValueError
            except Exception:
                raise GatewayError(503, "call_id_check_unavailable") from None
            if occupied:
                raise GatewayError(409, "duplicate_call_id")
            # No global reservation: a concurrent storage writer may still win.
            # Such terminal ID conflicts are durably quarantined, never re-sent.
            if not self._slots.acquire(blocking=False):
                raise GatewayError(429, "concurrency_limited")
            try:
                return self._execute(caller, call, model, traces, started)
            finally:
                self._slots.release()

    def _execute(
        self,
        caller: StorageClient,
        call: CallRequest,
        model: str,
        traces: tuple[TracedCause, ...],
        started: datetime,
    ):
        record = LLMCallRecord(
            caller_module=call.module,
            purpose=call.purpose,
            model=model,
            root_feedback_id=call.root_feedback_id,
            depth=call.depth,
            request_event_id=call.request_event_id,
            additional_causes=traces,
            started_at=started,
            ended_at=started,
            status="failed",
            error_code="provider_outcome_unknown",
        )
        # All serialization errors happen before reserve/provider side effects.
        request_text = text(
            {"system": call.system, "data": call.data, "output_schema": call.output_schema}
        )
        self.journal.reserve(
            call.call_id, record, global_rpm=self.global_rpm, caller_rpm=self.caller_rpm
        )
        result = None
        code = "provider_outcome_unknown"
        try:
            for attempt in range(self.attempts):
                # Live preflight before EVERY attempt. SDK auth failures are not
                # unknown provider outcomes, and never permit another model call.
                try:
                    self._check(caller, call, started)
                except (AuthenticationError, PermissionDenied):
                    code, result = "caller_access_revoked", None
                    break
                except GatewayError as exc:
                    code = (
                        "caller_preflight_unavailable"
                        if exc.status >= 500
                        else "caller_access_revoked"
                    )
                    result = None
                    break
                except Exception:
                    code, result = "caller_preflight_unavailable", None
                    break
                try:
                    result = self.provider.complete(call, model=model, timeout=self.timeout)
                    if not isinstance(result, ProviderResult) or not isinstance(
                        result.output, dict
                    ):
                        raise ValueError("invalid_provider_output")
                    response_text = text(result.output)
                    if len(response_text.encode("utf-8")) > self.max_response_bytes:
                        raise ValueError("response_limit")
                    if result.usage is not None and not isinstance(result.usage, LLMUsage):
                        raise ValueError("invalid_provider_usage")
                    code = ""
                    break
                except ProviderFailure as exc:
                    code = "provider_rejected" if exc.safe_to_retry else "provider_outcome_unknown"
                    if not exc.safe_to_retry or attempt + 1 == self.attempts:
                        break
                    # No sleeps while holding an admission slot: tightly bounded retry.
            if code:
                result = None
        except GatewayError:
            code, result = "caller_access_revoked", None
        except Exception:
            code, result = "provider_outcome_unknown", None
        end = max(datetime.now(UTC), started)
        updates: dict[str, Any] = {
            "ended_at": end,
            "status": "success" if result else "failed",
            "error_code": None if result else code,
            "usage": result.usage if result else None,
            "latency_ms": (end - started).total_seconds() * 1000,
        }
        # Policy is deployment-only, never a request flag. Byte-bounded prefix capture.
        # Journals may contain opted-in bodies, so protect/retain the volume accordingly.
        policy = self.body_policy
        if policy.enabled and (
            policy.allowed_callers is None or call.module in policy.allowed_callers
        ):
            request_body = bounded_utf8(request_text, policy.max_bytes)
            remaining = policy.max_bytes - len(request_body.encode("utf-8"))
            updates["request_text"] = request_body
            if result is not None:
                updates["response_text"] = bounded_utf8(text(result.output), remaining)
        record = LLMCallRecord.model_validate({**record.model_dump(), **updates})
        policy.validate(record)
        self.journal.pending(call.call_id, record)
        self._persist(call.call_id, call.module)
        # If access changed during the model call, don't release sensitive output.
        self._check(caller, call, started)
        if result is None:
            raise GatewayError(502, code)
        return {
            "call_id": call.call_id,
            "output": result.output,
            "usage": result.usage.model_dump() if result.usage else None,
            "record_saved": True,
        }

    def recover_pending(self, *, limit: int = 100):
        """Compatibility count: only saved audits, never quarantines."""
        return self.recover_batch(limit=limit).saved

    def recover_batch(self, *, limit: int = 100) -> RecoveryBatch:
        """Deployment-only recovery; never expose it as an untrusted HTTP route.

        Only ① audit writes are replayed. No provider calls, reply delivery or
        business success is inferred. Failures retain pending rows and surface.
        """
        quarantined = self.journal.quarantined_count()
        if quarantined:
            logging.getLogger(__name__).error(
                text(
                    {
                        "module": "llm_gateway",
                        "event_id": None,
                        "root_feedback_id": "system",
                        "depth": 0,
                        "code": "audit_quarantine_requires_resolution",
                        "count": quarantined,
                    }
                )
            )
        recovered = quarantined_now = 0
        pending = self.journal.pending_ids(limit=limit)
        for call_id, module in pending:
            try:
                self._persist(call_id, module)
            except GatewayError as exc:
                if exc.code == "record_conflict":
                    quarantined_now += 1
                    continue  # Retained quarantine; never counts as saved/recovered.
                raise
            recovered += 1
        return RecoveryBatch(len(pending), recovered, quarantined_now)

    def reconcile(self, token: str, call_id: str):
        with self.callers(token) as caller:
            module = caller.module
            row = self.journal.get(call_id, module)
            if row is None:
                raise GatewayError(404, "call_not_found")
            # Never return bodies or causal references using the privileged writer.
            self._persist(call_id, module)
            record = LLMCallRecord.model_validate_json(row["record"])
            return {
                "call_id": call_id,
                "record_saved": True,
                "status": record.status,
                "error_code": record.error_code,
                "response_recoverable": False,
            }

    def stats(self, token: str, root: str | None = None):
        with self.callers(token) as caller:
            module = caller.module
            where = {"caller_module": module}
            totals: dict[str, Any] = {}
            # Caller SDK enforces CURRENT llm_call + ALL cause read grants.
            with caller.snapshot() as reader:
                for item in iter_query(reader, Query("llm_call", where=where, limit=1000)):
                    record = LLMCallRecord.model_validate(item.payload)
                    if record.caller_module != module:
                        raise GatewayError(503, "invalid_storage_response")
                    # A merged call is attributed once to EACH distinct root,
                    # not silently assigned to its legacy primary root only.
                    roots = {
                        record.root_feedback_id,
                        *(t.root_feedback_id for t in record.additional_causes),
                    }
                    for key in sorted(roots):
                        if root is not None and key != root:
                            continue
                        bucket = totals.setdefault(
                            key,
                            {
                                "calls": 0,
                                "failed": 0,
                                "usage": {
                                    name: {"known_tokens": 0, "unknown_calls": 0}
                                    for name in LLMUsage.model_fields
                                },
                            },
                        )
                        bucket["calls"] += 1
                        bucket["failed"] += record.status == "failed"
                        for name, values in bucket["usage"].items():
                            value = getattr(record.usage, name) if record.usage else None
                            if value is None:
                                values["unknown_calls"] += 1
                            else:
                                values["known_tokens"] += value
            return {"module": module, "roots": totals}
