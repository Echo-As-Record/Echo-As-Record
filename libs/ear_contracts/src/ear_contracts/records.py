"""存入 ① 的数据：原始数据、语义信息的版本、系统记录、依赖登记。

与 contracts/records/*.schema.json 一一对应。读取模型允许未知字段（字段只增不减，
消费方必须忽略不认识的字段）；写入用的 NewRawRecord 拒绝未知字段，以便尽早发现拼写错误。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ear_contracts.base import (
    DataType,
    Id,
    ModuleName,
    RootFeedbackId,
    SnakeCase,
    UtcDatetime,
)

DataKind = Literal["raw_record", "semantic", "rule", "system_record"]
VersionStatus = Literal["active", "superseded"]

_READ = ConfigDict(frozen=True, extra="allow")
_WRITE = ConfigDict(frozen=True, extra="forbid")


def _check_range(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("event_start、event_end 必须同时为 null 或同时有值")
    if start is not None and end is not None and end < start:
        raise ValueError("event_end 早于 event_start")


class FileRef(BaseModel):
    """大文件引用：相对 Octopus 数据根目录的路径、SHA-256、字节数。"""

    model_config = _READ
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("path 必须是相对路径，且不能含 .. 段")
        return value


class Source(BaseModel):
    model_config = _READ
    device: str = Field(min_length=1)
    channel: SnakeCase
    user: str = Field(min_length=1, description="MVP 统一为 default")


class NewRawRecord(BaseModel):
    """写入方提交的原始数据：不含 version、ingested_at，由 ① 填写。"""

    model_config = _WRITE
    id: Id
    data_type: DataType
    event_start: UtcDatetime
    event_end: UtcDatetime
    source: Source
    adapter_version: str = Field(min_length=1)
    dedup_key: str = Field(min_length=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        _check_range(self.event_start, self.event_end)
        return self


class RawRecord(BaseModel):
    """存入 ① 的原始数据（contracts/records/raw-record.schema.json）。"""

    model_config = _READ
    id: Id
    data_type: DataType
    version: Literal[1] = 1
    event_start: UtcDatetime
    event_end: UtcDatetime
    ingested_at: UtcDatetime
    source: Source
    adapter_version: str = Field(min_length=1)
    dedup_key: str = Field(min_length=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        _check_range(self.event_start, self.event_end)
        return self

    @property
    def kind(self) -> DataKind:
        return "raw_record"


class SemanticRecord(BaseModel):
    """语义信息、归属关系、规则的一个版本（contracts/records/semantic-record.schema.json）。"""

    model_config = _READ
    id: Id
    data_type: DataType
    version: int = Field(ge=1)
    status: VersionStatus
    event_start: UtcDatetime | None
    event_end: UtcDatetime | None
    dedup_key: str | None = Field(min_length=1)
    payload: dict[str, Any]
    writer: ModuleName
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=0)
    skip_history: bool
    batch_id: Id
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        _check_range(self.event_start, self.event_end)
        return self

    @property
    def kind(self) -> DataKind:
        return "semantic"

    @property
    def superseded(self) -> bool:
        """对象在这个版本已被整体替代。"""
        return self.status == "superseded"


Record = RawRecord | SemanticRecord
"""按 id 读取时可能得到的数据。规则也用 SemanticRecord 表示。"""


class SystemRecord(BaseModel):
    """系统记录（contracts/records/system-record.schema.json）。"""

    model_config = _READ
    id: Id
    data_type: DataType
    revision: int = Field(ge=1)
    event_start: UtcDatetime | None
    event_end: UtcDatetime | None
    payload: dict[str, Any]
    writer: ModuleName
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        _check_range(self.event_start, self.event_end)
        return self


class VersionRef(BaseModel):
    """「对象 + 版本」。原始数据的版本固定为 1。"""

    model_config = ConfigDict(frozen=True, extra="allow")
    data_type: DataType
    id: Id
    version: int = Field(ge=1)


class DependencyRecord(BaseModel):
    """依赖登记（contracts/records/dependency-record.schema.json）。"""

    model_config = _READ
    dependent: VersionRef
    depends_on: VersionRef
    registered_by: ModuleName
    registered_at: UtcDatetime


def record_from_dict(data: dict[str, Any]) -> Record:
    """按字段区分原始数据和有版本的数据。"""
    if "ingested_at" in data:
        return RawRecord.model_validate(data)
    return SemanticRecord.model_validate(data)
