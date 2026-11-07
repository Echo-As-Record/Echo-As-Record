"""Local gateway contract, deliberately not a claim about Octopus's wire protocol.

Deployment provides a bounded provider adapter. Only a positively known rejection
before execution may be retried. A timeout, broken connection, malformed reply or
adapter exception has UNKNOWN execution/usage and MUST NOT be retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol

from ear_contracts import LLMCallRecord, LLMUsage, ReevaluationRequested
from ear_contracts.base import Id, ModuleName, RootFeedbackId, SnakeCase
from ear_contracts.events import TracedCause
from ear_storage_client.protocol import StorageClient
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: Id
    module: ModuleName
    model: str = Field(
        default="default", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@+-]*$", max_length=256
    )
    purpose: Annotated[SnakeCase, Field(max_length=128)]
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=0, le=10000, strict=True)
    request_event_id: Id | None = None
    additional_causes: tuple[TracedCause, ...] = Field(default=(), max_length=256)
    run_id: Id | None = None
    system: str = Field(default="", max_length=131072, repr=False)
    data: dict[str, Any] = Field(repr=False)
    output_schema: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def closed_causes(self):
        # Reuse the existing strict nested-cause validation, never silently drop extras.
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        LLMCallRecord(
            caller_module=self.module,
            purpose=self.purpose,
            model=self.model,
            root_feedback_id=self.root_feedback_id,
            depth=self.depth,
            request_event_id=self.request_event_id,
            additional_causes=self.additional_causes,
            started_at=now,
            ended_at=now,
            status="failed",
            error_code="not_started",
        )
        return self


@dataclass(frozen=True)
class ProviderResult:
    output: dict[str, Any] = field(repr=False)
    usage: LLMUsage | None = None


class ProviderFailure(Exception):
    """Adapter attestation, not parsed from an untrusted remote error message.

    safe_to_retry=True means positively NOT executed (e.g. an authoritative
    admission rejection). Never set it for a timeout, HTTP 5xx or lost response.
    Error text is intentionally neither accepted nor persisted.
    """

    def __init__(self, *, safe_to_retry: bool = False):
        self.safe_to_retry = safe_to_retry
        super().__init__("provider_rejected" if safe_to_retry else "provider_outcome_unknown")


class Provider(Protocol):
    def complete(self, call: CallRequest, *, model: str, timeout: float) -> ProviderResult:
        """Honor timeout, bound response bytes, disable redirects/env proxies, redact errors."""
        ...


class RequestVerifier(Protocol):
    def verify(self, caller: StorageClient, call: CallRequest) -> ReevaluationRequested:
        """Optional deployment-trusted override of the default ① SDK preflight.

        Must authenticate using caller's token, verify request ownership and
        present cause/target permissions, never merely parse caller-supplied events.
        The gateway independently checks canonical root/depth/all inherited traces.
        Default composition uses caller.preflight_llm_call, without an override.
        """
        ...


class GatewayError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)
