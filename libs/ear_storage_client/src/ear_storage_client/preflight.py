"""Side-effect-free, live caller preflight for a committed reevaluation model call.

No payloads, completion markers, leases or transferable authorization tickets.
A successful response is only a point-in-time check; recheck before each attempt.
The pure causal validator is also used by terminal llm_call writes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Annotated, Protocol

from ear_contracts import Cause, LLMCallRecord, Record, ReevaluationRequested, Registry, TracedCause
from ear_contracts.base import Id, ModuleName, RootFeedbackId, UtcDatetime, utcnow
from ear_contracts.events import unique_traces
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from ear_storage_client.errors import InvalidRequest, PermissionDenied


class LLMCallPreflight(BaseModel):
    """Closed request and canonical response; contains causal metadata only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    caller_module: ModuleName
    request_event_id: Id
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=0, strict=True)
    started_at: UtcDatetime
    additional_causes: Annotated[
        tuple[TracedCause, ...], Field(max_length=257), AfterValidator(unique_traces)
    ] = ()

    @model_validator(mode="before")
    @classmethod
    def strict_trace(cls, data):
        if isinstance(data, dict):
            # Reuse the terminal contract's strict nested fields, versions, timestamps
            # and unique origins BEFORE pydantic could coerce a boolean/string version.
            LLMCallRecord.model_validate(
                {
                    **data,
                    "purpose": "preflight",
                    "ended_at": data.get("started_at"),
                    "status": "success",
                }
            )
        return data


class LLMPreflightClient(Protocol):
    """Optional live-client capability, deliberately NOT available on snapshots."""

    def preflight_llm_call(self, call: LLMCallPreflight) -> LLMCallPreflight: ...


def validate_cause(cause: Cause, get_version: Callable[[str, int], Record | None]) -> None:
    """Same actual typed-version check for batch writes and preflight."""
    if cause.model_extra:
        raise InvalidRequest("unknown causal write fields")
    for version in (cause.old_version, cause.new_version):
        if version is None:
            continue
        record = get_version(cause.data_id, version)
        if record is None or record.data_type != cause.data_type:
            raise InvalidRequest("cause must reference an actual typed version")


def canonical_call_causes(
    *,
    request_event_id: str | None,
    root_feedback_id: str,
    depth: int,
    started_at: datetime,
    additional_causes: Sequence[TracedCause],
    request: ReevaluationRequested | None,
    get_version: Callable[[str, int], Record | None],
) -> tuple[TracedCause, ...]:
    """Existing llm_call lineage rules, shared without creating a terminal record."""
    traces = list(additional_causes)
    if request_event_id is not None:
        if request is None or request.event_id != request_event_id:
            raise InvalidRequest("call requires a committed reevaluation request")
        if (root_feedback_id, depth) != (request.root_feedback_id, request.depth) or any(
            t not in traces for t in request.additional_causes
        ):
            raise InvalidRequest("call must retain request root/depth and all origins")
        if started_at < request.occurred_at:
            raise InvalidRequest("call predates request")
        primary = TracedCause(
            cause=request.cause, root_feedback_id=request.root_feedback_id, depth=request.depth
        )
        if primary not in traces:
            traces.append(primary)
    for trace in traces:
        validate_cause(trace.cause, get_version)
    return tuple(traces)


def validate_preflight(
    call: LLMCallPreflight,
    *,
    module: str,
    registry: Registry,
    request: ReevaluationRequested | None,
    get: Callable[[str], Record | None],
    get_version: Callable[[str, int], Record | None],
    require_read: Callable[[Iterable[str]], None],
) -> LLMCallPreflight:
    """Caller must supply a committed request and stable, authorized record lookups.

    The authenticated service holds its existing admission gate for the whole check.
    MemoryStorage uses its committed immutable state, and is TEST-ONLY (no credentials).
    """
    if call.caller_module != module:
        raise PermissionDenied("access denied")
    traces = list(call.additional_causes)
    types = {t.cause.data_type for t in traces}
    roots = {call.root_feedback_id, *(t.root_feedback_id for t in traces)}
    if request is not None:
        types.update((request.target_type, request.cause.data_type))
        types.update(t.cause.data_type for t in request.additional_causes)
        roots.update(
            (request.root_feedback_id, *(t.root_feedback_id for t in request.additional_causes))
        )
    if roots - {"system"}:
        types.add("feedback")
    require_read(types)  # No request/target details before ALL declared grants pass.
    if request is None:
        raise InvalidRequest("call requires a committed reevaluation request")
    spec = registry.data_types.get(request.target_type)
    if spec is None or spec.writer != module:
        raise PermissionDenied("access denied")
    target = get(request.target_id)
    if target is None or target.data_type != request.target_type:
        raise InvalidRequest("invalid call target")
    if call.started_at > utcnow():
        raise InvalidRequest("call starts in the future")
    canonical = canonical_call_causes(
        request_event_id=call.request_event_id,
        root_feedback_id=call.root_feedback_id,
        depth=call.depth,
        started_at=call.started_at,
        additional_causes=call.additional_causes,
        request=request,
        get_version=get_version,
    )
    for root in roots - {"system"}:
        record = get(root)
        if record is None or record.data_type != "feedback":
            raise InvalidRequest("invalid call root")
    return LLMCallPreflight.model_validate({**call.model_dump(), "additional_causes": canonical})
