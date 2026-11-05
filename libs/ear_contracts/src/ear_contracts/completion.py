"""Terminal, executor-owned reevaluation metadata; never model prompts/responses.

The request event ID is also the immutable result record ID. A new attempt after
terminal failure requires a NEW request; retry exhaustion is a business decision.
not_applicable is not success: disposition explicitly describes the final target.
kept_protected/unavailable retain its exact version (unavailable can also be absent);
terminated requires an actual terminal version. Storage never infers protection or
changes business state on behalf of the owner. Cause remains forward compatible.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ear_contracts.base import DataType, Id, RootFeedbackId, SnakeCase, UtcDatetime
from ear_contracts.events import Cause


class ReevaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    request_event_id: Id
    target_type: DataType
    target_id: Id
    outcome: Literal["changed", "unchanged", "not_applicable", "failed"]
    before_version: int | None = Field(ge=1, strict=True)
    after_version: int | None = Field(ge=1, strict=True)
    extra_causes: tuple[Cause, ...] = ()
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=1, strict=True)
    completed_at: UtcDatetime
    error_code: SnakeCase | None = None
    reason_code: SnakeCase | None = None
    disposition: Literal["kept_protected", "terminated", "unavailable"] | None = None

    @model_validator(mode="after")
    def terminal_shape(self) -> Self:
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("failed requires error_code; other outcomes forbid it")
        if self.outcome == "not_applicable":
            if self.reason_code is None or self.disposition is None:
                raise ValueError("not_applicable requires reason_code and disposition")
        elif self.reason_code is not None or self.disposition is not None:
            raise ValueError("disposition/reason_code only for not_applicable")
        if self.outcome in {"failed", "unchanged"} and self.before_version != self.after_version:
            raise ValueError("unchanged/failed cannot change target version")
        if self.outcome == "unchanged" and self.after_version is None:
            raise ValueError("unchanged requires an existing active target")
        if self.outcome == "changed" and (
            self.after_version is None or self.before_version == self.after_version
        ):
            raise ValueError("changed requires a new target version")
        return self
