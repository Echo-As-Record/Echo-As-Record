"""Fail-closed call intents in ①, fenced by the extraction_run revision.

A sending intent is durable BEFORE invoking ⑦. Recovery never calls complete:
reconcile can repair audit metadata, but cannot recover the model output. This
bounded slice deliberately blocks rather than implementing output replay or a
risk-acknowledged new logical call. Requires a persistent ① for process recovery.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any

from ear_contracts import new_id
from ear_llm_gateway import CallRequest, GatewayClient
from ear_storage_client import Context, PutSystemRecord, StorageClient

from ear_extraction.domain import Blocked, Call, InputBudgetExceeded
from ear_extraction.model_io import ModelGateway


class TranscriptCallLimitExceeded(Exception):
    """The next send would exceed this transcript-only run's model call budget."""


@dataclass(frozen=True)
class _PreparedCall:
    call: Call
    request: CallRequest
    fingerprint: str


def _send(gateway: ModelGateway, prepared: _PreparedCall) -> dict[str, Any]:
    # No run tracker or Storage in this function. The production GatewayClient
    # only uses per-request state and a thread-shareable synchronous httpx.Client.
    try:
        if isinstance(gateway, GatewayClient):
            return gateway.complete(prepared.request)
        return gateway.complete(prepared.call)
    except Exception:
        # Non-200 responses and arbitrary port exceptions are not proof of no send.
        raise Blocked("gateway_outcome_unknown") from None


class DurableCalls:
    def __init__(
        self,
        storage: StorageClient,
        gateway: ModelGateway,
        run_id: str,
        payload: dict,
        revision: int,
        context: Context,
        *,
        max_calls: int | None = None,
        max_request_bytes: int | None = None,
        check_lease: Callable[[], None] | None = None,
    ):
        self.storage, self.gateway, self.run_id = storage, gateway, run_id
        self.payload, self.revision, self.context = payload, revision, context
        self._write_failed = False
        self.max_calls, self.max_request_bytes, self.check_lease = (
            max_calls,
            max_request_bytes,
            check_lease,
        )

    def save(self, payload: dict) -> None:
        # Keep the conservative intent locally even if the commit acknowledgment is lost.
        self.payload = payload
        try:
            result = self.storage.write(
                [
                    PutSystemRecord(
                        "extraction_run", self.run_id, payload, expected_revision=self.revision
                    )
                ],
                self.context,
            )
            if not result.ok:
                raise Blocked("run_fence_lost")
        except Exception:
            self._write_failed = True
            raise
        self.revision += 1

    def _prepare(self, call: Call) -> _PreparedCall:
        call = call.model_copy(update={"call_id": new_id()}, deep=True)
        # Include effective client defaults in the fingerprint, without storing the prompt.
        request = CallRequest.model_validate(
            {
                **call.model_dump(mode="json"),
                "model": getattr(self.gateway, "model", "default"),
                "system": getattr(self.gateway, "system", ""),
            }
        )
        body = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if self.max_request_bytes is not None and len(body) > self.max_request_bytes:
            raise InputBudgetExceeded("gateway_request_budget_exceeded")
        return _PreparedCall(call, request, hashlib.sha256(body).hexdigest())

    def _store_intent(self, prepared: _PreparedCall) -> int:
        previous = self.payload.get("gateway_calls", [])
        if self.max_calls is not None and len(previous) >= self.max_calls:
            raise TranscriptCallLimitExceeded
        if self.check_lease is not None:
            self.check_lease()
        intent = {
            "call_id": prepared.request.call_id,
            "purpose": prepared.request.purpose,
            "request_fingerprint": prepared.fingerprint,
            "state": "sending",
        }
        self.save({**self.payload, "gateway_calls": [*previous, intent]})
        # A slow intent commit must not let an expired claimant hand off a call.
        if self.check_lease is not None:
            self.check_lease()
        return len(previous)

    def _store_return(self, index: int) -> None:
        before = self.payload
        calls = list(before["gateway_calls"])
        calls[index] = {**calls[index], "state": "returned"}
        try:
            self.save({**before, "gateway_calls": calls})
        except Exception:
            # Do not turn an unacknowledged receipt into a returned intent in
            # the final write. If it did commit, its newer revision fences us.
            self.payload = before
            raise

    def complete(self, call: Call) -> dict[str, Any]:
        prepared = self._prepare(call)
        index = self._store_intent(prepared)
        output = _send(self.gateway, prepared)
        self._store_return(index)
        return output

    def complete_wave(self, call_at: Callable[[int], Call], *, width: int) -> list[dict[str, Any]]:
        """Admit at most width calls. Only the coordinator may touch the tracker."""
        if width == 1:
            return [self.complete(call_at(0))]
        assert width > 1 and isinstance(self.gateway, GatewayClient)
        gateway = self.gateway
        sent: list[tuple[int, Future[dict[str, Any]]]] = []
        admission_error: Exception | None = None
        receipt_error: Exception | None = None
        output: list[dict[str, Any]] = []
        handoff = Lock()
        unknown = False

        def send(prepared: _PreparedCall) -> dict[str, Any]:
            nonlocal unknown
            try:
                return _send(gateway, prepared)
            except BaseException:
                with handoff:
                    unknown = True
                raise

        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="timeline-review") as pool:
            try:
                for i in range(width):
                    with handoff:
                        if unknown:
                            break
                    prepared = self._prepare(call_at(i))  # Includes the coordinator's guards.
                    with handoff:
                        if unknown:
                            break
                    index = self._store_intent(prepared)
                    # Serialize the handoff with publishing unknown, never with
                    # Storage I/O. A durable but unhanded intent stays conservative.
                    with handoff:
                        if unknown:
                            break
                        sent.append((index, pool.submit(send, prepared)))
            except Exception as exc:
                admission_error = exc

            # Drain even if admission, lease, or a receipt write failed. CAS
            # each known return in plan order, leaving all other intents intact.
            for index, future in sent:
                try:
                    reply = future.result()
                except Exception:
                    continue
                if not self._write_failed:
                    try:
                        self._store_return(index)
                    except Exception as exc:
                        receipt_error = exc
                output.append(reply)

        if unknown:
            raise Blocked("gateway_outcome_unknown")
        if admission_error is not None:
            raise admission_error
        if receipt_error is not None:
            raise receipt_error
        return output
