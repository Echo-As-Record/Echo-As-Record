"""P0b 首批契约模型：DependencyRecord.fields、登记表能力项、核心/插件可信来源。

见 docs/proposals/storage-p0-contracts.md 第 2.1、2.3、2.7、2.9 节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataKind,
    DataTypeSpec,
    DependencyRecord,
    Registry,
    UnknownDataTypeError,
)
from jsonschema import Draft202012Validator
from pydantic import ValidationError

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
EXAMPLES = CONTRACTS / "records" / "examples"
REGISTRY_JSON = CONTRACTS / "registry" / "data-types.json"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(load(path), format_checker=Draft202012Validator.FORMAT_CHECKER)


DEP_SCHEMA = validator(CONTRACTS / "records" / "dependency-record.schema.json")
REGISTRY_SCHEMA = validator(CONTRACTS / "registry" / "data-types.schema.json")
OLD = load(EXAMPLES / "dependency-record.artifact-on-attribution.valid.json")


# DependencyRecord.fields ------------------------------------------------------------


def test_old_dependency_json_is_unchanged() -> None:
    record = DependencyRecord.model_validate(OLD)
    assert record.fields is None and record.whole_object
    assert "fields" not in record.model_dump()
    assert record.model_dump(mode="json", exclude_none=False) == OLD
    assert json.loads(record.model_dump_json()) == OLD


def test_fields_round_trip_without_reordering() -> None:
    data = {**OLD, "fields": ["references", "", "name"]}
    assert list(DEP_SCHEMA.iter_errors(data)) == []
    record = DependencyRecord.model_validate(data)
    assert record.fields == ("references", "", "name")
    assert not record.whole_object
    assert record.model_dump(mode="json") == data


def test_unknown_keys_are_kept_on_read() -> None:
    data = {
        **OLD,
        "fields": ["name"],
        "changed_fields": ["name"],
        "depends_on": {**OLD["depends_on"], "future": 1},
    }
    assert list(DEP_SCHEMA.iter_errors(data)) == []
    assert DependencyRecord.model_validate(data).model_dump(mode="json") == data


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (None, "null 不是整个对象的表示，应省略"),
        ([], "空数组"),
        ("name", "单个字符串不能逐字拆开"),
        (["name", 1], "非字符串元素"),
        (["name", "name"], "重复元素"),
        ({"name": True}, "对象"),
    ],
)
def test_bad_fields_rejected_by_schema_and_model(fields: Any, reason: str) -> None:
    data = {**OLD, "fields": fields}
    assert list(DEP_SCHEMA.iter_errors(data)), reason
    with pytest.raises(ValidationError):
        DependencyRecord.model_validate(data)


def test_python_inputs_are_strict() -> None:
    base = DependencyRecord.model_validate(OLD)
    kwargs = base.model_dump()
    with pytest.raises(ValidationError):
        DependencyRecord(**{**kwargs, "fields": None})
    with pytest.raises(ValidationError):
        DependencyRecord(**{**kwargs, "fields": [b"name"]})
    assert DependencyRecord(**{**kwargs, "fields": ("a",)}).fields == ("a",)
    assert DependencyRecord(**kwargs) == base


# 登记表能力项 -------------------------------------------------------------------------


def registry_doc(spec: dict[str, Any]) -> dict[str, Any]:
    return {"data_types": {"x": spec}, "event_publishers": {}}


def test_default_registry_json_unchanged_and_capabilities_default_false() -> None:
    data = load(REGISTRY_JSON)
    data.pop("$comment", None)
    assert DEFAULT_REGISTRY.to_json() == data
    for spec in DEFAULT_REGISTRY.data_types.values():
        # 候选核心类型（attribution、artifact、rule_selection）待 ③④ 实际输入后按类型启用
        assert not spec.requires_dependencies and not spec.supports_reevaluation
        assert "requires_dependencies" not in spec.to_json()
        expected_result = spec.name in {"attribution_completion", "extraction_completion"}
        assert spec.reevaluation_result is expected_result
        assert DataTypeSpec.from_json(spec.name, spec.to_json()) == spec
        assert spec.to_json().get("reevaluation_result", False) is expected_result


@pytest.mark.parametrize("kind", ["semantic", "rule"])
def test_capabilities_round_trip(kind: DataKind) -> None:
    spec = DataTypeSpec("x", kind, "social", requires_dependencies=True, supports_reevaluation=True)
    data = spec.to_json()
    assert data == {
        "kind": kind,
        "writer": "social",
        "requires_dependencies": True,
        "supports_reevaluation": True,
    }
    assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(data))) == []
    assert DataTypeSpec.from_json("x", data) == spec
    only_one = DataTypeSpec("x", "semantic", "social", supports_reevaluation=True)
    assert DataTypeSpec.from_json("x", only_one.to_json()) == only_one


def test_explicit_false_is_accepted_and_normalised() -> None:
    data = {"kind": "raw_record", "writer": "adapter", "requires_dependencies": False}
    assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(data))) == []
    spec = DataTypeSpec.from_json("x", data)
    assert spec.to_json() == {"kind": "raw_record", "writer": "adapter"}
    for name in ("sample_job_completion", "sample_completion"):
        legacy = DataTypeSpec.from_json(name, {"kind": "system_record", "writer": "sample"})
        assert not legacy.reevaluation_result
        assert (
            DataTypeSpec.from_json(name, {**legacy.to_json(), "reevaluation_result": False})
            == legacy
        )
        assert "reevaluation_result" not in legacy.to_json()
    protected = DataTypeSpec(
        "sample_completion", "system_record", "sample", reevaluation_result=True
    )
    assert DataTypeSpec.from_json(protected.name, protected.to_json()) == protected
    assert not list(REGISTRY_SCHEMA.iter_errors(registry_doc(protected.to_json())))
    for value in (1, 0, "true", None, []):
        invalid = {**protected.to_json(), "reevaluation_result": value}
        assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(invalid)))
        with pytest.raises(ValueError):
            DataTypeSpec.from_json(protected.name, invalid)
    for kind in ("semantic", "rule", "raw_record"):
        invalid = {**protected.to_json(), "kind": kind}
        assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(invalid)))
        with pytest.raises(ValueError):
            DataTypeSpec.from_json(protected.name, invalid)
    with pytest.raises(ValueError):
        DataTypeSpec("sample_job_completion", "system_record", "sample", reevaluation_result=True)
    with pytest.raises(ValueError):
        DEFAULT_REGISTRY.extend(
            DataTypeSpec(
                "extraction_completion", "system_record", "sample", reevaluation_result=False
            )
        )
    assert DEFAULT_REGISTRY.get("extraction_completion").reevaluation_result


@pytest.mark.parametrize("capability", ["requires_dependencies", "supports_reevaluation"])
@pytest.mark.parametrize("value", [1, 0, "true", None, []])
def test_capability_must_be_bool(capability: str, value: Any) -> None:
    data = {"kind": "semantic", "writer": "social", capability: value}
    assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(data)))
    with pytest.raises(ValueError):
        DataTypeSpec.from_json("x", data)
    with pytest.raises(ValueError):
        DataTypeSpec("x", "semantic", "social", **{capability: value})


@pytest.mark.parametrize("capability", ["requires_dependencies", "supports_reevaluation"])
@pytest.mark.parametrize("kind", ["raw_record", "system_record"])
def test_capability_only_for_versioned_kinds(capability: str, kind: str) -> None:
    data = {"kind": kind, "writer": "social", capability: True}
    assert list(REGISTRY_SCHEMA.iter_errors(registry_doc(data)))
    with pytest.raises(ValueError, match="有版本"):
        DataTypeSpec.from_json("x", data)


# 核心 / 插件来源 -----------------------------------------------------------------------


def test_builtin_registry_is_all_core() -> None:
    assert DEFAULT_REGISTRY.core_types == frozenset(DEFAULT_REGISTRY.data_types)
    assert DEFAULT_REGISTRY.is_core("attribution")
    assert Registry.load(REGISTRY_JSON, trusted_core=True) == DEFAULT_REGISTRY
    assert Registry.from_json(DEFAULT_REGISTRY.to_json(), trusted_core=True) == DEFAULT_REGISTRY
    with pytest.raises(UnknownDataTypeError):
        DEFAULT_REGISTRY.is_core("rule_selection")


def test_json_is_not_a_trust_source() -> None:
    data = DEFAULT_REGISTRY.to_json()
    data["data_types"]["social_summary"] = {
        "kind": "semantic",
        "writer": "social",
        "core": True,
        "origin": "core",
    }
    registry = Registry.from_json(data)
    assert registry.core_types == frozenset()
    assert not registry.is_core("social_summary")
    assert "core" not in registry.get("social_summary").to_json()


def test_extend_registers_plugins_and_extend_core_is_explicit() -> None:
    plugin = DataTypeSpec("social_summary", "semantic", "social", requires_dependencies=True)
    registry = DEFAULT_REGISTRY.extend(plugin)
    assert not registry.is_core("social_summary")
    assert registry.is_core("artifact")
    assert registry.get("social_summary").requires_dependencies

    core = registry.extend_core(DataTypeSpec("rule_selection", "semantic", "extraction"))
    assert core.is_core("rule_selection") and not core.is_core("social_summary")
    assert "rule_selection" not in DEFAULT_REGISTRY
    with pytest.raises(ValueError, match="已经登记"):
        core.extend_core(DataTypeSpec("social_summary", "semantic", "social"))
    with pytest.raises(ValueError, match="已经登记"):
        core.extend(DataTypeSpec("rule_selection", "semantic", "social"))


def test_core_types_must_be_registered_frozenset() -> None:
    with pytest.raises(ValueError, match="未登记"):
        Registry(DEFAULT_REGISTRY.data_types, core_types=frozenset({"nope"}))
    with pytest.raises(TypeError):
        Registry(DEFAULT_REGISTRY.data_types, core_types={"person"})  # type: ignore[arg-type]
