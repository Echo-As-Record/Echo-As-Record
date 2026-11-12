"""Private propagation payloads, NOT an approved cross-module contract.

Single worker only. Storage owns durability; MemoryStorage only demonstrates
worker reconstruction while the store survives, not database/process durability.
"""

from typing import Literal

from ear_contracts import DataChanged, ReevaluationResult, TracedCause, new_id
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "dependency_core_v1"


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_tag: str = SCHEMA
    record_kind: Literal["trigger"] = "trigger"
    id: str = Field(default_factory=new_id)
    event: DataChanged
    status: str = "open"
    reason: str | None = None
    summary: dict[str, int] = Field(default_factory=dict)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_tag: str = SCHEMA
    record_kind: Literal["step"] = "step"
    id: str = Field(default_factory=new_id)
    target_id: str
    target_type: str
    target_version: int
    generation: int = 1
    trigger_ids: list[str] = Field(default_factory=list)
    causes: list[TracedCause] = Field(default_factory=list)
    pending_causes: list[TracedCause] = Field(default_factory=list)
    absorbed_causes: list[TracedCause] = Field(default_factory=list)
    upstream_step_ids: list[str] = Field(default_factory=list)
    upstream_request_ids: list[str] = Field(default_factory=list)
    state: str = "pending"
    reason: str | None = None
    owner: str | None = None
    request_event_id: str | None = None
    # Local intent identity, NOT a remote write receipt. Memory write has no
    # request_id API; this slice cannot replay a remote transaction receipt.
    intent_id: str | None = None
    request_parent_id: str | None = None
    request_causes: list[TracedCause] = Field(default_factory=list)
    requested_at: float | None = None
    result: ReevaluationResult | None = None
    successor_id: str | None = None


def union[T](left: list[T], right: list[T]) -> list[T]:
    result = list(left)
    for value in right:
        if value not in result:
            result.append(value)
    return result


def event_causes(event: DataChanged) -> list[TracedCause]:
    from ear_contracts import Cause

    return union(
        [
            TracedCause(
                cause=Cause(
                    data_type=event.data_type,
                    data_id=event.data_id,
                    old_version=event.old_version,
                    new_version=event.new_version,
                ),
                root_feedback_id=event.root_feedback_id,
                depth=event.depth + 1,
            )
        ],
        [t.model_copy(update={"depth": t.depth + 1}) for t in event.additional_causes],
    )
