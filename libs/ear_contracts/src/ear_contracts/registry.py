"""数据类型登记表（contracts/registry/data-types.json）。

① 按它检查写入权限、执行唯一性约束、计算时间范围。
DEFAULT_REGISTRY 与 contracts/registry/data-types.json 保持一致，由仓库测试检查。

核心类型与插件类型的区别由**可信注册来源**决定，不采信 spec 或 JSON 中的自报字段：

- DEFAULT_REGISTRY（内置核心配置）中的类型全部是核心类型；
- Registry.extend 登记的是插件类型（① 处理的动态插件登记走这里）；
- 内置核心新增类型（如以后的 rule_selection）只能由受信的 bootstrap 代码调用
  Registry.extend_core，或用 from_json/load(..., trusted_core=True) 加载内置核心配置文件。
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

CAPABILITIES: tuple[str, ...] = ("requires_dependencies", "supports_reevaluation")
"""登记表中的布尔能力项。缺省 false；只能用于有版本的数据（semantic、rule），
因为只有有版本的数据才能登记依赖、才会因依赖过时而被请求重新判断。"""


class UnknownDataTypeError(LookupError):
    pass


def _json_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 JSON 布尔值，不是 {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DataTypeSpec:
    """一种数据类型的登记。

    - writer：唯一负责写这类数据的模块，也是重新判断的执行方（不另设执行人字段）。
    - requires_dependencies：Create、Update（含 unchanged）须同批完整登记结果版本的依赖，
      可为显式空集。缺省 false 只为兼容旧登记表，不表示核心类型应永久为 false。
    - supports_reevaluation：writer 接收「请求重新判断」并重判这类数据。
    - reevaluation_result：显式受保护的单写完成记录；名称后缀本身不授予能力。
    """

    name: str
    kind: DataKind
    writer: str
    unique_fields: tuple[str, ...] = ()
    exclusive_members: tuple[str, ...] = ()
    time_from: str | None = None
    description: str = ""
    requires_dependencies: bool = False
    supports_reevaluation: bool = False
    reevaluation_result: bool = False
    llm_call_metadata: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.llm_call_metadata, bool):
            raise ValueError("llm_call_metadata must be bool")
        if (self.name == "llm_call" or self.llm_call_metadata) and (
            self.name,
            self.kind,
            self.writer,
            self.llm_call_metadata,
        ) != ("llm_call", "system_record", "llm_gateway", True):
            raise ValueError("llm_call is reserved for gateway metadata")
        if not isinstance(self.reevaluation_result, bool):
            raise ValueError("reevaluation_result must be bool")
        if self.reevaluation_result and (
            self.name != self.writer + "_completion" or self.kind != "system_record"
        ):
            raise ValueError("completion must be an executor-owned system record")
        for capability in CAPABILITIES:
            value = getattr(self, capability)
            if not isinstance(value, bool):
                raise ValueError(f"{self.name}: {capability} 必须是 bool，不是 {value!r}")
            if value and self.kind not in VERSIONED_KINDS:
                raise ValueError(
                    f"{self.name}: {capability} 只能用于有版本的数据（semantic、rule），"
                    f"不能用于 {self.kind}"
                )

    @property
    def versioned(self) -> bool:
        return self.kind in VERSIONED_KINDS

    @property
    def emits_events(self) -> bool:
        return self.kind in EVENT_KINDS

    @classmethod
    def from_json(cls, name: str, data: Mapping[str, Any]) -> DataTypeSpec:
        """不认识的键被忽略（Schema 允许扩展）；核心/插件来源不从 JSON 读取。"""
        return cls(
            name=name,
            kind=data["kind"],
            writer=data["writer"],
            unique_fields=tuple(data.get("unique_fields", ())),
            exclusive_members=tuple(data.get("exclusive_members", ())),
            time_from=data.get("time_from"),
            description=data.get("description", ""),
            requires_dependencies=_json_bool(data, "requires_dependencies"),
            supports_reevaluation=_json_bool(data, "supports_reevaluation"),
            reevaluation_result=_json_bool(data, "reevaluation_result"),
            llm_call_metadata=_json_bool(data, "llm_call_metadata"),
        )

    def to_json(self) -> dict[str, Any]:
        """缺省值不输出：未开启能力项的登记与旧登记表的 JSON 完全相同。"""
        data: dict[str, Any] = {"kind": self.kind, "writer": self.writer}
        if self.unique_fields:
            data["unique_fields"] = list(self.unique_fields)
        if self.exclusive_members:
            data["exclusive_members"] = list(self.exclusive_members)
        if self.time_from is not None:
            data["time_from"] = self.time_from
        if self.description:
            data["description"] = self.description
        if self.requires_dependencies:
            data["requires_dependencies"] = True
        if self.supports_reevaluation:
            data["supports_reevaluation"] = True
        if self.reevaluation_result:
            data["reevaluation_result"] = True
        if self.llm_call_metadata:
            data["llm_call_metadata"] = True
        return data


@dataclass(frozen=True, slots=True)
class Registry:
    """数据类型登记表。core_types 是经可信来源登记的核心类型，其余已登记类型都是插件类型。"""

    data_types: Mapping[str, DataTypeSpec]
    event_publishers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    core_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.core_types, frozenset):
            raise TypeError("core_types 必须是 frozenset")
        unknown = self.core_types - self.data_types.keys()
        if unknown:
            raise ValueError(f"core_types 含未登记的数据类型：{sorted(unknown)}")

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

    def is_core(self, data_type: str) -> bool:
        """核心类型返回 True，插件类型返回 False；未登记的类型抛 UnknownDataTypeError。"""
        self.get(data_type)
        return data_type in self.core_types

    def _with(
        self,
        specs: tuple[DataTypeSpec, ...],
        event_publishers: Mapping[str, tuple[str, ...]] | None,
        *,
        core: bool,
    ) -> Registry:
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
        core_types = self.core_types | {s.name for s in specs} if core else self.core_types
        return Registry(MappingProxyType(types), MappingProxyType(publishers), core_types)

    def extend(
        self,
        *specs: DataTypeSpec,
        event_publishers: Mapping[str, tuple[str, ...]] | None = None,
    ) -> Registry:
        """登记**插件**数据类型或事件发布者，返回新的登记表。已登记的类型不能被覆盖。"""
        return self._with(specs, event_publishers, core=False)

    def extend_core(
        self,
        *specs: DataTypeSpec,
        event_publishers: Mapping[str, tuple[str, ...]] | None = None,
    ) -> Registry:
        """登记**核心**数据类型（受信接口）。

        只供内置核心配置的 bootstrap 代码调用；① 处理动态插件登记、或任何来自请求的数据
        都不得走这里，否则插件可以自称核心。已登记的类型不能被覆盖或升级为核心。
        """
        return self._with(specs, event_publishers, core=True)

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, trusted_core: bool = False) -> Registry:
        """从登记表 JSON 构造。

        trusted_core=True 只用于受信的内置核心配置文件，此时其中全部类型为核心类型；
        缺省 False，全部视为插件类型。JSON 中任何自报来源的字段都不被采信。
        """
        types = {
            name: DataTypeSpec.from_json(name, spec) for name, spec in data["data_types"].items()
        }
        publishers = {k: tuple(v) for k, v in data["event_publishers"].items()}
        core_types = frozenset(types) if trusted_core else frozenset()
        return cls(MappingProxyType(types), MappingProxyType(publishers), core_types)

    @classmethod
    def load(cls, path: str | Path, *, trusted_core: bool = False) -> Registry:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_json(data, trusted_core=trusted_core)

    def to_json(self) -> dict[str, Any]:
        """不输出 core_types：JSON 不是可信来源，核心/插件区别不随 JSON 往返。"""
        return {
            "data_types": {name: spec.to_json() for name, spec in self.data_types.items()},
            "event_publishers": {k: list(v) for k, v in self.event_publishers.items()},
        }


def _raw(name: str, writer: str, description: str) -> DataTypeSpec:
    return DataTypeSpec(name, "raw_record", writer, description=description)


def _semantic(name: str, writer: str, description: str, **kwargs: Any) -> DataTypeSpec:
    return DataTypeSpec(name, "semantic", writer, description=description, **kwargs)


_BUILTIN = Registry(
    MappingProxyType(
        {
            spec.name: spec
            for spec in (
                DataTypeSpec("llm_call", "system_record", "llm_gateway", llm_call_metadata=True),
                _raw("audio_ref", "adapter", "录音引用"),
                _raw("transcript_segment", "adapter", "转写段"),
                _raw("local_speaker", "adapter", "局部说话人"),
                _raw("voiceprint", "adapter", "声纹"),
                _raw("ambient_sound", "adapter", "环境声"),
                _raw("gps_point", "adapter", "GPS 点"),
                _raw("gps_geocode", "adapter", "GPS 逆地理编码"),
                _raw("phone_info", "adapter", "手机信息"),
                _raw("feedback", "api", "用户反馈"),
                _raw("plugin_management_feedback", "api", "插件管理反馈"),
                DataTypeSpec(
                    "plugin_management_result",
                    "system_record",
                    "storage",
                    description="插件管理结果",
                ),
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
                    "attribution_completion",
                    "system_record",
                    "attribution",
                    reevaluation_result=True,
                ),
                DataTypeSpec(
                    "extraction_completion", "system_record", "extraction", reevaluation_result=True
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
DEFAULT_REGISTRY = Registry(
    _BUILTIN.data_types,
    _BUILTIN.event_publishers,
    core_types=frozenset(_BUILTIN.data_types),
)
"""内置核心配置：全部类型都是核心类型。
业务重判/依赖能力保持缺省 false；完成记录能力显式启用。候选类型
（attribution、artifact，以及尚未登记的 rule_selection）按类型取得 ③④ 实际输入后再启用。"""
