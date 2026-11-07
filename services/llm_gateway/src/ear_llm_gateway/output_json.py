"""Strict JSON and a bounded, offline schema subset (not a full JSON Schema engine).

Supports acyclic local $defs references, anyOf, object/array/scalar constraints.
Unsupported keywords fail closed BEFORE HTTP, rather than silently weakening validation.
No network resolution, caller-defined regex execution, recursive schemas or coercion.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

# Exact allowlist, NOT a regex safety heuristic. Caller patterns are never compiled.
_UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_UUID7 = re.compile(_UUID7_PATTERN)
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# Fixed linear grammar; datetime checks calendar dates (including leap years).
# Like extraction's datetime model, leap seconds are not representable.
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt](?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _format_valid(name: str, value: str) -> bool:
    if name == "uuid":
        return _UUID.fullmatch(value) is not None
    if _DATETIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.upper())
    except ValueError:
        return False
    return True


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite_json")


def strict_json(raw: bytes | str) -> Any:
    # Check nesting before invoking the recursive JSON decoder.
    source = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
    depth, quoted, escaped = 0, False, False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 48:
                raise ValueError("json_depth_limit")
        elif char in "]}":
            depth -= 1
    value = json.loads(source, object_pairs_hook=_pairs, parse_constant=_constant)
    # Also rejects exponent overflow and unpaired surrogate escapes.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class OutputSchema:
    """Compile a small explicit dialect, with work limits at compile and validation."""

    def __init__(self, schema: dict[str, Any]):
        raw = encode(schema)
        if len(raw) > 65536:
            raise ValueError("schema_limit")
        self.root = strict_json(raw)
        self._compile(self.root, (), [2048], 0)

    def _ref(self, ref):
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise ValueError("unsupported_schema_reference")
        key = ref[8:]
        if not key or "/" in key or "~" in key:
            raise ValueError("unsupported_schema_reference")
        try:
            return self.root["$defs"][key]
        except (KeyError, TypeError):
            raise ValueError("invalid_schema_reference") from None

    def _compile(self, node, refs, budget, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > 32 or not isinstance(node, dict):
            raise ValueError("schema_limit")
        allowed = {
            "$defs",
            "$ref",
            "title",
            "description",
            "default",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "anyOf",
            "enum",
            "const",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "pattern",
            "format",
        }
        if node.keys() - allowed:
            raise ValueError("unsupported_schema_keyword")
        if "pattern" in node and node["pattern"] != _UUID7_PATTERN:
            raise ValueError("unsupported_schema_pattern")
        if "format" in node and node["format"] not in ("uuid", "date-time"):
            raise ValueError("unsupported_schema_format")
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or ref in refs:
                raise ValueError("recursive_schema")
            self._compile(self._ref(ref), (*refs, ref), budget, depth + 1)
        if "type" in node and node["type"] not in (
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        ):
            raise ValueError("unsupported_schema_type")
        for name in ("$defs", "properties"):
            if name in node:
                if not isinstance(node[name], dict):
                    raise ValueError("invalid_schema")
                for child in node[name].values():
                    self._compile(child, refs, budget, depth + 1)
        if "items" in node:
            self._compile(node["items"], refs, budget, depth + 1)
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            self._compile(node["additionalProperties"], refs, budget, depth + 1)
        if "anyOf" in node:
            if not isinstance(node["anyOf"], list) or not 1 <= len(node["anyOf"]) <= 16:
                raise ValueError("invalid_schema")
            for child in node["anyOf"]:
                self._compile(child, refs, budget, depth + 1)
        if "required" in node and (
            not isinstance(node["required"], list)
            or any(not isinstance(key, str) for key in node["required"])
            or len(set(node["required"])) != len(node["required"])
        ):
            raise ValueError("invalid_schema")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise ValueError("invalid_schema")
        for key in ("minItems", "maxItems", "minLength", "maxLength"):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                raise ValueError("invalid_schema")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if key in node and (
                type(node[key]) not in (int, float) or not math.isfinite(node[key])
            ):
                raise ValueError("invalid_schema")

    def validate(self, value):
        if not self._valid(self.root, value, [100000]):
            raise ValueError("output_schema_mismatch")

    def _valid(self, node, value, budget):
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("schema_work_limit")
        if "$ref" in node and not self._valid(self._ref(node["$ref"]), value, budget):
            return False
        if "anyOf" in node and not any(self._valid(n, value, budget) for n in node["anyOf"]):
            return False
        types = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in (int, float),
            "boolean": type(value) is bool,
            "null": value is None,
        }
        if "type" in node and not types[node["type"]]:
            return False
        if "const" in node and not _equal(value, node["const"]):
            return False
        if "enum" in node and not any(_equal(value, v) for v in node["enum"]):
            return False
        if isinstance(value, dict):
            if any(key not in value for key in node.get("required", [])):
                return False
            properties = node.get("properties", {})
            for key, item in value.items():
                child = properties.get(key, node.get("additionalProperties", True))
                if child is False or (
                    isinstance(child, dict) and not self._valid(child, item, budget)
                ):
                    return False
        if isinstance(value, list):
            if not node.get("minItems", 0) <= len(value) <= node.get("maxItems", len(value)):
                return False
            if "items" in node and not all(self._valid(node["items"], v, budget) for v in value):
                return False
        if isinstance(value, str) and not (
            node.get("minLength", 0) <= len(value) <= node.get("maxLength", len(value))
        ):
            return False
        if isinstance(value, str):
            if "pattern" in node and _UUID7.fullmatch(value) is None:
                return False
            if "format" in node and not _format_valid(node["format"], value):
                return False
        if type(value) in (int, float):
            if value < node.get("minimum", value) or value > node.get("maximum", value):
                return False
            if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
                return False
            if "exclusiveMaximum" in node and value >= node["exclusiveMaximum"]:
                return False
        return True


def _equal(left, right):
    # JSON equality must not confuse bool with numeric 0/1, including nested values.
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return left == right
