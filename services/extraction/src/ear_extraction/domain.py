"""Module-local payload contracts. Public storage/event envelopes remain in ear_contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from ear_contracts.base import Id, UtcDatetime
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    outcome: Literal["executed", "failed"] | None = None
    receipt: str | None = None
    # Returned by preview_reextract. Exact version set is required, even when empty.
    preview: dict[str, int] | None = None
    description: str = ""

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
        return self


class Call(Model):
    module: Literal["extraction"] = "extraction"
    root_feedback_id: str
    depth: int
    run_id: str
    purpose: str
    data: dict[str, Any]
    output_schema: dict[str, Any]


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
