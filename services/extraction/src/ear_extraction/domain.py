"""Module-local payload contracts. Public storage/event envelopes remain in ear_contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from ear_contracts.base import Id, UtcDatetime
from ear_contracts.events import AdditionalCauses
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Interval(Model):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("empty_or_reversed_interval")
        return self


class Rule(Model):
    scope: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    action: Literal["calendar", "reminder"] | None = None
    activity_id: Id | None = None
    enabled: bool = True


class Activity(Interval):
    title_md: str = Field(min_length=1)
    description_md: str = ""
    evidence_ids: list[Id] = Field(min_length=1)


class Segmentation(Model):
    activities: list[Activity]
    empty_reason: str = ""


class Selection(Model):
    rule_ids: list[Id]


class Action(Model):
    kind: Literal["calendar", "reminder"]
    title: str = Field(min_length=1)
    notes: str = ""
    start: UtcDatetime | None = None
    end: UtcDatetime | None = None
    due_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def valid_dates(self) -> Self:
        if self.kind == "calendar":
            if not self.start or not self.end or self.end <= self.start or self.due_at:
                raise ValueError("calendar_requires_start_end")
        elif self.due_at is None or self.start or self.end:
            raise ValueError("reminder_requires_due_at")
        return self


class Generated(Model):
    body_md: str = ""
    evidence_ids: list[Id] = Field(default_factory=list)
    action: Action | None = None
    action_reason: str = ""
    unchanged: bool = False


class ActionExecution(Model):
    """Internal ④/⑥ handoff, never a public user command or adapter receipt."""

    operation: Literal["start", "result", "confirm_executed", "retry"]
    action_key: str = Field(pattern=r"^ear_action_[0-9a-f]{64}$")
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_version: int = Field(strict=True, ge=1)
    attempt: int = Field(strict=True, ge=1)
    acknowledge_duplicate_risk: bool = Field(default=False, strict=True)
    acknowledge_executed: bool = Field(default=False, strict=True)


class Command(Model):
    kind: Literal[
        "extract",
        "reextract",
        "apply_rule",
        "extract_once",
        "reevaluate",
        "rule_create",
        "rule_update",
        "rule_delete",
        "review_suggestion",
        "action_result",
    ]
    interval: Interval | None = None
    target_id: Id | None = None
    rule_id: Id | None = None
    expected_version: int | None = Field(default=None, ge=1)
    rule: Rule | None = None
    decision: Literal["approved", "rejected"] | None = None
    outcome: Literal["executed", "failed", "executing", "unknown"] | None = None
    execution: ActionExecution | None = None
    receipt: str | None = None
    # Returned by preview_reextract. Exact version set is required, even when empty.
    preview: dict[str, int] | None = None
    description: str = ""
    # Absent in old commands: their reevaluation target remains artifact.
    target_type: Literal["artifact", "rule_selection"] = "artifact"

    @model_serializer(mode="wrap")
    def omit_legacy_target_default(self, handler):
        # API feedback fingerprints depend on the normalized Command shape.
        # Old artifact commands must serialize exactly as before this addition.
        data = handler(self)
        if self.target_type == "artifact":
            data.pop("target_type", None)
        if self.execution is None:
            data.pop("execution", None)
        return data

    @model_validator(mode="before")
    @classmethod
    def execution_version_is_strict(cls, value: Any) -> Any:
        # New internal protocol is strict without changing legacy Command coercion.
        if (
            isinstance(value, dict)
            and value.get("execution") is not None
            and type(value.get("expected_version")) is not int
        ):
            raise ValueError("execution_expected_version_required")
        return value

    @model_validator(mode="after")
    def required_fields(self) -> Self:
        if self.kind in {"extract", "reextract"} and self.interval is None:
            raise ValueError("interval_required")
        if self.kind == "reextract" and self.preview is None:
            raise ValueError("preview_required")
        if self.kind not in {"extract", "reextract", "rule_create"} and not self.target_id:
            raise ValueError("target_id_required")
        if self.kind in {"rule_create", "rule_update", "extract_once"} and self.rule is None:
            raise ValueError("rule_required")
        if self.kind == "apply_rule" and not self.rule_id:
            raise ValueError("rule_id_required")
        if (
            self.kind in {"rule_update", "rule_delete", "review_suggestion", "action_result"}
            and self.expected_version is None
        ):
            raise ValueError("expected_version_required")
        if self.kind == "review_suggestion" and self.decision is None:
            raise ValueError("decision_required")
        if self.kind == "action_result" and (not self.outcome or not self.receipt):
            raise ValueError("execution_receipt_required")
        if self.execution is not None:
            if self.kind != "action_result":
                raise ValueError("execution_requires_internal_action_result")
            e = self.execution
            if e.operation in {"start", "retry"} and self.outcome != "executing":
                raise ValueError("execution_start_requires_executing")
            if e.operation == "result" and self.outcome not in {"unknown", "executed", "failed"}:
                raise ValueError("invalid_execution_result")
            if e.operation == "confirm_executed" and (
                self.outcome != "executed" or not e.acknowledge_executed
            ):
                raise ValueError("execution_confirmation_required")
            if e.operation == "retry" and not e.acknowledge_duplicate_risk:
                raise ValueError("duplicate_risk_confirmation_required")
            if (e.acknowledge_executed and e.operation != "confirm_executed") or (
                e.acknowledge_duplicate_risk and e.operation != "retry"
            ):
                raise ValueError("unexpected_execution_acknowledgement")
        elif self.outcome in {"executing", "unknown"}:
            raise ValueError("execution_protocol_required")
        return self


class Call(Model):
    call_id: Id | None = None
    request_event_id: Id | None = None
    additional_causes: AdditionalCauses = ()
    module: Literal["extraction"] = "extraction"
    root_feedback_id: str
    depth: int
    run_id: str
    purpose: str
    data: dict[str, Any]
    output_schema: dict[str, Any]

    @model_serializer(mode="wrap")
    def omit_absent_identity(self, handler):
        # Preserve the legacy fake/client shape when no durable identity was supplied.
        data = handler(self)
        for name in ("call_id", "request_event_id"):
            if data.get(name) is None:
                data.pop(name, None)
        if not self.additional_causes:
            data.pop("additional_causes", None)
        return data


class Blocked(Exception):
    """A missing prerequisite or an unresolved shared contract, never a fabricated success."""


class Waiting(Exception):
    """Attributions have not arrived yet."""


def now_utc() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class MergeDecision(Model):
    merge: bool
    title_md: str = ""
    description_md: str = ""


class InputBudgetExceeded(ValueError):
    """A bounded request must be split, or rejected if even one evidence item cannot fit."""
