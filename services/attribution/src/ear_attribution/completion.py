"""Use the existing, storage-validated Finish protocol, including no-op decisions."""

from datetime import UTC, datetime
from typing import Literal

from ear_contracts import ReevaluationRequested, ReevaluationResult, SemanticRecord
from ear_storage_client import FinishReevaluation


def finish(
    event: ReevaluationRequested,
    record: SemanticRecord,
    outcome: Literal["changed", "unchanged", "not_applicable", "failed"],
    *,
    reason: str | None = None,
    disposition: Literal["kept_protected", "terminated", "unavailable"] | None = None,
    error: str | None = None,
) -> FinishReevaluation:
    return FinishReevaluation(
        ReevaluationResult(
            request_event_id=event.event_id,
            target_type=event.target_type,
            target_id=event.target_id,
            outcome=outcome,
            before_version=record.version,
            after_version=record.version + int(outcome == "changed"),
            root_feedback_id=event.root_feedback_id,
            depth=event.depth,
            additional_causes=event.additional_causes,
            completed_at=max(datetime.now(UTC), event.occurred_at),
            reason_code=reason,
            disposition=disposition,
            error_code=error,
        )
    )
