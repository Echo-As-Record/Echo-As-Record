"""一次原子写入可以包含的操作。

所有操作放进一个列表交给 StorageClient.write：要么全部生效，要么全部不生效；
生效后，每条有变化的原始数据、语义信息、归属关系、规则各发一条「数据库被修改」。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ear_contracts import Cause, NewRawRecord, VersionRef, is_id

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def _check_type(data_type: str) -> None:
    if not _SNAKE.fullmatch(data_type):
        raise ValueError(f"data_type 必须是 snake_case：{data_type!r}")


def _check_id(value: str, name: str = "id") -> None:
    if not is_id(value):
        raise ValueError(f"{name} 必须是小写 UUIDv7：{value!r}")


@dataclass(frozen=True, slots=True)
class AppendRaw:
    """追加一条原始数据。按 (data_type, adapter_version, dedup_key) 去重：已存在时跳过，
    结果为 deduplicated，返回已有数据的 id，不发事件。"""

    record: NewRawRecord


@dataclass(frozen=True, slots=True)
class Create:
    """新建一个有版本的对象（版本 1）。

    - id 由写入方生成，已存在时冲突 id_exists。
    - dedup_key 可选：同一 data_type 内永远唯一，已存在时冲突 dedup_key_exists
      （例如 unnamed_person:<声纹 id>，防止同一事件并发处理时建出两个未命名人物）。
    - event_start/event_end：对象描述的时间段，用于按时间查询；登记表里设置了
      time_from 的类型（归属关系）由 ① 填写，写入方不填。
    """

    data_type: str
    id: str
    payload: Mapping[str, Any]
    dedup_key: str | None = None
    event_start: datetime | None = None
    event_end: datetime | None = None
    skip_history: bool = False

    def __post_init__(self) -> None:
        _check_type(self.data_type)
        _check_id(self.id)
        if self.dedup_key is not None and not self.dedup_key:
            raise ValueError("dedup_key 不能是空字符串")


@dataclass(frozen=True, slots=True)
class Update:
    """基于 expected_version 写入新版本（乐观锁）。payload 是完整的新内容。

    - 当前版本不是 expected_version：冲突 version_mismatch。
    - 对象已被整体替代：冲突 object_superseded。
    - 内容（payload 与时间）和当前版本相同：结果为 unchanged，不产生新版本，不发事件。
    - event_start/event_end 为 None 时沿用当前版本的时间。
    """

    data_type: str
    id: str
    expected_version: int
    payload: Mapping[str, Any]
    event_start: datetime | None = None
    event_end: datetime | None = None
    skip_history: bool = False

    def __post_init__(self) -> None:
        _check_type(self.data_type)
        _check_id(self.id)
        if self.expected_version < 1:
            raise ValueError("expected_version 从 1 开始")


@dataclass(frozen=True, slots=True)
class Supersede:
    """把对象整体标记为已被替代：新增一个 status=superseded 的版本（内容与当前版本相同），
    之后不能再写入。对象已被替代时结果为 unchanged。"""

    data_type: str
    id: str
    expected_version: int
    skip_history: bool = False

    def __post_init__(self) -> None:
        _check_type(self.data_type)
        _check_id(self.id)
        if self.expected_version < 1:
            raise ValueError("expected_version 从 1 开始")


@dataclass(frozen=True, slots=True)
class RegisterDependencies:
    """登记「依赖方的某个版本」依赖了哪些「对象 + 版本」，整体替换这个依赖方版本之前的登记。

    - 只有负责写依赖方的模块可以登记。
    - dependent_version 为 None 时，取本批前面的操作执行后依赖方的当前版本
      （适合和 Update 放在同一批：内容没变时版本号不变）。
    - depends_on 的版本必须是被依赖方此刻的当前版本，且被依赖方未被整体替代，否则冲突
      stale_dependency：说明计算所用的数据已经过时，应重新读取后重算。
    - 重新判断后结果不变时，也要用新的被依赖版本重新登记，否则会一直被查成「已过时」。
    """

    dependent_type: str
    dependent_id: str
    depends_on: Sequence[VersionRef]
    dependent_version: int | None = None

    def __post_init__(self) -> None:
        _check_type(self.dependent_type)
        _check_id(self.dependent_id, "dependent_id")
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True, slots=True)
class PutSystemRecord:
    """写系统记录（提取记录、传播记录等）。不发事件，不保留旧内容。

    - expected_revision 为 None：新建，已存在时冲突 id_exists。
    - 否则整体覆盖：当前 revision 不同时冲突 revision_mismatch；内容相同时为 unchanged。
    - event_start/event_end 为 None 时沿用原来的时间（新建时表示没有时间）。
    """

    data_type: str
    id: str
    payload: Mapping[str, Any]
    expected_revision: int | None = None
    event_start: datetime | None = None
    event_end: datetime | None = None

    def __post_init__(self) -> None:
        _check_type(self.data_type)
        _check_id(self.id)


@dataclass(frozen=True, slots=True)
class PublishReevaluation:
    """发布「请求重新判断」（只有登记表允许的模块，即 ⑤）。

    root_feedback_id、depth 取自本次写入的 Context，depth 必须 >= 1（用 Context.deeper()）。
    和传播记录放在同一批写入，二者同时生效或同时不生效。
    """

    target_type: str
    target_id: str
    cause: Cause
    change_description: str
    event_id: str | None = None

    def __post_init__(self) -> None:
        _check_type(self.target_type)
        _check_id(self.target_id, "target_id")
        if self.event_id is not None:
            _check_id(self.event_id, "event_id")


@dataclass(frozen=True, slots=True)
class MarkProcessed:
    """记录「消费方 consumer 已处理事件 event_id」。已记录过时冲突 already_processed，
    整批不生效：把它和处理结果放在同一批，就能保证同一事件只生效一次。

    consumer 必须是调用方模块名，或以「模块名.」开头（如 extraction.feedback）。
    """

    consumer: str
    event_id: str

    def __post_init__(self) -> None:
        _check_id(self.event_id, "event_id")


WriteOp = (
    AppendRaw
    | Create
    | Update
    | Supersede
    | RegisterDependencies
    | PutSystemRecord
    | PublishReevaluation
    | MarkProcessed
)


@dataclass(frozen=True, slots=True)
class Batch:
    """便于逐步构造一批写入。"""

    ops: list[WriteOp] = field(default_factory=list)

    def add(self, *ops: WriteOp) -> Batch:
        self.ops.extend(ops)
        return self
