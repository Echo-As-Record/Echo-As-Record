"""Logical model calls, produced by trusted llm_gateway.

LLMCallRecord is the SystemRecord payload: call_id is the envelope id, not a
second identity. Use PutSystemRecord("llm_call", call_id, payload); only terminal
success/failed are supported. Existing terminal content is immutable (an identical
expected_revision Put is unchanged); another logical call needs another id.
Storage retains request root/depth and all additional paths, and also persists the
request primary cause in additional_causes for normal read-grant enforcement.
caller_module/model are producer assertions, NOT authentication
proof; gateway authenticates its callers. No attempt-by-attempt requirement.
Only explicitly supplied request_text/response_text may be stored when the deployment
write policy allows the asserted caller; the default is off. These are plain text,
not arbitrary nested channels. Never supply API keys, credentials or headers.
Unknown fields (including include_body) are rejected. No automatic collection.
The registry capability is NOT a body switch. Reads use the existing type/cause ACL;
disabling writes does not hide or delete old bodies. No retention/cleanup; records persist until
a separately authorized retention facility exists. Never put secrets in codes.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from ear_contracts.base import Id, ModuleName, RootFeedbackId, SnakeCase, UtcDatetime
from ear_contracts.events import TracedCause, TraceFields, unique_traces


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class LLMCallRecord(TraceFields):
    """A terminal logical call; absent usage means unknown, not zero consumption."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    # One extra slot for the persisted request primary cause (256 inherited paths).
    additional_causes: Annotated[
        tuple[TracedCause, ...], Field(max_length=257), AfterValidator(unique_traces)
    ] = ()
    caller_module: ModuleName
    purpose: Annotated[SnakeCase, Field(max_length=128)]
    model: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@+-]*$"
    )
    started_at: UtcDatetime
    ended_at: UtcDatetime
    status: Literal["success", "failed"]
    usage: LLMUsage | None = None
    latency_ms: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)
    request_event_id: Id | None = None
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=0, strict=True)
    error_code: Annotated[SnakeCase, Field(max_length=128)] | None = None
    request_text: str | None = Field(
        default=None,
        strict=True,
        description="Explicit call text only; deployment write policy defaults off. No secrets.",
    )
    response_text: str | None = Field(
        default=None,
        strict=True,
        description="Explicit call text only; deployment write policy defaults off. No secrets.",
    )

    @model_serializer(mode="wrap")
    def omit_absent_bodies(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Keep legacy metadata wire shape, including explicit null body inputs.

        Use the v2 serializer API supported by the declared pydantic>=2.9 floor.
        """
        data = super()._omit_absent_trace(handler)
        for name in ("request_text", "response_text"):
            if data.get(name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="before")
    @classmethod
    def closed_traces(cls, data):
        if isinstance(data, dict):
            for name in ("started_at", "ended_at"):
                if name in data and not isinstance(data[name], str | datetime):
                    raise ValueError("timestamps require UTC datetime or RFC3339 text")
            for trace in data.get("additional_causes", ()):
                if hasattr(trace, "model_dump"):
                    trace = trace.model_dump()
                if isinstance(trace, dict):
                    if set(trace) - {"cause", "root_feedback_id", "depth"}:
                        raise ValueError("unknown trace fields")
                    cause = trace.get("cause", {})
                    if isinstance(cause, dict):
                        if set(cause) - {"data_type", "data_id", "old_version", "new_version"}:
                            raise ValueError("unknown cause fields")
                        for key in ("old_version", "new_version"):
                            value = cause.get(key)
                            if value is not None and type(value) is not int:
                                raise ValueError("cause versions must be integers")
        return data

    @model_validator(mode="after")
    def terminal(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        elapsed = (self.ended_at - self.started_at).total_seconds() * 1000
        if self.latency_ms is not None and not math.isclose(
            self.latency_ms, elapsed, rel_tol=0, abs_tol=0.000001
        ):
            raise ValueError("latency_ms differs from timestamps")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("only failed calls require an error_code")
        return self
