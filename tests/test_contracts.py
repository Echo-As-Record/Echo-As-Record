"""contracts/ 下 JSON Schema 的自检。

- 每个 *.schema.json 本身是合法的 Draft 2020-12 Schema。
- examples/<schema 名>.<说明>.valid.json 必须通过校验；
  examples/<schema 名>.<说明>.invalid.json 必须不通过。
- JSON Schema 表达不了的规则写在 SEMANTIC_CHECKS 里，示例要同时满足。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
SCHEMAS = sorted(CONTRACTS.rglob("*.schema.json"))
EXAMPLES = sorted(CONTRACTS.rglob("examples/*.json"))
RAW_RECORD = CONTRACTS / "records" / "raw-record.schema.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_name_of(example: Path) -> str:
    return example.name.split(".", 1)[0]


def validator_for(example: Path) -> Draft202012Validator:
    schema_path = example.parent.parent / f"{schema_name_of(example)}.schema.json"
    assert schema_path.is_file(), f"{example.name} 找不到对应的 {schema_path.name}"
    return Draft202012Validator(
        load(schema_path), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def raw_record_time_order(data: dict) -> list[str]:
    """event_end >= event_start（由 ① 在写入时校验）。"""
    try:
        start = datetime.fromisoformat(data["event_start"])
        end = datetime.fromisoformat(data["event_end"])
    except (KeyError, TypeError, ValueError):
        return []  # 缺字段或格式错误由 Schema 报告
    return [] if end >= start else ["event_end 早于 event_start"]


def optional_time_range(data: dict) -> list[str]:
    """event_start、event_end 同时为 null 或同时有值；有值时 event_end >= event_start。"""
    start, end = data.get("event_start"), data.get("event_end")
    if start is None and end is None:
        return []
    if start is None or end is None:
        return ["event_start、event_end 必须同时为 null 或同时有值"]
    return raw_record_time_order(data)


SEMANTIC_CHECKS: dict[str, Callable[[dict], list[str]]] = {
    "raw-record": raw_record_time_order,
    "semantic-record": optional_time_range,
    "system-record": optional_time_range,
}


def test_has_schemas_and_examples() -> None:
    assert SCHEMAS
    assert EXAMPLES


@pytest.mark.parametrize("schema_path", SCHEMAS, ids=lambda p: p.name)
def test_schema_is_valid(schema_path: Path) -> None:
    Draft202012Validator.check_schema(load(schema_path))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example(example: Path) -> None:
    data = load(example)
    errors = [e.message for e in validator_for(example).iter_errors(data)]
    errors += SEMANTIC_CHECKS.get(schema_name_of(example), lambda _: [])(data)
    if example.name.endswith(".valid.json"):
        assert errors == [], errors
    elif example.name.endswith(".invalid.json"):
        assert errors, "反例应当校验失败"
    else:
        pytest.fail("示例文件名必须以 .valid.json 或 .invalid.json 结尾")


def file_ref_errors(ref: dict) -> list[str]:
    schema = load(RAW_RECORD)
    validator = Draft202012Validator({"$ref": "#/$defs/file_ref", "$defs": schema["$defs"]})
    return [e.message for e in validator.iter_errors(ref)]


SHA = "ab" * 32


@pytest.mark.parametrize(
    "path",
    ["rec-0001.m4a", "octopus/phone-01/2026-10-29/rec-0001.m4a", "a/..b/c", "a/b../c"],
)
def test_file_ref_valid(path: str) -> None:
    assert file_ref_errors({"path": path, "sha256": SHA, "size": 0}) == []


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ({"path": "/home/u/rec.m4a", "sha256": SHA, "size": 1}, "绝对路径"),
        ({"path": "../rec.m4a", "sha256": SHA, "size": 1}, "开头是 .."),
        ({"path": "a/../../rec.m4a", "sha256": SHA, "size": 1}, "中间有 .."),
        ({"path": "a/..", "sha256": SHA, "size": 1}, "结尾是 .."),
        ({"path": "", "sha256": SHA, "size": 1}, "空路径"),
        ({"path": "a.m4a", "sha256": SHA.upper(), "size": 1}, "哈希大写"),
        ({"path": "a.m4a", "sha256": SHA[:-2], "size": 1}, "哈希长度不对"),
        ({"path": "a.m4a", "sha256": SHA, "size": -1}, "大小为负"),
        ({"path": "a.m4a", "sha256": SHA}, "缺 size"),
    ],
)
def test_file_ref_invalid(ref: dict, reason: str) -> None:
    assert file_ref_errors(ref), reason


REGISTRY = CONTRACTS / "registry" / "data-types.json"
REGISTRY_SCHEMA = CONTRACTS / "registry" / "data-types.schema.json"
VERSIONED_KINDS = {"semantic", "rule"}


def test_registry_matches_schema() -> None:
    validator = Draft202012Validator(load(REGISTRY_SCHEMA))
    errors = [e.message for e in validator.iter_errors(load(REGISTRY))]
    assert errors == [], errors


def test_registry_semantics() -> None:
    registry = load(REGISTRY)
    for name, spec in registry["data_types"].items():
        for key in ("unique_fields", "exclusive_members", "time_from"):
            if key in spec:
                assert spec["kind"] in VERSIONED_KINDS, f"{name}: {key} 只用于有版本的数据"
    assert registry["event_publishers"]["data_changed"] == ["storage"]
