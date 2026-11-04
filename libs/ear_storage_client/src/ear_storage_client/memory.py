"""① 的内存实现：供各模块开发和测试使用，语义与正式实现相同。

    storage = MemoryStorage()
    attribution = storage.client("attribution")
    attribution.subscribe("attribution", handler)
    adapter = storage.client("adapter")
    adapter.write([AppendRaw(make_raw(...))], Context.system())
    storage.run_until_idle()   # 投递事件，直到没有待投递的事件

- 状态写时复制：一批写入在副本上执行，全部成功才替换，因此天然原子；快照就是旧状态的引用。
- 事件在提交后进入进程内总线，由 run_until_idle() 同步投递。duplicate_delivery=True 时，
  每个事件成功处理后还会再投递一次，用来测试「同一事件送达多次，结果和送达一次相同」。
- 不持久化，不适合大数据量。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

from ear_contracts import (
    DEFAULT_REGISTRY,
    DataChanged,
    DataTypeSpec,
    DependencyRecord,
    Event,
    RawRecord,
    Record,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    SystemRecord,
    UnknownDataTypeError,
    VersionRef,
    new_id,
    utcnow,
)
from pydantic import BaseModel, ValidationError

from ear_storage_client.context import Context
from ear_storage_client.errors import InvalidRequest, PermissionDenied
from ear_storage_client.ops import (
    AppendRaw,
    Create,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)
from ear_storage_client.protocol import EventHandler, Reader
from ear_storage_client.queries import Page, Query, QueryItem, StaleDependency
from ear_storage_client.results import Conflict, ConflictReason, OpResult, WriteResult


def canonical_json(value: Any) -> str:
    """内容比较用的规范形式：键排序、不转义非 ASCII；字符串逐字保留。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _json_payload(payload: Any) -> dict[str, Any]:
    try:
        data = json.loads(canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"payload 必须是可序列化为 JSON 的对象：{exc}") from None
    if not isinstance(data, dict):
        raise InvalidRequest("payload 必须是 JSON 对象")
    return data


def _build[M: BaseModel](model: type[M], **fields: Any) -> M:
    try:
        return model(**fields)
    except ValidationError as exc:
        raise InvalidRequest(str(exc)) from None


def _copy[M: BaseModel](record: M) -> M:
    return record.model_copy(deep=True)


# ---------------------------------------------------------------------------
# 状态


@dataclass(slots=True)
class _State:
    raw: dict[str, RawRecord] = field(default_factory=dict)
    raw_keys: dict[tuple[str, str, str], str] = field(default_factory=dict)
    versions: dict[str, tuple[SemanticRecord, ...]] = field(default_factory=dict)
    semantic_keys: dict[tuple[str, str], str] = field(default_factory=dict)
    system: dict[str, SystemRecord] = field(default_factory=dict)
    deps: dict[tuple[str, int], tuple[DependencyRecord, ...]] = field(default_factory=dict)
    deps_by_target: dict[str, frozenset[tuple[str, int]]] = field(default_factory=dict)
    processed: dict[tuple[str, str], None] = field(default_factory=dict)
    published: dict[str, None] = field(default_factory=dict)

    def copy(self) -> _State:
        return _State(
            dict(self.raw),
            dict(self.raw_keys),
            dict(self.versions),
            dict(self.semantic_keys),
            dict(self.system),
            dict(self.deps),
            dict(self.deps_by_target),
            dict(self.processed),
            dict(self.published),
        )

    def current(self, id: str) -> Record | None:
        if id in self.raw:
            return self.raw[id]
        versions = self.versions.get(id)
        return versions[-1] if versions else None

    def id_in_use(self, id: str) -> bool:
        return id in self.raw or id in self.versions or id in self.system


# ---------------------------------------------------------------------------
# 读取


