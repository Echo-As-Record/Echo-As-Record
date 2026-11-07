"""Closed management input; no caller-supplied authority, SQL, secrets or core flag."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from ear_contracts import DataTypeSpec
from ear_contracts.base import Id, UtcDatetime
from ear_contracts.records import DataKind
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=128)]
Module = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=55)]


class PluginTypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Name
    kind: DataKind
    requires_dependencies: StrictBool = False
    supports_reevaluation: StrictBool = False
    reevaluation_result: StrictBool = False
    unique_fields: tuple[Name, ...] = ()
    exclusive_members: tuple[Name, ...] = ()
    time_from: Name | None = None

    def spec(self, module: str, *, reevaluable: bool = False) -> DataTypeSpec:
        return DataTypeSpec(
            self.name,
            self.kind,
            module,
            unique_fields=self.unique_fields,
            exclusive_members=self.exclusive_members,
            time_from=self.time_from,
            requires_dependencies=self.requires_dependencies,
            supports_reevaluation=self.supports_reevaluation,
            reevaluation_result=self.reevaluation_result
            or (reevaluable and self.name == module + "_completion"),
        )


class ManagementInput(BaseModel):
    """Management operations are immutable API-owned feedback, never direct CRUD.

    retry_task uses a NEW request_id and target_feedback_id pointing at the old
    task in the same module. Only the storage management worker may execute it,
    after checking both administrators' current authority. It queues only the
    original consumer's exhausted delivery, preserving the original task's
    request/provenance/resources. Completed or non-recoverable tasks are rejected.
    Subscription changes stage immutable broker generations before activation.
    New types use a per-consumer DB commit boundary; recovery stays explicit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    request_id: Id
    action: Literal[
        "register_plugin",
        "enable",
        "disable",
        "set_permissions",
        "status",
        "retry_task",
        "rotate_credentials",
        "revoke_credentials",
        "history_backfill",
    ]
    module: Module
    target_feedback_id: Id | None = None
    types: tuple[PluginTypeInput, ...] = Field(default=(), max_length=128)
    read_types: tuple[Name, ...] = Field(default=(), max_length=512)
    subscribe_types: tuple[Name, ...] = Field(default=(), max_length=512)

    resume_choice: Literal["catch_up", "skip"] | None = None
    expected_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    range_start: UtcDatetime | None = Field(default=None, alias="rangeStart")
    range_end: UtcDatetime | None = Field(default=None, alias="rangeEnd")
    input_types: tuple[Name, ...] = Field(default=(), max_length=512)

    def payload(self):
        # Preserve pre-retry schema6 request fingerprints, including nested nulls.
        data = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={
                name
                for name in (
                    "target_feedback_id",
                    "resume_choice",
                    "expected_revision",
                    "range_start",
                    "range_end",
                    "input_types",
                )
                if getattr(self, name) is None or getattr(self, name) == ()
            },
        )

        # Preserve old feedback fingerprints when the additive capability is absent/false.
        for item in data["types"]:
            if not item["reevaluation_result"]:
                item.pop("reevaluation_result")
        return data

    @model_validator(mode="after")
    def validate_declaration(self) -> Self:
        if (self.resume_choice is None) != (self.expected_revision is None):
            raise ValueError("resume_choice requires expected_revision")
        if self.resume_choice is not None and self.action not in {"enable", "set_permissions"}:
            raise ValueError("resume choice only allowed during recovery")
        if self.action == "history_backfill":
            if self.range_start is None or self.range_end is None or not self.input_types:
                raise ValueError("explicit rangeStart/rangeEnd and input_types required")
            if self.range_start >= self.range_end:
                raise ValueError("nonempty half-open history range required")
        elif self.range_start is not None or self.range_end is not None or self.input_types:
            raise ValueError("history scope only allowed for history_backfill")
        if (self.action == "retry_task") != (self.target_feedback_id is not None):
            raise ValueError("retry_task requires an original target_feedback_id only")
        if not set(self.subscribe_types) <= set(self.read_types):
            raise ValueError("subscription subset required")
        if self.action == "register_plugin":
            if not self.types or len({t.name for t in self.types}) != len(self.types):
                raise ValueError("nonempty unique types required")
            for item in self.types:
                if not item.name.startswith(self.module + "_"):
                    raise ValueError("module namespace required")
                item.spec(self.module, reevaluable=any(t.supports_reevaluation for t in self.types))
            if any(t.supports_reevaluation for t in self.types) and not any(
                t.name == self.module + "_completion" and t.kind == "system_record"
                for t in self.types
            ):
                raise ValueError("owned completion system type required")
        elif self.types:
            raise ValueError("types only allowed at registration")
        if self.action not in {"register_plugin", "set_permissions"} and (
            self.read_types or self.subscribe_types
        ):
            raise ValueError("unexpected permission declaration")
        return self
