"""contracts/ 下 JSON Schema 的自检。

- 每个 *.schema.json 本身是合法的 Draft 2020-12 Schema。
- examples/<schema 名>.<说明>.valid.json 必须通过校验；
  examples/<schema 名>.<说明>.invalid.json 必须不通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
SCHEMAS = sorted(CONTRACTS.rglob("*.schema.json"))
EXAMPLES = sorted(CONTRACTS.rglob("examples/*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validator_for(example: Path) -> Draft202012Validator:
    schema_name = example.name.split(".", 1)[0]
    schema_path = example.parent.parent / f"{schema_name}.schema.json"
    assert schema_path.is_file(), f"{example.name} 找不到对应的 {schema_path.name}"
    return Draft202012Validator(
        load(schema_path), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def test_has_schemas_and_examples() -> None:
    assert SCHEMAS
    assert EXAMPLES


@pytest.mark.parametrize("schema_path", SCHEMAS, ids=lambda p: p.name)
def test_schema_is_valid(schema_path: Path) -> None:
    Draft202012Validator.check_schema(load(schema_path))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example(example: Path) -> None:
    errors = list(validator_for(example).iter_errors(load(example)))
    if example.name.endswith(".valid.json"):
        assert errors == [], [e.message for e in errors]
    elif example.name.endswith(".invalid.json"):
        assert errors, "反例应当校验失败"
    else:
        pytest.fail("示例文件名必须以 .valid.json 或 .invalid.json 结尾")
