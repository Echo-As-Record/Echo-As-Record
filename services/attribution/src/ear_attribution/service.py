"""先读取当前状态，再将业务操作与事件去重标记一起提交。

此层不解释尚待确认的 payload。Planner 必须根据当前状态判断是否已有归属、
是否为用户确认过的结果；返回 None 表示事件不归本策略处理。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Protocol

from ear_contracts import DataChanged, Event, RawRecord, Record, SemanticRecord
from ear_storage_client import (
    Context,
    MarkProcessed,
    Reader,
    StorageClient,
    WriteOp,
    write_with_retry,
)

logger = logging.getLogger(__name__)


class Planner(Protocol):
    """只读快照、返回写入计划；冲突时可能再次调用，不能直接写库或执行外部动作。

    返回 None：无关事件，不记录已处理。
    返回空序列：已处理且无需修改数据，仍记录已处理。
    返回操作序列：由服务原子提交；不要自行添加 MarkProcessed。
    """

    def __call__(
        self, reader: Reader, record: Record, event: Event
    ) -> Sequence[WriteOp] | None: ...


class AttributionService:
    """通过注入策略隔离待确认的字段和匹配算法，不提供假装完成业务的默认策略。"""

    def __init__(
        self,
        client: StorageClient,
        *,
        new_record: Planner,
        feedback: Planner,
        reevaluation: Planner,
        attempts: int = 5,
    ) -> None:
        if client.module != "attribution":
            raise ValueError("必须使用 attribution 身份的存储客户端")
        if attempts < 1:
            raise ValueError("attempts 至少为 1")
        self.client = client
        self.new_record = new_record
        self.feedback = feedback
        self.reevaluation = reevaluation
        self.attempts = attempts

    def subscribe(self, *, from_beginning: bool = False) -> None:
        self.client.subscribe("attribution", self.handle, from_beginning=from_beginning)

    def handle(self, event: Event) -> None:
        if isinstance(event, DataChanged):
            if event.operation != "created":
                return
            if event.data_type in {"voiceprint", "gps_point"}:
                planner = self.new_record
            elif event.data_type == "feedback":
                planner = self.feedback
            else:
                return
            record_id = event.data_id
            context = (
                Context.feedback(record_id) if event.data_type == "feedback" else Context.of(event)
            )
        else:
            if event.target_type != "attribution":
                return
            planner = self.reevaluation
            record_id = event.target_id
            context = Context.of(event)

        def plan(reader: Reader) -> Sequence[WriteOp]:
            if reader.is_processed("attribution", event.event_id):
                return []
            record = reader.get(record_id)
            if record is None:
                # 不静默确认缺失数据的事件，让总线按重试/死信机制处理。
                raise LookupError(f"事件目标不存在：{record_id}")
            if isinstance(event, DataChanged):
                if not isinstance(record, RawRecord) or record.data_type != event.data_type:
                    raise ValueError("事件与当前原始数据类型不一致")
            else:
                if not isinstance(record, SemanticRecord) or record.data_type != "attribution":
                    raise ValueError("重新判断目标不是归属关系")
                if record.superseded:
                    return [MarkProcessed("attribution", event.event_id)]
            ops = planner(reader, record, event)
            if ops is None:
                return []
            return [*ops, MarkProcessed("attribution", event.event_id)]

        result = write_with_retry(self.client, plan, context, attempts=self.attempts)
        if result.conflict is not None and result.conflict.reason != "already_processed":
            result.check()
        logger.info(
            json.dumps(
                {
                    "module": "attribution",
                    "event_id": event.event_id,
                    "root_feedback_id": context.root_feedback_id,
                    "depth": context.depth,
                    "message": "event_checked",
                }
            )
        )
