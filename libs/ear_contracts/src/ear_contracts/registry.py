"""数据类型登记表（contracts/registry/data-types.json）。

① 按它检查写入权限、执行唯一性约束、计算时间范围。扩展模块用 Registry.extend 登记新类型。
DEFAULT_REGISTRY 与 contracts/registry/data-types.json 保持一致，由仓库测试检查。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ear_contracts.records import DataKind

VERSIONED_KINDS: frozenset[DataKind] = frozenset({"semantic", "rule"})
EVENT_KINDS: frozenset[DataKind] = frozenset({"raw_record", "semantic", "rule"})
"""写入时会发出「数据库被修改」的数据分类；系统记录不发。"""


class UnknownDataTypeError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DataTypeSpec:
    name: str
    kind: DataKind
    writer: str
    unique_fields: tuple[str, ...] = ()
    exclusive_members: tuple[str, ...] = ()
    time_from: str | None = None
    description: str = ""

    @property
    def versioned(self) -> bool:
        return self.kind in VERSIONED_KINDS

    @property
    def emits_events(self) -> bool:
        return self.kind in EVENT_KINDS

    @classmethod
    def from_json(cls, name: str, data: Mapping[str, Any]) -> DataTypeSpec:
        return cls(
            name=name,
            kind=data["kind"],
            writer=data["writer"],
            unique_fields=tuple(data.get("unique_fields", ())),
            exclusive_members=tuple(data.get("exclusive_members", ())),
            time_from=data.get("time_from"),
            description=data.get("description", ""),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "writer": self.writer}
        if self.unique_fields:
            data["unique_fields"] = list(self.unique_fields)
        if self.exclusive_members:
            data["exclusive_members"] = list(self.exclusive_members)
        if self.time_from is not None:
            data["time_from"] = self.time_from
        if self.description:
            data["description"] = self.description
        return data


@dataclass(frozen=True, slots=True)
class Registry:
    data_types: Mapping[str, DataTypeSpec]
    event_publishers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def get(self, data_type: str) -> DataTypeSpec:
        try:
            return self.data_types[data_type]
        except KeyError:
            raise UnknownDataTypeError(f"数据类型 {data_type!r} 没有登记") from None

    def __contains__(self, data_type: object) -> bool:
        return data_type in self.data_types

    def writer_of(self, data_type: str) -> str:
        return self.get(data_type).writer

    def publishers_of(self, event_type: str) -> tuple[str, ...]:
        return self.event_publishers.get(event_type, ())

    def extend(
        self,
        *specs: DataTypeSpec,
        event_publishers: Mapping[str, tuple[str, ...]] | None = None,
    ) -> Registry:
        """登记新的数据类型或事件发布者，返回新的登记表。已登记的类型不能被覆盖。"""
        types = dict(self.data_types)
        for spec in specs:
            if spec.name in types:
                raise ValueError(f"数据类型 {spec.name!r} 已经登记")
            types[spec.name] = spec
        publishers = dict(self.event_publishers)
        for event_type, modules in (event_publishers or {}).items():
            if event_type in publishers:
                raise ValueError(f"事件类型 {event_type!r} 已经登记")
            publishers[event_type] = tuple(modules)
        return Registry(MappingProxyType(types), MappingProxyType(publishers))

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Registry:
        types = {
            name: DataTypeSpec.from_json(name, spec) for name, spec in data["data_types"].items()
        }
        publishers = {k: tuple(v) for k, v in data["event_publishers"].items()}
        return cls(MappingProxyType(types), MappingProxyType(publishers))

    @classmethod
    def load(cls, path: str | Path) -> Registry:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_json(self) -> dict[str, Any]:
        return {
            "data_types": {name: spec.to_json() for name, spec in self.data_types.items()},
            "event_publishers": {k: list(v) for k, v in self.event_publishers.items()},
        }


def _raw(name: str, writer: str, description: str) -> DataTypeSpec:
    return DataTypeSpec(name, "raw_record", writer, description=description)


def _semantic(name: str, writer: str, description: str, **kwargs: Any) -> DataTypeSpec:
    return DataTypeSpec(name, "semantic", writer, description=description, **kwargs)


DEFAULT_REGISTRY = Registry(
    MappingProxyType(
        {
            spec.name: spec
            for spec in (
                _raw("audio_ref", "adapter", "录音引用"),
                _raw("transcript_segment", "adapter", "转写段"),
                _raw("local_speaker", "adapter", "局部说话人"),
                _raw("voiceprint", "adapter", "声纹"),
                _raw("ambient_sound", "adapter", "环境声"),
                _raw("gps_point", "adapter", "GPS 点"),
                _raw("phone_info", "adapter", "手机信息"),
                _raw("feedback", "api", "用户反馈"),
                _semantic(
                    "person", "attribution", "人物（含参考）", exclusive_members=("references",)
                ),
                _semantic(
                    "place", "attribution", "地点（含参考）", exclusive_members=("references",)
                ),
                _semantic(
                    "attribution",
                    "attribution",
                    "归属关系：声纹 → 人物、GPS 点 → 地点",
                    unique_fields=("subject_id",),
                    time_from="subject_id",
                ),
                _semantic("activity", "extraction", "活动"),
                _semantic("artifact", "extraction", "md 产物"),
                _semantic("action_suggestion", "extraction", "外部动作建议"),
                DataTypeSpec(
                    "rule", "rule", "extraction", description="提取规则（含只改这一次的提取）"
                ),
                DataTypeSpec(
                    "extraction_run", "system_record", "extraction", description="提取记录"
                ),
                DataTypeSpec(
                    "propagation_record", "system_record", "dependency", description="传播记录"
                ),
            )
        }
    ),
    MappingProxyType(
        {
            "data_changed": ("storage",),
            "reevaluation_requested": ("dependency",),
        }
    ),
)
