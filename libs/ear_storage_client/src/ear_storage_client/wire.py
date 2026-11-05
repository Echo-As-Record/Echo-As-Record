"""Versioned typed JSON codecs; no pickle or silent future-outcome coercion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from ear_contracts import (
    DependencyRecord,
    Event,
    NewRawRecord,
    Record,
    ReevaluationResult,
    SemanticRecord,
    SystemRecord,
    format_utc,
)
from ear_contracts.base import Id
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ear_storage_client import (
    AppendRaw,
    AssertTypeRevision,
    Context,
    Create,
    DependencyInput,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
    WriteResult,
)
from ear_storage_client.engine import canonical_json
from ear_storage_client.queries import DependencySet, Page, Query, StaleDependency

RESULT_ADAPTER = TypeAdapter(WriteResult)
EVENT_ADAPTER = TypeAdapter(Event)
CONTEXT_ADAPTER = TypeAdapter(Context)
OP_ADAPTERS = {
    cls.__name__: TypeAdapter(cls)
    for cls in (
        AppendRaw,
        AssertTypeRevision,
        FinishReevaluation,
        Create,
        Update,
        Supersede,
        RegisterDependencies,
        PutSystemRecord,
        PublishReevaluation,
        MarkProcessed,
    )
}


def _value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return format_utc(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_value(v) for v in value]
    return value


def encode_request(ops: Sequence[WriteOp], context: Context) -> str:
    if not isinstance(context, Context):
        raise InvalidRequest("context must be Context")
    encoded = []
    try:
        for op in ops:
            name = type(op).__name__
            if name not in OP_ADAPTERS or type(op) not in (
                AppendRaw,
                AssertTypeRevision,
                FinishReevaluation,
                Create,
                Update,
                Supersede,
                RegisterDependencies,
                PutSystemRecord,
                PublishReevaluation,
                MarkProcessed,
            ):
                raise InvalidRequest("unknown write operation")
            payload = op.record.payload if isinstance(op, AppendRaw) else getattr(op, "payload", {})
            if not isinstance(json.loads(canonical_json(payload)), dict):
                raise InvalidRequest("payload must be a JSON object")
            body = _value(op)
            if isinstance(op, RegisterDependencies):
                body["depends_on"] = [
                    _value(DependencyInput(item.ref, item.fields)) for item in op.inputs()
                ]
            encoded.append({"op": name, "body": body})
        return canonical_json({"codec": 1, "context": _value(context), "ops": encoded})
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InvalidRequest):
            raise
        raise InvalidRequest("operations must contain canonical JSON values") from None


def decode_request(text: str) -> tuple[tuple[WriteOp, ...], Context]:
    try:
        data = load_json(text)
        check_keys(data, {"codec", "context", "ops"})
        if not isinstance(data, dict) or type(data.get("codec")) is not int or data["codec"] != 1:
            raise InvalidRequest("unsupported request codec")
        check_keys(data["context"], {f.name for f in fields(Context)})
        context = CONTEXT_ADAPTER.validate_json(canonical_json(data["context"]), strict=True)
        ops = []
        if not isinstance(data["ops"], list):
            raise InvalidRequest("ops must be an array")
        for item in data["ops"]:
            if not isinstance(item, dict) or not isinstance(item.get("op"), str):
                raise InvalidRequest("invalid operation envelope")
            if not isinstance(item.get("body"), dict):
                raise InvalidRequest("operation body must be an object")
            check_keys(item, {"op", "body"})
            adapter = OP_ADAPTERS.get(item["op"])
            if adapter is None:
                raise InvalidRequest("unknown write operation")
            validate_operation(item["op"], item["body"])
            if item["op"] == "AppendRaw":
                # UtcDatetime's BeforeValidator needs the contract's JSON date parser.
                # Payload values remain Any: this does not coerce 1 / 1.0 / True.
                op = AppendRaw(
                    NewRawRecord.model_validate_json(canonical_json(item["body"]["record"]))
                )
            elif item["op"] == "FinishReevaluation":
                # UtcDatetime's before-validator needs contract JSON date parsing.
                # Numeric result fields are individually strict; cause versions
                # are checked in validate_operation before this contract decode.
                op = FinishReevaluation(
                    ReevaluationResult.model_validate_json(canonical_json(item["body"]["result"]))
                )
            else:
                op = adapter.validate_json(canonical_json(item["body"]), strict=True)
            ops.append(op)
        return tuple(ops), context
    except (TypeError, ValueError, KeyError, ValidationError, RecursionError, OverflowError):
        raise InvalidRequest("invalid versioned batch request") from None


def fingerprint(ops: Sequence[WriteOp], context: Context) -> str:
    return hashlib.sha256(encode_request(ops, context).encode()).hexdigest()


def encode_result(result: WriteResult) -> str:
    return canonical_json({"codec": 1, "result": RESULT_ADAPTER.dump_python(result, mode="json")})


def decode_result(text: str) -> WriteResult:
    data = load_json(text)
    if not isinstance(data, dict) or type(data.get("codec")) is not int or data["codec"] != 1:
        raise InvalidRequest("unsupported result codec")
    # Stored events also use UtcDatetime. Decode with the public contract validators;
    # Literal outcomes still reject unknown values rather than claiming success.
    result = RESULT_ADAPTER.validate_json(canonical_json(data["result"]))
    # This existing public conflict detail is a tuple in both local backends.
    if result.conflict is not None and result.conflict.reason == "stale_dependency":
        details = dict(result.conflict.details)
        if isinstance(details.get("fields"), list):
            details["fields"] = tuple(details["fields"])
            result = replace(result, conflict=replace(result.conflict, details=details))
    return result


def request_size(ops: Sequence[WriteOp], context: Context) -> int:
    return len(encode_request(ops, context).encode())


# Wire v1 is deliberately closed for commands/arguments. Read records retain the
# contracts' forward-compatible extra fields. Future conflict Literals fail closed;
# this is not a claim that older SDKs understand future conflict reasons.
def check_keys(value: Any, allowed: set[str]) -> None:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise InvalidRequest("unknown or invalid command fields")


def load_json(text: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidRequest("duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(value):
        raise InvalidRequest("non-finite JSON number")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        canonical_json(result).encode("utf-8")  # Reject overflow and invalid Unicode.
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise InvalidRequest("invalid JSON") from None


def utc_fields(body: dict[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        value = body.get(name)
        if value is not None and (not isinstance(value, str) or not value.endswith("Z")):
            raise InvalidRequest("timestamps must be UTC JSON strings ending in Z")


def validate_operation(name: str, body: dict[str, Any]) -> None:
    classes = (
        AppendRaw,
        AssertTypeRevision,
        FinishReevaluation,
        Create,
        Update,
        Supersede,
        RegisterDependencies,
        PutSystemRecord,
        PublishReevaluation,
        MarkProcessed,
    )
    cls = next(cls for cls in classes if cls.__name__ == name)
    check_keys(body, {f.name for f in fields(cls)})
    utc_fields(body, ("event_start", "event_end"))
    if name == "AppendRaw":
        record = body["record"]
        check_keys(record, set(NewRawRecord.model_fields))
        check_keys(record["source"], {"device", "channel", "user"})
        utc_fields(record, ("event_start", "event_end"))
    elif name == "RegisterDependencies":
        for dep in body["depends_on"]:
            if isinstance(dep, dict) and "ref" in dep:
                check_keys(dep, {"ref", "fields"})
                check_keys(dep["ref"], {"data_type", "id", "version"})
            else:
                check_keys(dep, {"data_type", "id", "version"})
    elif name == "FinishReevaluation":
        check_keys(body["result"], set(ReevaluationResult.model_fields))
        utc_fields(body["result"], ("completed_at",))
        for cause in body["result"].get("extra_causes", []):
            for key in ("old_version", "new_version"):
                value = cause.get(key)
                if not (key == "old_version" and value is None) and type(value) is not int:
                    raise InvalidRequest("cause versions must be integers")
    elif name == "PublishReevaluation":
        from ear_contracts import Cause

        check_keys(body["cause"], set(Cause.model_fields))


# Shared read protocol: an explicit method enum, per-method closed argument models,
# and typed response adapters. No getattr dispatch from an unchecked client string.


class ReadMethod(StrEnum):
    TYPE_REVISION = "type_revision"
    GET = "get"
    GET_VERSION = "get_version"
    HISTORY = "history"
    GET_MANY = "get_many"
    GET_SYSTEM = "get_system_record"
    GET_REEVALUATION_RESULT = "get_reevaluation_result"
    SYSTEM_HISTORY = "system_history"
    QUERY = "query"
    COUNT = "count"
    DEPENDENCY_SET = "dependency_set"
    DEPENDENCIES = "dependencies_of"
    DEPENDENTS = "dependents_of"
    STALE = "stale_dependencies"
    PROCESSED = "is_processed"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TypeArgs(ClosedModel):
    data_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class IdArgs(ClosedModel):
    id: Id


class CompletionArgs(ClosedModel):
    request_event_id: Id


class VersionArgs(IdArgs):
    version: Annotated[int, Field(ge=1)]


class ManyArgs(ClosedModel):
    ids: Annotated[list[Id], Field(max_length=1000)]


class QueryArgs(ClosedModel):
    query: dict[str, Any]


class DependencyArgs(ClosedModel):
    dependent_id: Id
    version: Annotated[int, Field(ge=1)] | None = None


class DependentsArgs(ClosedModel):
    target_id: Id
    only_current_dependents: bool = True


class StaleArgs(ClosedModel):
    targets: Annotated[list[Id], Field(max_length=1000)]
    only_current_dependents: bool = True


class ProcessedArgs(ClosedModel):
    consumer: Annotated[str, Field(min_length=1, max_length=128)]
    event_id: Id


class ReadRequest(ClosedModel):
    codec: Literal[1]
    method: ReadMethod
    args: dict[str, Any]
    snapshot_id: Id | None = None


class SnapshotRequest(ClosedModel):
    codec: Literal[1]
    ttl_seconds: Annotated[int, Field(ge=1, le=3600)] | None = None


READ_ARGS: dict[str, type[ClosedModel]] = {
    "type_revision": TypeArgs,
    "get": IdArgs,
    "get_version": VersionArgs,
    "history": IdArgs,
    "get_many": ManyArgs,
    "get_system_record": IdArgs,
    "get_reevaluation_result": CompletionArgs,
    "system_history": IdArgs,
    "query": QueryArgs,
    "count": QueryArgs,
    "dependency_set": DependencyArgs,
    "dependencies_of": DependencyArgs,
    "dependents_of": DependentsArgs,
    "stale_dependencies": StaleArgs,
    "is_processed": ProcessedArgs,
}
READ_RESULTS: dict[str, Any] = {
    "type_revision": Annotated[int, Field(strict=True, ge=0)],
    "get": Record | None,
    "get_version": Record | None,
    "history": list[SemanticRecord],
    "get_many": dict[str, Record],
    "get_system_record": SystemRecord | None,
    "get_reevaluation_result": ReevaluationResult | None,
    "system_history": list[SystemRecord],
    "query": Page,
    "count": int,
    "dependency_set": DependencySet | None,
    "dependencies_of": list[DependencyRecord],
    "dependents_of": list[DependencyRecord],
    "stale_dependencies": list[StaleDependency],
    "is_processed": bool,
}
READ_ADAPTERS = {name: TypeAdapter(tp) for name, tp in READ_RESULTS.items()}


def decode_read(text: str | bytes) -> tuple[ReadRequest, dict[str, Any]]:
    data = load_json(text)
    if not isinstance(data, dict) or type(data.get("codec")) is not int:
        raise InvalidRequest("unsupported wire codec")
    request = ReadRequest.model_validate_json(canonical_json(data))
    args = READ_ARGS[request.method].model_validate_json(canonical_json(request.args))
    values = args.model_dump()
    if request.method in {"query", "count"}:
        query = values["query"]
        check_keys(query, {f.name for f in fields(Query)})
        if query.get("time") is not None:
            check_keys(query["time"], {"start", "end"})
            utc_fields(query["time"], ("start", "end"))
        values["query"] = TypeAdapter(Query).validate_json(canonical_json(query), strict=True)
    return request, values


def encode_read_result(method: str, value: Any) -> str:
    return canonical_json({"codec": 1, "result": _value(value)})


def decode_read_result(method: str, text: str) -> Any:
    data = load_json(text)
    if not isinstance(data, dict) or type(data.get("codec")) is not int or data["codec"] != 1:
        raise InvalidRequest("unsupported read codec")
    return READ_ADAPTERS[method].validate_json(canonical_json(data["result"]))
