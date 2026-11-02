"""ear_contracts 与 contracts/ 的一致性。

- contracts 的正例能被对应模型解析，再序列化后仍通过 JSON Schema；反例被模型拒绝。
- DEFAULT_REGISTRY 与 contracts/registry/data-types.json 相同。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataChanged,
    DependencyRecord,
    RawRecord,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    SystemRecord,
    format_utc,
    is_id,
    new_id,
    parse_event,
    to_json_dict,
)
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
MODELS: dict[str, type[BaseModel]] = {
    "raw-record": RawRecord,
    "semantic-record": SemanticRecord,
    "system-record": SystemRecord,
    "dependency-record": DependencyRecord,
    "data-changed": DataChanged,
    "reevaluation-requested": ReevaluationRequested,
}
EXAMPLES = sorted(
    p for p in CONTRACTS.rglob("examples/*.json") if p.name.split(".", 1)[0] in MODELS
)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_for(name: str) -> Draft202012Validator:
    (path,) = CONTRACTS.rglob(f"{name}.schema.json")
    return Draft202012Validator(load(path), format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_every_schema_has_a_model() -> None:
    names = {p.name.removesuffix(".schema.json") for p in CONTRACTS.rglob("*.schema.json")}
    assert names - {"data-types"} == set(MODELS)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_model_agrees_with_example(example: Path) -> None:
    name = example.name.split(".", 1)[0]
    model, data = MODELS[name], load(example)
    if example.name.endswith(".valid.json"):
        parsed = model.model_validate(data)
        dumped = parsed.model_dump(mode="json", exclude_none=False)
        if isinstance(parsed, DataChanged | ReevaluationRequested):
            dumped = to_json_dict(parsed)
        errors = [e.message for e in schema_for(name).iter_errors(dumped)]
        assert errors == [], errors
        assert dumped == data
    else:
        with pytest.raises(ValidationError):
            model.model_validate(data)


def test_default_registry_matches_contract() -> None:
    data = load(CONTRACTS / "registry" / "data-types.json")
    data.pop("$comment", None)
    assert DEFAULT_REGISTRY.to_json() == data
    assert Registry.from_json(data) == Registry.from_json(DEFAULT_REGISTRY.to_json())


def test_parse_event_ignores_unknown_types() -> None:
    assert parse_event({"event_type": "something_new"}) is None
    example = load(CONTRACTS / "events/examples/data-changed.voiceprint-created.valid.json")
    assert isinstance(parse_event(example), DataChanged)


def test_new_id_is_uuid7_and_monotonic() -> None:
    ids = [new_id() for _ in range(5000)]
    assert all(is_id(i) for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_utc_handling() -> None:
    assert format_utc(datetime(2026, 10, 29, 13, 0, tzinfo=UTC)) == "2026-10-29T13:00:00Z"
    with pytest.raises(ValueError):
        format_utc(datetime(2026, 10, 29, 13, 0))
