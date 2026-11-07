"""写入上下文：根源反馈 id 与依赖分析深度（补充章节 3.2、3.3）。"""

from __future__ import annotations

from dataclasses import dataclass

from ear_contracts import SYSTEM, DataChanged, ReevaluationRequested, TracedCause, is_id
from ear_contracts.events import AdditionalCauses
from pydantic import TypeAdapter


def checked_traces(values) -> tuple[TracedCause, ...]:
    traces = TypeAdapter(AdditionalCauses).validate_python(values)
    if any(t.model_extra or t.cause.model_extra for t in traces):
        raise ValueError("unknown causal write fields")
    return tuple(t.model_copy(deep=True) for t in traces)


@dataclass(frozen=True, slots=True)
class Context:
    """一次写入的传播上下文。① 原样写进每个版本和每条「数据库被修改」。

    「不处理历史」（skip_history）按每条写入单独设置，不在这里（同一次纠正里，
    参考的写入要标记、归属关系的写入不标记）。
    """

    root_feedback_id: str
    depth: int = 0
    request_event_id: str | None = None
    additional_causes: AdditionalCauses = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "additional_causes", checked_traces(self.additional_causes))
        if self.request_event_id is not None and not is_id(self.request_event_id):
            raise ValueError("request_event_id must be UUIDv7")
        if self.root_feedback_id != SYSTEM and not is_id(self.root_feedback_id):
            raise ValueError("root_feedback_id 必须是 UUIDv7 或字面量 'system'")
        if self.depth < 0:
            raise ValueError("depth 不能为负")

    @classmethod
    def system(cls) -> Context:
        """系统发起的行为（定时提取、适配器入库等）：根源为 system，深度 0。"""
        return cls(SYSTEM, 0)

    @classmethod
    def feedback(cls, feedback_id: str) -> Context:
        """执行一条用户反馈时直接产生的写入：根源为这条反馈，深度 0。"""
        return cls(feedback_id, 0)

    @classmethod
    def of(cls, event: DataChanged | ReevaluationRequested) -> Context:
        """沿用事件或请求里的根源和深度（负责模块按「请求重新判断」写入时用它）。"""
        return cls(
            event.root_feedback_id,
            event.depth,
            event.event_id if isinstance(event, ReevaluationRequested) else event.request_event_id,
            event.additional_causes,
        )

    def deeper(self) -> Context:
        """深度加 1（⑤ 发出「请求重新判断」时用）。"""
        return Context(
            self.root_feedback_id,
            self.depth + 1,
            self.request_event_id,
            tuple(t.model_copy(update={"depth": t.depth + 1}) for t in self.additional_causes),
        )

    def origins_for(self, explicit: tuple[TracedCause, ...]) -> tuple[TracedCause, ...]:
        """Explicit origins may extend, never silently discard inherited paths."""
        if explicit and any(t not in explicit for t in self.additional_causes):
            raise ValueError("publish must retain inherited causal paths")
        return checked_traces(explicit or self.additional_causes)