class _StateReader:
    """在一个固定状态上实现 Reader。"""

    def __init__(self, state: _State, registry: Registry) -> None:
        self._s = state
        self._registry = registry

    def _spec(self, data_type: str) -> DataTypeSpec:
        try:
            return self._registry.get(data_type)
        except UnknownDataTypeError as exc:
            raise InvalidRequest(str(exc)) from None

    def get(self, id: str) -> Record | None:
        record = self._s.current(id)
        return None if record is None else _copy(record)

    def get_version(self, id: str, version: int) -> Record | None:
        if id in self._s.raw:
            return _copy(self._s.raw[id]) if version == 1 else None
        versions = self._s.versions.get(id, ())
        if 1 <= version <= len(versions):
            return _copy(versions[version - 1])
        return None

    def history(self, id: str) -> list[SemanticRecord]:
        return [_copy(v) for v in self._s.versions.get(id, ())]

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        found: dict[str, Record] = {}
        for id in ids:
            record = self._s.current(id)
            if record is not None:
                found[id] = _copy(record)
        return found

    def get_system_record(self, id: str) -> SystemRecord | None:
        record = self._s.system.get(id)
        return None if record is None else _copy(record)

    def _select(self, query: Query) -> list[QueryItem]:
        spec = self._spec(query.data_type)
        if query.source_user is not None and spec.kind != "raw_record":
            raise InvalidRequest("source_user 只用于原始数据")
        excluded: set[str] = set()
        if query.unreferenced_by is not None:
            ref_type, ref_field = query.unreferenced_by
            if not self._spec(ref_type).versioned:
                raise InvalidRequest("unreferenced_by 只能引用有版本的数据类型")
            for versions in self._s.versions.values():
                last = versions[-1]
                if last.data_type == ref_type and not last.superseded:
                    value = last.payload.get(ref_field)
                    if isinstance(value, str):
                        excluded.add(value)

        items: list[QueryItem]
        if spec.kind == "raw_record":
            raws = [r for r in self._s.raw.values() if r.data_type == query.data_type]
            if query.source_user is not None:
                raws = [r for r in raws if r.source.user == query.source_user]
            items = list(raws)
        elif spec.versioned:
            items = [
                vs[-1]
                for vs in self._s.versions.values()
                if vs[-1].data_type == query.data_type
                and (query.include_superseded or not vs[-1].superseded)
            ]
        else:
            items = [r for r in self._s.system.values() if r.data_type == query.data_type]

        def keep(item: QueryItem) -> bool:
            if item.id in excluded:
                return False
            if query.time is not None:
                if item.event_start is None or item.event_end is None:
                    return False
                if not query.time.overlaps(item.event_start, item.event_end):
                    return False
            for key, value in query.where.items():
                if key not in item.payload or canonical_json(item.payload[key]) != canonical_json(
                    value
                ):
                    return False
            for key, value in query.contains.items():
                members = item.payload.get(key)
                if not isinstance(members, list):
                    return False
                wanted = canonical_json(value)
                if all(canonical_json(m) != wanted for m in members):
                    return False
            return True

        items = [i for i in items if keep(i)]
        if query.order == "time":
            items.sort(key=lambda i: (i.event_start is None, i.event_start or datetime.min, i.id))
        else:
            items.sort(key=lambda i: i.id)
        return items

    def query(self, query: Query) -> Page:
        items = self._select(query)
        try:
            offset = int(query.cursor) if query.cursor is not None else 0
        except ValueError:
            raise InvalidRequest("cursor 无效") from None
        if offset < 0:
            raise InvalidRequest("cursor 无效")
        page = items[offset : offset + query.limit]
        end = offset + len(page)
        return Page(
            items=tuple(_copy(i) for i in page),
            next_cursor=str(end) if end < len(items) else None,
        )

    def count(self, query: Query) -> int:
        return len(self._select(query))

    def _current_dependent(self, dependent_id: str, version: int) -> bool:
        versions = self._s.versions.get(dependent_id)
        if not versions:
            return False
        return versions[-1].version == version and not versions[-1].superseded

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        stale: list[StaleDependency] = []
        for target_id in dict.fromkeys(targets):
            target = self._s.current(target_id)
            if target is None:
                continue
            superseded = isinstance(target, SemanticRecord) and target.superseded
            for key in sorted(self._s.deps_by_target.get(target_id, frozenset())):
                if only_current_dependents and not self._current_dependent(*key):
                    continue
                for dep in self._s.deps.get(key, ()):
                    if dep.depends_on.id == target_id and dep.depends_on.version < target.version:
                        stale.append(
                            StaleDependency(
                                dependent=dep.dependent.model_copy(),
                                depends_on=dep.depends_on.model_copy(),
                                current_version=target.version,
                                target_superseded=superseded,
                            )
                        )
        return stale

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        if version is None:
            versions = self._s.versions.get(dependent_id)
            if not versions:
                return []
            version = versions[-1].version
        return [_copy(d) for d in self._s.deps.get((dependent_id, version), ())]

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        found: list[DependencyRecord] = []
        for key in sorted(self._s.deps_by_target.get(target_id, frozenset())):
            if only_current_dependents and not self._current_dependent(*key):
                continue
            found.extend(
                _copy(d) for d in self._s.deps.get(key, ()) if d.depends_on.id == target_id
            )
        return found

    def is_processed(self, consumer: str, event_id: str) -> bool:
        return (consumer, event_id) in self._s.processed


# ---------------------------------------------------------------------------
# 写入


class _Abort(Exception):
    def __init__(self, conflict: Conflict) -> None:
        super().__init__(conflict.message)
        self.conflict = conflict


