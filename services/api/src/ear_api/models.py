"""Bounded gateway inputs; existing ④ Command is reused, never reinterpreted."""

from __future__ import annotations

import json
import math
from typing import Any, Literal, Self

from ear_contracts import is_id
from ear_contracts.base import Id
from ear_extraction.domain import Command, Interval
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REVISION = 2147483647
PUBLIC_COMMAND_FIELDS = {
    "extract": {"interval"},
    "reextract": {"interval", "preview"},
    "apply_rule": {"target_id", "rule_id"},
    "extract_once": {"target_id", "rule"},
    "rule_create": {"rule"},
    "rule_update": {"target_id", "expected_version", "rule"},
    "rule_delete": {"target_id", "expected_version"},
    "review_suggestion": {"target_id", "expected_version", "decision"},
}
PENDING_FIELDS = {
    "correct_attribution": {"subject_id", "previous_target_id", "new_target_id"},
    "confirm_attribution": {"subject_id", "target_id"},
    "add_reference": {"subject_id", "target_id", "rejudge_history"},
    "remove_reference": {"subject_id", "target_id", "rejudge_history"},
    "rename": {"target_id", "name"},
    "create_place": {"name", "gps_ids"},
    "reattribute_range": {"interval"},
    "reevaluate_related": {"previous_target_id", "new_target_id"},
}


def revision(value: Any) -> None:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ValueError("invalid revision")


def interval_input(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError("interval required")
    if any(not isinstance(item, str) for item in value.values()):
        raise ValueError("UTC timestamp strings required")
    Interval.model_validate(value)


def command_fields(value: Any, fields: dict[str, set[str]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("command object required")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError("public command required")
    if not set(value) <= fields[kind] | {"kind"}:
        raise ValueError("fields do not belong to command")
    bound_json(value)
    if "interval" in value:
        interval_input(value["interval"])
    return value


class PendingCommand(BaseModel):
    """Local recording contract only: no claim of a ③/⑤ consuming implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    kind: Literal[
        "correct_attribution",
        "confirm_attribution",
        "add_reference",
        "remove_reference",
        "rename",
        "create_place",
        "reattribute_range",
        "reevaluate_related",
    ]
    target_id: Id | None = None
    subject_id: Id | None = None
    previous_target_id: Id | None = None
    new_target_id: Id | None = None
    name: str | None = Field(default=None, min_length=1, max_length=256)
    gps_ids: list[Id] = Field(default_factory=list, max_length=100)
    interval: dict[str, str] | None = None
    rejudge_history: bool = Field(default=False, strict=True)
    expected_version: int | None = Field(default=None, strict=True, ge=1, le=MAX_REVISION)

    @model_validator(mode="before")
    @classmethod
    def closed_input(cls, value: Any) -> Any:
        # Optional concurrency guards remain available for pending ③ commands.
        fields = {kind: names | {"expected_version"} for kind, names in PENDING_FIELDS.items()}
        value = command_fields(value, fields)
        if "expected_version" in value:
            revision(value["expected_version"])
        return value

    @model_validator(mode="after")
    def required(self) -> Self:
        if self.kind == "correct_attribution" and not all(
            (self.subject_id, self.previous_target_id, self.new_target_id)
        ):
            raise ValueError("correction targets required")
        if self.kind in {"confirm_attribution", "add_reference", "remove_reference"} and not all(
            (self.subject_id, self.target_id)
        ):
            raise ValueError("subject and target required")
        if self.kind == "rename" and not (self.target_id and self.name and self.name.strip()):
            raise ValueError("name and target required")
        if self.kind == "create_place" and not (self.name and self.name.strip() and self.gps_ids):
            raise ValueError("name and points required")
        if self.kind == "create_place" and len(set(self.gps_ids)) != len(self.gps_ids):
            raise ValueError("duplicate points")
        if self.kind == "reattribute_range":
            Interval.model_validate(self.interval)
        if self.kind == "reevaluate_related" and not (
            self.previous_target_id and self.new_target_id
        ):
            raise ValueError("both related targets required")
        return self


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: Id
    module: Literal["extraction", "attribution", "dependency"]
    command: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value: Any) -> Any:
        bound_json(value)
        return value

    def normalized_command(self) -> dict[str, Any]:
        if self.module == "extraction":
            value = command_fields(self.command, PUBLIC_COMMAND_FIELDS)
            if "expected_version" in value:
                revision(value["expected_version"])
            if "rule" in value:
                rule = value["rule"]
                if not isinstance(rule, dict):
                    raise ValueError("rule object required")
                if "enabled" in rule and type(rule["enabled"]) is not bool:
                    raise ValueError("boolean required")
            if "preview" in value:
                preview = value["preview"]
                if not isinstance(preview, dict) or any(not is_id(key) for key in preview):
                    raise ValueError("UUIDv7 preview keys required")
                for version in preview.values():
                    revision(version)
            # Validation/serialization semantics remain the existing ④ Command's.
            return Command.model_validate(value).model_dump(mode="json")
        pending = PendingCommand.model_validate(self.command)
        if (self.module == "dependency") != (pending.kind == "reevaluate_related"):
            raise ValueError("command owner mismatch")
        return pending.model_dump(mode="json")


def bound_json(value: Any, depth: int = 0, *, authority: bool = True) -> None:
    """Bound work as well as nesting; reject non-JSON values, overflow and bad Unicode."""
    stack = [(value, depth)]
    remaining = 4096
    forbidden = {
        "token",
        "authorization",
        "admin",
        "storage_token",
        "secret",
        "role",
        "principal",
        "principal_id",
        "is_admin",
        "api_key",
        "password",
        "credentials",
    }
    while stack:
        item, level = stack.pop()
        remaining -= 1
        if remaining < 0 or level > 12:
            raise ValueError("JSON complexity limit")
        if isinstance(item, dict):
            if len(item) > 512:
                raise ValueError("object limit")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ValueError("object key limit")
                key.encode("utf-8")
                if authority and key.lower() in forbidden:
                    raise ValueError("authority must not be in body")
                if key in {"expected_version", "expected_revision"} and child is not None:
                    revision(child)
                stack.append((child, level + 1))
        elif isinstance(item, list):
            if len(item) > 512:
                raise ValueError("array limit")
            stack.extend((child, level + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 8192:
                raise ValueError("text limit")
            item.encode("utf-8")
        elif item is None or type(item) is bool:
            continue
        elif type(item) is int:
            if item.bit_length() > 63:
                raise ValueError("integer limit")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("finite number required")
        else:
            raise ValueError("JSON value required")


def load_json_object(
    raw: str | bytes | bytearray, *, max_bytes: int = 65536, authority: bool = True
) -> dict[str, Any]:
    """Shared strict decoder for boundary/config/upstream; never includes input in errors."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def bad_constant(_value):
        raise ValueError("nonfinite number")

    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > max_bytes:
            raise ValueError("JSON size limit")
        data = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
        if not isinstance(data, dict):
            raise ValueError("object required")
        bound_json(data, authority=authority)
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid JSON object") from None
