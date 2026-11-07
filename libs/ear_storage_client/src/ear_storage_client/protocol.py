"""① 的客户端接口。各模块只依赖这里的 Protocol，不依赖具体实现。

- 内存实现：ear_storage_client.memory.MemoryStorage，供开发和测试使用。
- 正式实现（数据库 + 总线）提供同样的接口，换实现时调用方不用改。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Protocol

from ear_contracts import (
    DependencyRecord,
    Event,
    Record,
    ReevaluationResult,
    Registry,
    SemanticRecord,
    SystemRecord,
)

from ear_storage_client.context import Context
from ear_storage_client.ops import WriteOp
from ear_storage_client.queries import DependencySet, Page, Query, QueryItem, StaleDependency
from ear_storage_client.results import WriteResult

EventHandler = Callable[[Event], None]


class Reader(Protocol):
    """读取接口。返回的对象是副本，修改它不影响 ①。"""

    def type_revision(self, data_type: str) -> int:
        """Monotonic actual-record-change counter (initially zero), not a watermark.

        Use the SAME snapshot Reader for this counter and the candidate query.
        Separate live reads do not capture a common consistency point.
        """
        ...

    def get(self, id: str) -> Record | None:
        """按 id 读当前版本（原始数据或有版本的数据）。已被整体替代的对象也能读到，
        其 status 为 superseded。系统记录用 get_system_record。"""
        ...

    def get_version(self, id: str, version: int) -> Record | None:
        """按 id 和版本号读任意历史版本。原始数据只有版本 1。"""
        ...

    def history(self, id: str) -> list[SemanticRecord]:
        """有版本的数据的全部版本，从版本 1 开始；原始数据或不存在时返回空列表。"""
        ...

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        """批量读当前版本，结果在同一时刻的状态上读出；找不到的 id 不出现在结果里。"""
        ...

    def query(self, query: Query) -> Page: ...

    def count(self, query: Query) -> int:
        """满足条件的数量（忽略 limit 和 cursor）。"""
        ...

    def get_reevaluation_result(self, request_event_id: str) -> ReevaluationResult | None:
        """Read only registered Finish results, subject to result-type read permission.

        Snapshots use their captured completion index; legacy snapshots without
        that index fail explicitly rather than reporting false absence.
        """
        ...

    def get_system_record(self, id: str) -> SystemRecord | None: ...

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        """依赖了 targets 中某个对象且已过时的登记：全对象按版本，字段模式按完整 JSON 值。
        被依赖方整体替代始终使登记过时。

        only_current_dependents=True（默认）：只看依赖方当前版本的登记，且依赖方未被整体替代。
        """
        ...

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        """完整依赖集及最近实际写入的登记元数据；默认取当前版本。

        从未登记返回 None；显式空集返回 empty=True 的 DependencySet。
        返回值是副本；重复同集登记为 unchanged，不改变 registered_at。
        """
        ...

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        """依赖方某个版本（默认当前版本）登记的全部依赖。"""
        ...

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        """依赖了 target_id（任意版本）的全部依赖登记，不论是否过时。"""
        ...

    def is_processed(self, consumer: str, event_id: str) -> bool:
        """消费方是否已用 MarkProcessed 记录过这个事件。"""
        ...


class StorageClient(Reader, Protocol):
    """绑定到一个调用方模块的客户端。写入权限按这个模块名检查。"""

    @property
    def module(self) -> str: ...

    @property
    def registry(self) -> Registry: ...

    def write(self, ops: Sequence[WriteOp], context: Context) -> WriteResult:
        """原子写入一批操作。要么全部生效，要么全部不生效（返回 conflict 或抛出异常）。"""
        ...

    def snapshot(self) -> AbstractContextManager[Reader]:
        """在 with 块内，所有读取都基于同一时刻的状态。"""
        ...

    def subscribe(
        self,
        consumer: str,
        handler: EventHandler,
        *,
        event_types: Sequence[str] | None = None,
        from_beginning: bool = False,
    ) -> None:
        """订阅总线事件。consumer 必须是本模块名，或以「模块名.」开头；同一 consumer 只能订阅一次。

        同一事件可能送达多次；handler 抛出异常时会重投，超过次数进入死信。
        event_types 为 None 时接收全部事件；不认识的事件类型应忽略。
        """
        ...


def iter_query(reader: Reader, query: Query) -> Iterator[QueryItem]:
    """逐页读完全部结果。"""
    cursor = query.cursor
    while True:
        page = reader.query(replace(query, cursor=cursor))
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def write_with_retry(
    client: StorageClient,
    plan: Callable[[Reader], Sequence[WriteOp]],
    context: Context,
    *,
    attempts: int = 5,
) -> WriteResult:
    """「读取 → 计算 → 原子写入」，遇到可重试的冲突时重新读取、重新计算。

    plan 在一个快照上读取并返回要写入的操作；返回空列表表示无事可做。
    最多尝试 attempts 次，返回最后一次的结果（可能仍有 conflict）。
    """
    if attempts < 1:
        raise ValueError("attempts 至少为 1")
    result = WriteResult(batch_id=None)
    for _ in range(attempts):
        with client.snapshot() as reader:
            ops = list(plan(reader))
        if not ops:
            return WriteResult(batch_id=None)
        result = client.write(ops, context)
        if result.conflict is None or not result.conflict.retryable:
            return result
    return result
