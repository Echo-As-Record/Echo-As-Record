"""Bounded action-v2 HTTP operations; no worker, thread, adapter discovery or URLs.

Trusted host: create_app(Settings(execution_action_v2=True, execution_journal=...),
                         adapter=trusted_adapter).
Review stays POST /v1/feedback. After actual approval, POST /v1/actions/{id}/prepare
with expected_version. GET .../execution is read-only and supplies both versions;
POST .../pump with those versions advances ONE turn. Run the real extraction
consumer separately between turns. POST .../recovery also requires both versions;
verify/reconcile only looks up, confirm_executed and retry need their explicit ack.
Neither accepted feedback nor a queued extraction_run authorizes an external call.
Keep the private single-owner journal across restarts. No automatic worker here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from ear_contracts import SemanticRecord
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ear_api.execution import ExecutionPump
from ear_api.models import MAX_REVISION
from ear_api.upstream import GatewayError

Version = Annotated[int, Field(strict=True, ge=1, le=MAX_REVISION)]


class PrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_version: Version


class PumpInput(PrepareInput):
    expected_revision: Version


class VerifyInput(PumpInput):
    operation: Literal["verify", "reconcile"]


class ConfirmInput(PumpInput):
    operation: Literal["confirm_executed"]
    acknowledge_executed: bool = Field(default=False, strict=True)


class RetryInput(PumpInput):
    operation: Literal["retry"]
    acknowledge_duplicate_risk: bool = Field(default=False, strict=True)


RECOVERY_INPUT = TypeAdapter(
    Annotated[VerifyInput | ConfirmInput | RetryInput, Field(discriminator="operation")]
)


def projection(service: ExecutionPump, suggestion_id: str) -> dict[str, object]:
    """No sync/delivery/lookup on GET. Authorization precedes all local disclosures."""
    with service.journal.lock:
        record = service.client.get(suggestion_id)
        if not isinstance(record, SemanticRecord) or record.data_type != "action_suggestion":
            raise GatewayError(404, "not_found")
        local = service.status(suggestion_id)
        recovery = None
        if service.journal.get(suggestion_id) is not None:
            recovery = {
                **service.recovery_request(suggestion_id),
                "http_execution_connected": True,
            }
        return {
            "suggestion_id": suggestion_id,
            "suggestion_version": record.version,
            "suggestion_state": record.payload.get("state"),
            "suggestion_status": record.status,
            **local,
            "expected_version": record.version,
            "expected_revision": local["journal_revision"],
            "execution_enabled": True,
            "verification_enabled": True,
            "recovery_protocol": "action-v2",
            "http_execution_connected": True,
            "automatic_worker_enabled": False,
            "recovery": recovery,
        }


def pump_once(service: ExecutionPump, suggestion_id: str, parsed: PumpInput) -> None:
    # Keep preconditions and the bounded pump in the SAME journal critical section.
    # Owner acknowledgement may change revision during pump, never before this check.
    with service.journal.lock:
        current = service.client.get(suggestion_id)
        if not isinstance(current, SemanticRecord) or current.data_type != "action_suggestion":
            raise GatewayError(404, "not_found")
        if current.version != parsed.expected_version:
            raise GatewayError(409, "suggestion_version_changed")
        entry = service.journal.get(suggestion_id)
        if entry is None:
            raise GatewayError(409, "execution_not_started")
        if entry.revision != parsed.expected_revision:
            raise GatewayError(409, "execution_revision_conflict")
        service.pump(suggestion_id)


def recover(
    service: ExecutionPump,
    suggestion_id: str,
    parsed: VerifyInput | ConfirmInput | RetryInput,
) -> None:
    versions = {
        "expected_version": parsed.expected_version,
        "expected_revision": parsed.expected_revision,
    }
    if isinstance(parsed, ConfirmInput):
        service.confirm_executed(
            suggestion_id, **versions, acknowledge_executed=parsed.acknowledge_executed
        )
    elif isinstance(parsed, RetryInput):
        service.retry(
            suggestion_id, **versions, acknowledge_duplicate_risk=parsed.acknowledge_duplicate_risk
        )
    else:
        service.reconcile(suggestion_id, **versions)
