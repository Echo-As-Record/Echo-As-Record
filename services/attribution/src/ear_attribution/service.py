"""先读取当前状态，再将业务操作与事件去重标记一起提交。

沿用 origin/attribution/initial-implementation 的可注入 Planner 框架。
Planner 必须根据当前状态判断是否已有归属、
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
    FinishReevaluation,
    MarkProcessed,
    Reader,
    StorageClient,
    WriteOp,
)

from ear_attribution.completion import finish
from ear_attribution.matching import MatchConfig
from ear_attribution.transactions import CommitJournal, Transactions

logger = logging.getLogger(__name__)


class Planner(Protocol):
    """只读快照、返回写入计划；冲突时可能再次调用，不能直接写库或执行外部动作。

    返回 None：无关事件，不记录已处理。
    返回空序列：新原始数据已处理且无需修改，仍记录已处理。
    重判必须提供 Finish，不能返回空序列或 None 冒充完成。
    返回操作序列：由服务原子提交；不要自行添加 MarkProcessed。
    """

    def __call__(
        self, reader: Reader, record: Record, event: Event
    ) -> Sequence[WriteOp] | None: ...


class AttributionService:
    """可注入策略框架；business() 接入已采纳 v1.2 的首个业务切片。"""

    def __init__(
        self,
        client: StorageClient,
        *,
        new_record: Planner,
        feedback: Planner,
        reevaluation: Planner,
        attempts: int = 5,
        journal: CommitJournal | None = None,
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
        self.transactions = Transactions(client, journal)

    @classmethod
    def business(
        cls,
        client: StorageClient,
        *,
        config: MatchConfig,
        attempts: int = 5,
        journal: CommitJournal | None = None,
    ) -> AttributionService:
        from ear_attribution.planning import BusinessPlanner

        registry = client.registry
        for name in ("attribution_state", "attribution_feedback_result"):
            spec = registry.get(name)
            if spec.writer != "attribution" or spec.kind != "system_record":
                raise ValueError("attribution system types require trusted registry integration")
        spec = registry.get("attribution")
        if not spec.supports_reevaluation or not spec.requires_dependencies:
            raise ValueError("attribution requires dependency and reevaluation capabilities")
        planner = BusinessPlanner(config)
        return cls(
            client,
            new_record=planner.new_record,
            feedback=planner.feedback,
            reevaluation=planner.reevaluation,
            attempts=attempts,
            journal=journal,
        )

    def subscribe(self, *, from_beginning: bool = False) -> None:
        self.transactions.recover_pending()
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
                    return [
                        finish(
                            event,
                            record,
                            "not_applicable",
                            reason="target_terminated",
                            disposition="terminated",
                        ),
                        MarkProcessed("attribution", event.event_id),
                    ]
            ops = planner(reader, record, event)
            if not isinstance(event, DataChanged) and not any(
                isinstance(op, FinishReevaluation) and op.result.request_event_id == event.event_id
                for op in (ops or ())
            ):
                raise ValueError("reevaluation planner must provide FinishReevaluation")
            if ops is None:
                return []
            return [*ops, MarkProcessed("attribution", event.event_id)]

        result = self.transactions.run(plan, context, event.event_id, self.attempts)
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
