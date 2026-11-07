"""Completion polling delegates to the Reader's authoritative, access-controlled index."""

from ear_contracts import ReevaluationResult, Registry, SystemRecord

from ear_storage_client.errors import InvalidRequest
from ear_storage_client.protocol import Reader


def reevaluation_result(reader: Reader, request_event_id: str) -> ReevaluationResult | None:
    method = getattr(reader, "get_reevaluation_result", None)
    if not callable(method):
        raise InvalidRequest("authoritative completion reads unavailable; upgrade the Reader")
    return reader.get_reevaluation_result(request_event_id)


def _indexed_result(
    registry: Registry, request_event_id: str, result_type: str, record: SystemRecord | None
) -> ReevaluationResult:
    """Internal validation AFTER a trusted completion-index lookup, never raw-read proof."""
    spec = registry.data_types.get(result_type)
    if (
        spec is None
        or not spec.reevaluation_result
        or record is None
        or record.data_type != result_type
        or record.writer != spec.writer
        or record.id != request_event_id
    ):
        raise InvalidRequest("invalid authoritative completion metadata")
    result = ReevaluationResult.model_validate(record.payload)
    if result.request_event_id != request_event_id:
        raise InvalidRequest("invalid completion identity")
    return result.model_copy(deep=True)
