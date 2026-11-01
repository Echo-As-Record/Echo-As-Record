"""总线上的两种事件（contracts/events/*.schema.json）。

事件只带 id 和版本号，不带内容；需要内容时到 ① 读取。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ear_contracts.base import DataType, Id, ModuleName, RootFeedbackId, UtcDatetime

Operation = Literal["created", "updated", "superseded"]

_EVENT = ConfigDict(frozen=True, extra="allow")


class DataChanged(BaseModel):
    """「数据库被修改」：只由 ① 在写入原始数据、语义信息、归属关系、规则时自动发出。"""

    model_config = _EVENT
    event_id: Id
    event_type: Literal["data_changed"] = "data_changed"
    occurred_at: UtcDatetime
    data_type: DataType
    data_id: Id
    operation: Operation
    new_version: int = Field(ge=1)
    old_version: int | None = Field(ge=1)
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=0)
    skip_history: bool
    # 以下为 DRAFT 新增的可选字段
    writer: ModuleName | None = None
    batch_id: Id | None = None


class Cause(BaseModel):
    """引起「请求重新判断」的那次修改。"""

    model_config = _EVENT
    data_type: DataType
    data_id: Id
    old_version: int | None = Field(ge=1)
    new_version: int = Field(ge=1)


class ReevaluationRequested(BaseModel):
    """「请求重新判断」：只由 ⑤ 发出，经 ① 投递。"""

    model_config = _EVENT
    event_id: Id
    event_type: Literal["reevaluation_requested"] = "reevaluation_requested"
    occurred_at: UtcDatetime
    target_type: DataType
    target_id: Id
    cause: Cause
    change_description: str = Field(min_length=1)
    root_feedback_id: RootFeedbackId
    depth: int = Field(ge=1)


Event = DataChanged | ReevaluationRequested

_EVENT_TYPES: dict[str, type[DataChanged] | type[ReevaluationRequested]] = {
    "data_changed": DataChanged,
    "reevaluation_requested": ReevaluationRequested,
}


def parse_event(data: dict[str, Any]) -> Event | None:
    """解析总线上的事件。遇到不认识的事件类型返回 None，调用方应忽略它，不能报错。"""
    model = _EVENT_TYPES.get(str(data.get("event_type")))
    return None if model is None else model.model_validate(data)


def to_json_dict(event: Event) -> dict[str, Any]:
    """序列化为符合 contracts 的 JSON 对象；可选字段为 None 时省略。"""
    data = event.model_dump(mode="json")
    for key in ("writer", "batch_id"):
        if data.get(key) is None:
            data.pop(key, None)
    return data
