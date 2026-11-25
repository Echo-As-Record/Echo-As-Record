"""L1/L2 桶的存储：登记类型、写日桶、由日桶汇总周桶和月桶。

- 桶类型 habit_bucket 由本模块（habit）独占写。bucket_key 单字段唯一，保证
  同一人物同一周期只有一个桶；requires_dependencies=True，每次写入同批登记依赖。
- 周桶、月桶都直接由日桶合并，只有一层依赖，不串联，传播深度因此固定。
- 日桶依赖什么（归属关系等）由调用方传入；归属 payload 的字段名仍待 ③ 确认，
  这里不写死。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from ear_contracts import DataTypeSpec, Registry, SemanticRecord, VersionRef, new_id
from ear_storage_client import (
    Create,
    DependencyInput,
    Query,
    Reader,
    RegisterDependencies,
    TimeRange,
    Update,
    WriteOp,
    iter_query,
)

from ear_habit.aggregate import Scale, bucket_key, period_range, rollup
from ear_habit.buckets import Stats

MODULE = "habit"
BUCKET_TYPE = "habit_bucket"
EPISODE_TYPE = "memory_episode"

HABIT_SPEC = DataTypeSpec(
    BUCKET_TYPE,
    "semantic",
    MODULE,
    unique_fields=("bucket_key",),
    requires_dependencies=True,
    supports_reevaluation=True,
    description="习惯扩展：人物 × 周期的说话统计桶（日、周、月）",
)

EPISODE_SPEC = DataTypeSpec(
    EPISODE_TYPE,
    "semantic",
    MODULE,
    unique_fields=("activity_id",),
    requires_dependencies=True,
    supports_reevaluation=True,
    description="活动级可回查记忆索引",
)
COMPLETION_SPEC = DataTypeSpec(
    "habit_completion", "system_record", MODULE, reevaluation_result=True
)


def scope_key(since: datetime | None) -> str | None:
    if since is None:
        return None
    if since.utcoffset() != timedelta(0):
        raise ValueError("scope start must be UTC")
    return since.isoformat()


def check_scope(record: SemanticRecord, scope: str | None) -> None:
    if record.payload.get("scope_start") != scope:
        raise ValueError("existing memory uses a different ingestion scope")


def extend_registry(registry: Registry) -> Registry:
    """在构造存储时传入，插件类型不能运行时动态登记。"""
    return registry.extend(HABIT_SPEC, EPISODE_SPEC, COMPLETION_SPEC)


def find_bucket(reader: Reader, key: str) -> SemanticRecord | None:
    page = reader.query(Query(BUCKET_TYPE, where={"bucket_key": key}, limit=2))
    items = [i for i in page.items if isinstance(i, SemanticRecord)]
    if len(items) > 1:
        raise RuntimeError(f"bucket_key {key!r} 对应多个当前桶，唯一约束被破坏")
    return items[0] if items else None


def upsert_bucket_ops(
    reader: Reader,
    person_id: str,
    scale: Scale,
    period: str,
    stats: Stats,
    depends_on: Iterable[VersionRef | DependencyInput] = (),
    *,
    evidence_ids: Iterable[str] | None = None,
    scope_start: str | None = None,
) -> list[WriteOp]:
    """新建或更新一个桶，并在同批整体登记依赖（空集也要显式登记）。"""
    key = bucket_key(person_id, scale, period)
    start, end = period_range(period, scale)
    payload = {
        "bucket_key": key,
        "person_id": person_id,
        "scale": scale,
        "period": period,
        "stats": stats,
        "scope_start": scope_start,
    }
    if evidence_ids is not None:
        payload["evidence_ids"] = sorted(set(evidence_ids))
    current = find_bucket(reader, key)
    if current is None:
        bucket_id = new_id()
        write: WriteOp = Create(
            BUCKET_TYPE, bucket_id, payload, dedup_key=key, event_start=start, event_end=end
        )
    else:
        check_scope(current, scope_start)
        bucket_id = current.id
        write = Update(
            BUCKET_TYPE,
            bucket_id,
            current.version,
            payload,
            event_start=start,
            event_end=end,
        )
    return [write, RegisterDependencies(BUCKET_TYPE, bucket_id, list(depends_on))]


def day_buckets(reader: Reader, person_id: str, start, end) -> list[SemanticRecord]:
    """[start, end) 内某人物的日桶，按时间、id 排序，结果稳定。"""
    query = Query(
        BUCKET_TYPE, time=TimeRange(start, end), where={"person_id": person_id, "scale": "day"}
    )
    items = [r for r in iter_query(reader, query) if isinstance(r, SemanticRecord)]
    return sorted(items, key=lambda r: (r.payload["period"], r.id))


def rollup_ops(
    reader: Reader, person_id: str, scale: Scale, period: str, *, since: datetime | None = None
) -> list[WriteOp]:
    """由日桶汇总周桶或月桶；没有日桶时不建桶，返回空列表。"""
    if scale == "day":
        raise ValueError("日桶由原始数据统计，不是汇总得到")
    start, end = period_range(period, scale)
    children = day_buckets(reader, person_id, start, end)
    if not children:
        return []
    scope = scope_key(since)
    for child in children:
        check_scope(child, scope)
    stats = rollup(c.payload["stats"] for c in children)
    deps = [
        DependencyInput(VersionRef(data_type=BUCKET_TYPE, id=c.id, version=c.version), ("stats",))
        for c in children
    ]
    return upsert_bucket_ops(reader, person_id, scale, period, stats, deps, scope_start=scope)