class _Txn:
    """在状态副本上执行一批操作。

    遇到冲突抛 _Abort；请求错误抛 InvalidRequest / PermissionDenied。
    """

    def __init__(
        self,
        state: _State,
        registry: Registry,
        module: str,
        context: Context,
        now: datetime,
        batch_id: str,
    ) -> None:
        self.s = state
        self.registry = registry
        self.module = module
        self.context = context
        self.now = now
        self.batch_id = batch_id
        self.results: list[OpResult] = []
        self.events: list[Event] = []
        self.touched: dict[str, int] = {}  # 本批写过的有版本对象 → 最后一条相关操作的序号
        self.index = 0

    # -- 工具 --

    def conflict(
        self,
        reason: ConflictReason,
        message: str,
        *,
        data_type: str | None = None,
        id: str | None = None,
        **details: Any,
    ) -> NoReturn:
        raise _Abort(Conflict(self.index, reason, message, data_type, id, details))

    def spec(self, data_type: str) -> DataTypeSpec:
        try:
            return self.registry.get(data_type)
        except UnknownDataTypeError as exc:
            raise InvalidRequest(f"第 {self.index} 条操作：{exc}") from None

    def require_writer(self, spec: DataTypeSpec) -> None:
        if spec.writer != self.module:
            raise PermissionDenied(
                f"第 {self.index} 条操作：{self.module} 不能写 {spec.name}"
                f"（负责模块：{spec.writer}）"
            )

    def invalid(self, message: str) -> NoReturn:
        raise InvalidRequest(f"第 {self.index} 条操作：{message}")

    def result(
        self,
        outcome: Any,
        data_type: str | None,
        id: str | None,
        version: int | None,
        previous: int | None = None,
        event: Event | None = None,
    ) -> None:
        if event is not None:
            self.events.append(event)
        self.results.append(
            OpResult(
                self.index,
                outcome,
                data_type,
                id,
                version,
                previous,
                None if event is None else event.event_id,
            )
        )

    def changed(
        self,
        data_type: str,
        id: str,
        operation: str,
        new_version: int,
        old_version: int | None,
        skip_history: bool,
    ) -> DataChanged:
        return _build(
            DataChanged,
            event_id=new_id(),
            occurred_at=self.now,
            data_type=data_type,
            data_id=id,
            operation=operation,
            new_version=new_version,
            old_version=old_version,
            root_feedback_id=self.context.root_feedback_id,
            depth=self.context.depth,
            skip_history=skip_history,
            writer=self.module,
            batch_id=self.batch_id,
        )

    def time_range(
        self,
        spec: DataTypeSpec,
        payload: dict[str, Any],
        start: datetime | None,
        end: datetime | None,
        current: SemanticRecord | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        if spec.time_from is not None:
            if start is not None or end is not None:
                self.invalid(f"{spec.name} 的时间由 ① 从 payload.{spec.time_from} 复制，写入方不填")
            subject = payload.get(spec.time_from)
            raw = self.s.raw.get(subject) if isinstance(subject, str) else None
            if raw is None:
                self.invalid(f"payload.{spec.time_from} 必须是已存在的原始数据 id")
            return raw.event_start, raw.event_end
        if start is None and end is None and current is not None:
            return current.event_start, current.event_end
        if (start is None) != (end is None):
            self.invalid("event_start、event_end 必须同时给出")
        if start is not None and end is not None and end < start:
            self.invalid("event_end 早于 event_start")
        return start, end

    def current_versioned(self, data_type: str, id: str) -> SemanticRecord:
        versions = self.s.versions.get(id)
        if not versions:
            self.conflict("not_found", f"{data_type} {id} 不存在", data_type=data_type, id=id)
        current = versions[-1]
        if current.data_type != data_type:
            self.invalid(f"{id} 的类型是 {current.data_type}，不是 {data_type}")
        return current

    def push_version(self, record: SemanticRecord) -> None:
        self.s.versions[record.id] = (*self.s.versions.get(record.id, ()), record)
        self.touched[record.id] = self.index

    def new_version(
        self,
        current: SemanticRecord | None,
        *,
        data_type: str,
        id: str,
        status: str,
        payload: dict[str, Any],
        start: datetime | None,
        end: datetime | None,
        dedup_key: str | None,
        skip_history: bool,
    ) -> SemanticRecord:
        return _build(
            SemanticRecord,
            id=id,
            data_type=data_type,
            version=1 if current is None else current.version + 1,
            status=status,
            event_start=start,
            event_end=end,
            dedup_key=dedup_key,
            payload=payload,
            writer=self.module,
            root_feedback_id=self.context.root_feedback_id,
            depth=self.context.depth,
            skip_history=skip_history,
            batch_id=self.batch_id,
            created_at=self.now,
        )

    # -- 操作 --

    def run(self, ops: Sequence[WriteOp]) -> None:
        handlers: dict[type, Callable[[Any], None]] = {
            AppendRaw: self.append_raw,
            Create: self.create,
            Update: self.update,
            Supersede: self.supersede,
            RegisterDependencies: self.register,
            PutSystemRecord: self.put_system,
            PublishReevaluation: self.publish,
            MarkProcessed: self.mark_processed,
        }
        for self.index, op in enumerate(ops):
            handler = handlers.get(type(op))
            if handler is None:
                self.invalid(f"未知的操作：{type(op).__name__}")
            handler(op)
        self.check_constraints()

    def append_raw(self, op: AppendRaw) -> None:
        rec = op.record
        spec = self.spec(rec.data_type)
        if spec.kind != "raw_record":
            self.invalid(f"{rec.data_type} 不是原始数据，不能用 AppendRaw")
        self.require_writer(spec)
        key = (rec.data_type, rec.adapter_version, rec.dedup_key)
        existing = self.s.raw_keys.get(key)
        if existing is not None:
            self.result("deduplicated", rec.data_type, existing, 1)
            return
        if self.s.id_in_use(rec.id):
            self.conflict("id_exists", f"id {rec.id} 已被使用", data_type=rec.data_type, id=rec.id)
        fields = rec.model_dump()
        fields["payload"] = _json_payload(rec.payload)
        stored = _build(RawRecord, **fields, version=1, ingested_at=self.now)
        self.s.raw[rec.id] = stored
        self.s.raw_keys[key] = rec.id
        event = self.changed(rec.data_type, rec.id, "created", 1, None, False)
        self.result("written", rec.data_type, rec.id, 1, None, event)

    def create(self, op: Create) -> None:
        spec = self.spec(op.data_type)
        if not spec.versioned:
            self.invalid(f"{op.data_type} 不是有版本的数据，不能用 Create")
        self.require_writer(spec)
        payload = _json_payload(op.payload)
        if self.s.id_in_use(op.id):
            self.conflict("id_exists", f"id {op.id} 已被使用", data_type=op.data_type, id=op.id)
        if op.dedup_key is not None:
            existing = self.s.semantic_keys.get((op.data_type, op.dedup_key))
            if existing is not None:
                self.conflict(
                    "dedup_key_exists",
                    f"{op.data_type} 的去重键 {op.dedup_key!r} 已被 {existing} 使用",
                    data_type=op.data_type,
                    id=op.id,
                    existing_id=existing,
                )
        start, end = self.time_range(spec, payload, op.event_start, op.event_end)
        record = self.new_version(
            None,
            data_type=op.data_type,
            id=op.id,
            status="active",
            payload=payload,
            start=start,
            end=end,
            dedup_key=op.dedup_key,
            skip_history=op.skip_history,
        )
        self.push_version(record)
        if op.dedup_key is not None:
            self.s.semantic_keys[(op.data_type, op.dedup_key)] = op.id
        event = self.changed(op.data_type, op.id, "created", 1, None, op.skip_history)
        self.result("written", op.data_type, op.id, 1, None, event)

    def update(self, op: Update) -> None:
        spec = self.spec(op.data_type)
        if not spec.versioned:
            self.invalid(f"{op.data_type} 不是有版本的数据，不能用 Update")
        self.require_writer(spec)
        payload = _json_payload(op.payload)
        current = self.current_versioned(op.data_type, op.id)
        if current.superseded:
            self.conflict(
                "object_superseded",
                f"{op.data_type} {op.id} 已被整体替代",
                data_type=op.data_type,
                id=op.id,
                current_version=current.version,
            )
        if current.version != op.expected_version:
            self.conflict(
                "version_mismatch",
                f"{op.data_type} {op.id} 的当前版本是 {current.version}，"
                f"不是 {op.expected_version}",
                data_type=op.data_type,
                id=op.id,
                expected_version=op.expected_version,
                current_version=current.version,
            )
        start, end = self.time_range(spec, payload, op.event_start, op.event_end, current)
        if (
            canonical_json(payload) == canonical_json(current.payload)
            and start == current.event_start
            and end == current.event_end
        ):
            self.result("unchanged", op.data_type, op.id, current.version, current.version)
            return
        record = self.new_version(
            current,
            data_type=op.data_type,
            id=op.id,
            status="active",
            payload=payload,
            start=start,
            end=end,
            dedup_key=current.dedup_key,
            skip_history=op.skip_history,
        )
        self.push_version(record)
        event = self.changed(
            op.data_type, op.id, "updated", record.version, current.version, op.skip_history
        )
        self.result("written", op.data_type, op.id, record.version, current.version, event)

    def supersede(self, op: Supersede) -> None:
        spec = self.spec(op.data_type)
        if not spec.versioned:
            self.invalid(f"{op.data_type} 不是有版本的数据，不能整体替代")
        self.require_writer(spec)
        current = self.current_versioned(op.data_type, op.id)
        if current.superseded:
            self.result("unchanged", op.data_type, op.id, current.version, current.version)
            return
        if current.version != op.expected_version:
            self.conflict(
                "version_mismatch",
                f"{op.data_type} {op.id} 的当前版本是 {current.version}，"
                f"不是 {op.expected_version}",
                data_type=op.data_type,
                id=op.id,
                expected_version=op.expected_version,
                current_version=current.version,
            )
        record = self.new_version(
            current,
            data_type=op.data_type,
            id=op.id,
            status="superseded",
            payload=current.payload,
            start=current.event_start,
            end=current.event_end,
            dedup_key=current.dedup_key,
            skip_history=op.skip_history,
        )
        self.push_version(record)
        event = self.changed(
            op.data_type, op.id, "superseded", record.version, current.version, op.skip_history
        )
        self.result("written", op.data_type, op.id, record.version, current.version, event)

    def register(self, op: RegisterDependencies) -> None:
        spec = self.spec(op.dependent_type)
        if not spec.versioned:
            self.invalid("只能为有版本的数据登记依赖")
        self.require_writer(spec)
        current = self.current_versioned(op.dependent_type, op.dependent_id)
        if current.superseded:
            self.conflict(
                "object_superseded",
                f"{op.dependent_type} {op.dependent_id} 已被整体替代，不能再登记依赖",
                data_type=op.dependent_type,
                id=op.dependent_id,
            )
        version = current.version if op.dependent_version is None else op.dependent_version
        if version != current.version:
            self.conflict(
                "version_mismatch",
                f"只能为当前版本 {current.version} 登记依赖，不是 {version}",
                data_type=op.dependent_type,
                id=op.dependent_id,
                expected_version=version,
                current_version=current.version,
            )
        refs: dict[str, VersionRef] = {}
        for ref in op.depends_on:
            if ref.id == op.dependent_id:
                self.invalid("不能依赖自己")
            target = self.s.current(ref.id)
            if target is None:
                self.conflict(
                    "not_found",
                    f"被依赖的 {ref.data_type} {ref.id} 不存在",
                    data_type=ref.data_type,
                    id=ref.id,
                )
            if target.data_type != ref.data_type:
                self.invalid(f"{ref.id} 的类型是 {target.data_type}，不是 {ref.data_type}")
            superseded = isinstance(target, SemanticRecord) and target.superseded
            if superseded or target.version != ref.version:
                self.conflict(
                    "stale_dependency",
                    f"{ref.data_type} {ref.id} 的当前版本是 {target.version}"
                    f"{'（已被整体替代）' if superseded else ''}，不能登记版本 {ref.version}",
                    data_type=ref.data_type,
                    id=ref.id,
                    registered_version=ref.version,
                    current_version=target.version,
                    target_superseded=superseded,
                )
            refs[ref.id] = VersionRef(data_type=ref.data_type, id=ref.id, version=ref.version)

        key = (op.dependent_id, version)
        old = self.s.deps.get(key, ())
        old_set = {(d.depends_on.id, d.depends_on.version) for d in old}
        new_set = {(r.id, r.version) for r in refs.values()}
        if old_set == new_set and old:
            self.result("unchanged", op.dependent_type, op.dependent_id, version)
            return
        dependent = VersionRef(data_type=op.dependent_type, id=op.dependent_id, version=version)
        self.s.deps[key] = tuple(
            DependencyRecord(
                dependent=dependent,
                depends_on=ref,
                registered_by=self.module,
                registered_at=self.now,
            )
            for ref in sorted(refs.values(), key=lambda r: r.id)
        )
        for target_id in {d.depends_on.id for d in old} - set(refs):
            remaining = self.s.deps_by_target.get(target_id, frozenset()) - {key}
            if remaining:
                self.s.deps_by_target[target_id] = remaining
            else:
                self.s.deps_by_target.pop(target_id, None)
        for target_id in refs:
            self.s.deps_by_target[target_id] = self.s.deps_by_target.get(target_id, frozenset()) | {
                key
            }
        self.result("written", op.dependent_type, op.dependent_id, version)

    def put_system(self, op: PutSystemRecord) -> None:
        spec = self.spec(op.data_type)
        if spec.kind != "system_record":
            self.invalid(f"{op.data_type} 不是系统记录，不能用 PutSystemRecord")
        self.require_writer(spec)
        payload = _json_payload(op.payload)
        current = self.s.system.get(op.id)
        if op.expected_revision is None:
            if self.s.id_in_use(op.id):
                self.conflict("id_exists", f"id {op.id} 已被使用", data_type=op.data_type, id=op.id)
            start, end = self.time_range(spec, payload, op.event_start, op.event_end)
            self.s.system[op.id] = _build(
                SystemRecord,
                id=op.id,
                data_type=op.data_type,
                revision=1,
                event_start=start,
                event_end=end,
                payload=payload,
                writer=self.module,
                created_at=self.now,
                updated_at=self.now,
            )
            self.result("written", op.data_type, op.id, 1)
            return
        if current is None:
            self.conflict(
                "not_found", f"{op.data_type} {op.id} 不存在", data_type=op.data_type, id=op.id
            )
        if current.data_type != op.data_type:
            self.invalid(f"{op.id} 的类型是 {current.data_type}，不是 {op.data_type}")
        if current.revision != op.expected_revision:
            self.conflict(
                "revision_mismatch",
                f"{op.data_type} {op.id} 的 revision 是 {current.revision}，"
                f"不是 {op.expected_revision}",
                data_type=op.data_type,
                id=op.id,
                expected_revision=op.expected_revision,
                current_revision=current.revision,
            )
        if op.event_start is None and op.event_end is None:
            start, end = current.event_start, current.event_end
        else:
            start, end = self.time_range(spec, payload, op.event_start, op.event_end)
        # 与新建一样经模型校验（UTC 等）。先校验再比较，同一时刻的非 UTC 输入也不能借“无变化”跳过。
        candidate = _build(
            SystemRecord,
            id=current.id,
            data_type=current.data_type,
            revision=current.revision,
            event_start=start,
            event_end=end,
            payload=payload,
            writer=current.writer,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        if (
            canonical_json(candidate.payload) == canonical_json(current.payload)
            and candidate.event_start == current.event_start
            and candidate.event_end == current.event_end
        ):
            self.result("unchanged", op.data_type, op.id, current.revision, current.revision)
            return
        self.s.system[op.id] = _build(
            SystemRecord,
            **{
                **candidate.model_dump(),
                "revision": current.revision + 1,
                "writer": self.module,
                "updated_at": self.now,
            },
        )
        self.result("written", op.data_type, op.id, current.revision + 1, current.revision)

    def publish(self, op: PublishReevaluation) -> None:
        if self.module not in self.registry.publishers_of("reevaluation_requested"):
            raise PermissionDenied(f"{self.module} 不能发布 reevaluation_requested")
        if self.context.depth < 1:
            self.invalid("「请求重新判断」的 depth 至少为 1，请用 Context.deeper()")
        self.spec(op.target_type)
        target = self.s.current(op.target_id)
        if target is None:
            self.conflict(
                "not_found",
                f"目标 {op.target_type} {op.target_id} 不存在",
                data_type=op.target_type,
                id=op.target_id,
            )
        if target.data_type != op.target_type:
            self.invalid(f"{op.target_id} 的类型是 {target.data_type}，不是 {op.target_type}")
        event_id = op.event_id or new_id()
        if event_id in self.s.published:
            self.result("deduplicated", op.target_type, op.target_id, None)
            return
        event = _build(
            ReevaluationRequested,
            event_id=event_id,
            occurred_at=self.now,
            target_type=op.target_type,
            target_id=op.target_id,
            cause=op.cause,
            change_description=op.change_description,
            root_feedback_id=self.context.root_feedback_id,
            depth=self.context.depth,
        )
        self.s.published[event_id] = None
        self.result("written", op.target_type, op.target_id, None, None, event)

    def mark_processed(self, op: MarkProcessed) -> None:
        if op.consumer != self.module and not op.consumer.startswith(self.module + "."):
            raise PermissionDenied(f"consumer 必须是 {self.module} 或以 {self.module}. 开头")
        key = (op.consumer, op.event_id)
        if key in self.s.processed:
            self.conflict(
                "already_processed",
                f"{op.consumer} 已处理过事件 {op.event_id}",
                id=op.event_id,
                consumer=op.consumer,
            )
        self.s.processed[key] = None
        self.result("written", None, op.event_id, None)

    # -- 批末检查 --

    def check_constraints(self) -> None:
        """唯一字段、互斥成员：在整批执行完之后检查，所以同一批里可以先移出再加入。"""
        by_type: dict[str, list[str]] = {}
        for id in self.touched:
            by_type.setdefault(self.s.versions[id][-1].data_type, []).append(id)
        for data_type, ids in by_type.items():
            spec = self.registry.get(data_type)
            if not (spec.unique_fields or spec.exclusive_members):
                continue
            active = [
                vs[-1]
                for vs in self.s.versions.values()
                if vs[-1].data_type == data_type and not vs[-1].superseded
            ]
            touched = set(ids)
            for field_name in spec.unique_fields:
                owners: dict[str, list[str]] = {}
                for record in active:
                    value = record.payload.get(field_name)
                    if value is not None:
                        owners.setdefault(canonical_json(value), []).append(record.id)
                self.report(data_type, field_name, owners, touched, "unique_field_taken")
            for field_name in spec.exclusive_members:
                owners = {}
                for record in active:
                    members = record.payload.get(field_name, [])
                    if not isinstance(members, list):
                        self.index = self.touched.get(record.id, self.index)
                        self.invalid(f"{data_type}.{field_name} 必须是数组")
                    for member in members:
                        owners.setdefault(canonical_json(member), []).append(record.id)
                self.report(data_type, field_name, owners, touched, "member_taken")

    def report(
        self,
        data_type: str,
        field_name: str,
        owners: dict[str, list[str]],
        touched: set[str],
        reason: ConflictReason,
    ) -> None:
        for value, ids in owners.items():
            if len(ids) > 1 and touched.intersection(ids):
                id = next(i for i in ids if i in touched)
                self.index = self.touched[id]
                self.conflict(
                    reason,
                    f"{data_type}.{field_name} 的值 {value} 同时出现在 {sorted(set(ids))}",
                    data_type=data_type,
                    id=id,
                    field=field_name,
                    value=json.loads(value),
                    owners=sorted(set(ids)),
                )


# ---------------------------------------------------------------------------
# 进程内总线


@dataclass(frozen=True, slots=True)
class DeadLetter:
    consumer: str
    event: Event
    error: BaseException


@dataclass(slots=True)
class _Subscription:
    consumer: str
    handler: EventHandler
    event_types: frozenset[str] | None
    queue: deque[tuple[Event, int, bool]] = field(
        default_factory=deque
    )  # 事件、已失败次数、是否重复投递
    busy: bool = False  # 有 runner 正在处理它的一个事件（在总线锁下读写）

    def wants(self, event: Event) -> bool:
        return self.event_types is None or event.event_type in self.event_types


class MemoryBus:
    """每个 consumer 各收到一份事件；同一 consumer 按发布顺序处理。"""

    def __init__(self, *, max_attempts: int = 5, duplicate_delivery: bool = False) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        self.max_attempts = max_attempts
        self.duplicate_delivery = duplicate_delivery
        self.dead_letters: list[DeadLetter] = []
        self._subs: dict[str, _Subscription] = {}
        self._lock = threading.RLock()

    def subscribe(
        self,
        consumer: str,
        handler: EventHandler,
        event_types: Sequence[str] | None,
        replay: Sequence[Event] = (),
    ) -> None:
        with self._lock:
            if consumer in self._subs:
                raise ValueError(f"consumer {consumer!r} 已经订阅过")
            sub = _Subscription(
                consumer, handler, None if event_types is None else frozenset(event_types)
            )
            sub.queue.extend((e, 0, False) for e in replay if sub.wants(e))
            self._subs[consumer] = sub

    def publish(self, events: Sequence[Event]) -> None:
        with self._lock:
            for sub in self._subs.values():
                sub.queue.extend((e, 0, False) for e in events if sub.wants(e))

    def pending(self) -> int:
        with self._lock:
            return sum(len(s.queue) for s in self._subs.values())

    def run_until_idle(self, max_deliveries: int = 100_000) -> int:
        """轮流给各 consumer 投递一个事件，直到没有可投递的事件。返回本次调用的投递次数。

        handler 抛出异常时，同一事件稍后重投（保持该 consumer 的顺序）；失败 max_attempts 次
        进入 dead_letters。再投递就会超过 max_deliveries 次时抛出 RuntimeError（多半是传播没有停），
        尚未投递的事件留在队列里。

        同一 consumer 同时只处理一个事件：handler 在总线锁外执行；其他 runner（包括 handler 里
        嵌套的调用）跳过正在处理的 consumer，由持有它的 runner 继续投递，其余 consumer 照常轮转。
        """
        delivered = 0
        while True:
            with self._lock:
                ready = [s for s in self._subs.values() if s.queue and not s.busy]
            if not ready:
                return delivered
            for sub in ready:
                with self._lock:
                    if sub.busy or not sub.queue:
                        continue
                    if delivered >= max_deliveries:
                        raise RuntimeError(
                            f"投递超过 {max_deliveries} 次仍未结束，可能存在无限传播"
                        )
                    event, failures, duplicate = sub.queue.popleft()
                    sub.busy = True
                delivered += 1
                try:
                    self._deliver(sub, event, failures, duplicate)
                finally:
                    with self._lock:
                        sub.busy = False

    def _deliver(self, sub: _Subscription, event: Event, failures: int, duplicate: bool) -> None:
        """调用 handler；重投或重复投递的事件在释放 busy 之前放回队列，保证顺序。"""
        try:
            sub.handler(event)
        except Exception as exc:
            with self._lock:
                if failures + 1 >= self.max_attempts:
                    self.dead_letters.append(DeadLetter(sub.consumer, event, exc))
                else:
                    sub.queue.appendleft((event, failures + 1, duplicate))
            return
        if self.duplicate_delivery and not duplicate:
            with self._lock:
                sub.queue.append((event, 0, True))


# ---------------------------------------------------------------------------
# 存储与客户端


class MemoryStorage:
    """一套内存中的 ①。用 client(模块名) 取得各模块的客户端。"""

    def __init__(
        self,
        registry: Registry = DEFAULT_REGISTRY,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_attempts: int = 5,
        duplicate_delivery: bool = False,
    ) -> None:
        self.registry = registry
        self.bus = MemoryBus(max_attempts=max_attempts, duplicate_delivery=duplicate_delivery)
        self._clock = clock
        self._state = _State()
        self._log: list[Event] = []
        self._lock = threading.RLock()

    def client(self, module: str) -> MemoryClient:
        return MemoryClient(self, module)

    def reader(self) -> Reader:
        """不绑定模块的只读视图（当前状态）。"""
        return _StateReader(self._state, self.registry)

    @property
    def events(self) -> tuple[Event, ...]:
        """已提交的全部事件，按提交顺序（相当于事件日志）。"""
        with self._lock:
            return tuple(self._log)

    @property
    def dead_letters(self) -> list[DeadLetter]:
        return self.bus.dead_letters

    def run_until_idle(self, max_deliveries: int = 100_000) -> int:
        return self.bus.run_until_idle(max_deliveries)

    def _write(self, module: str, ops: Sequence[WriteOp], context: Context) -> WriteResult:
        if not isinstance(context, Context):
            raise InvalidRequest("context 必须是 Context，例如 Context.system()")
        ops = list(ops)
        with self._lock:
            now = self._clock()
            batch_id = new_id()
            txn = _Txn(self._state.copy(), self.registry, module, context, now, batch_id)
            try:
                txn.run(ops)
            except _Abort as abort:
                return WriteResult(batch_id=None, conflict=abort.conflict)
            self._state = txn.s
            self._log.extend(txn.events)
            self.bus.publish(txn.events)
        return WriteResult(batch_id=batch_id, results=tuple(txn.results), events=tuple(txn.events))

    def _subscribe(
        self,
        module: str,
        consumer: str,
        handler: EventHandler,
        event_types: Sequence[str] | None,
        from_beginning: bool,
    ) -> None:
        if consumer != module and not consumer.startswith(module + "."):
            raise PermissionDenied(f"consumer 必须是 {module} 或以 {module}. 开头")
        with self._lock:
            replay = tuple(self._log) if from_beginning else ()
            self.bus.subscribe(consumer, handler, event_types, replay)


class MemoryClient:
    """绑定到一个模块的客户端，实现 StorageClient。"""

    def __init__(self, storage: MemoryStorage, module: str) -> None:
        if not module or not module.replace("_", "a").isalnum() or not module[0].islower():
            raise ValueError(f"模块名必须是 snake_case：{module!r}")
        self._storage = storage
        self._module = module

    @property
    def module(self) -> str:
        return self._module

    @property
    def registry(self) -> Registry:
        return self._storage.registry

    def _reader(self) -> _StateReader:
        return _StateReader(self._storage._state, self._storage.registry)

    # -- 写 --

    def write(self, ops: Sequence[WriteOp], context: Context) -> WriteResult:
        return self._storage._write(self._module, ops, context)

    @contextmanager
    def snapshot(self) -> Iterator[Reader]:
        yield self._reader()

    def subscribe(
        self,
        consumer: str,
        handler: EventHandler,
        *,
        event_types: Sequence[str] | None = None,
        from_beginning: bool = False,
    ) -> None:
        self._storage._subscribe(self._module, consumer, handler, event_types, from_beginning)

    # -- 读 --

    def get(self, id: str) -> Record | None:
        return self._reader().get(id)

    def get_version(self, id: str, version: int) -> Record | None:
        return self._reader().get_version(id, version)

    def history(self, id: str) -> list[SemanticRecord]:
        return self._reader().history(id)

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        return self._reader().get_many(ids)

    def query(self, query: Query) -> Page:
        return self._reader().query(query)

    def count(self, query: Query) -> int:
        return self._reader().count(query)

    def get_system_record(self, id: str) -> SystemRecord | None:
        return self._reader().get_system_record(id)

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        return self._reader().stale_dependencies(
            targets, only_current_dependents=only_current_dependents
        )

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        return self._reader().dependencies_of(dependent_id, version)

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        return self._reader().dependents_of(
            target_id, only_current_dependents=only_current_dependents
        )

    def is_processed(self, consumer: str, event_id: str) -> bool:
        return self._reader().is_processed(consumer, event_id)
