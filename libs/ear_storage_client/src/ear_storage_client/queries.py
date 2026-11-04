"""查询条件与查询结果。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

from ear_contracts import RawRecord, SemanticRecord, SystemRecord, VersionRef

from ear_storage_client.errors import InvalidRequest

MAX_LIMIT = 1000


def _require_utc(value: datetime, name: str) -> None:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise InvalidRequest(f"{name} 必须是 UTC 时间")


@dataclass(frozen=True, slots=True)
class TimeRange:
    """半开区间 [start, end)。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_utc(self.start, "start")
        _require_utc(self.end, "end")
        if self.end < self.start:
            raise InvalidRequest("end 早于 start")

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """数据的时间段 [start, end] 是否命中本区间（contracts/records/raw-record 的规则）。

        单点数据（start == end）满足 self.start <= start < self.end 即命中；
        有时长的数据满足 start < self.end 且 end > self.start 即命中。
        本区间为空（self.start == self.end，即 [t, t)）时什么都不命中。
        """
        if self.start == self.end:
            return False
        if start == end:
            return self.start <= start < self.end
        return start < self.end and end > self.start


@dataclass(frozen=True, slots=True)
class Query:
    """按数据类型查询。可用于原始数据、有版本的数据（只看当前版本）和系统记录。

    - time：按数据的时间段过滤；没有时间的数据不会命中。归属关系的时间由 ① 从它的原始数据复制。
    - where：payload 顶层字段等值过滤，例如 {"target_id": 人物 id}。
    - contains：payload 顶层数组字段包含某个值，例如 {"references": 声纹 id}。
    - unreferenced_by=(类型, 字段)：只返回 id 没有出现在该类型任何未被替代对象的该字段里的数据。
      例如 ("attribution", "subject_id") 查出还没有归属对象的声纹或 GPS 点。
    - source_user：只用于原始数据。
    - include_superseded：有版本的数据默认不返回已被整体替代的对象。
    - order：time 按 event_start（没有时间的排在最后）再按 id；id 按 id（UUIDv7 大致按创建时间）。
    """

    data_type: str
    time: TimeRange | None = None
    where: Mapping[str, Any] = field(default_factory=dict)
    contains: Mapping[str, Any] = field(default_factory=dict)
    unreferenced_by: tuple[str, str] | None = None
    source_user: str | None = None
    include_superseded: bool = False
    order: Literal["time", "id"] = "time"
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_LIMIT:
            raise InvalidRequest(f"limit 必须在 1～{MAX_LIMIT} 之间")
        if self.order not in ("time", "id"):
            raise InvalidRequest(f"未知的 order：{self.order!r}")
        object.__setattr__(self, "where", MappingProxyType(dict(self.where)))
        object.__setattr__(self, "contains", MappingProxyType(dict(self.contains)))


QueryItem = RawRecord | SemanticRecord | SystemRecord


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[QueryItem, ...]
    next_cursor: str | None
    """还有下一页时，把它放进 Query.cursor 再查一次；游标不透明，不要解析。"""


@dataclass(frozen=True, slots=True)
class StaleDependency:
    """一条已经过时的依赖登记：依赖方登记的被依赖方版本，已经不是被依赖方的当前版本。"""

    dependent: VersionRef
    depends_on: VersionRef
    """登记时的被依赖方版本。"""
    current_version: int
    """被依赖方现在的当前版本。"""
    target_superseded: bool
    """被依赖方已被整体替代。"""
