"""Shared pure batch validator; no database, bus, or MemoryStorage instance.

State may be a bounded projection: current semantic graph plus explicitly requested
historical versions. Callers must fail closed before omitting any graph node/edge.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

from ear_contracts import (
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
)
from pydantic import BaseModel, ValidationError

from ear_storage_client.context import Context
from ear_storage_client.errors import InvalidRequest, PermissionDenied
from ear_storage_client.ops import (
    AppendRaw,
    Create,
    DependencyInput,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)
from ear_storage_client.queries import DependencySet
from ear_storage_client.results import Conflict, ConflictReason, OpResult


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
class State:
    raw: dict[str, RawRecord] = field(default_factory=dict)
    raw_keys: dict[tuple[str, str, str], str] = field(default_factory=dict)
    versions: dict[str, tuple[SemanticRecord, ...]] = field(default_factory=dict)
    semantic_keys: dict[tuple[str, str], str] = field(default_factory=dict)
    system: dict[str, SystemRecord] = field(default_factory=dict)
    deps: dict[tuple[str, int], DependencySet] = field(default_factory=dict)
    deps_by_target: dict[str, frozenset[tuple[str, int]]] = field(default_factory=dict)
    processed: dict[tuple[str, str], None] = field(default_factory=dict)
    published: dict[str, None] = field(default_factory=dict)

    prior: dict[tuple[str, int], Record] = field(default_factory=dict)
    occupied: set[str] = field(default_factory=set)

    def copy(self) -> State:
        return State(
            dict(self.raw),
            dict(self.raw_keys),
            dict(self.versions),
            dict(self.semantic_keys),
            dict(self.system),
            dict(self.deps),
            dict(self.deps_by_target),
            dict(self.processed),
            dict(self.published),
            dict(self.prior),
            set(self.occupied),
        )

    def current(self, id: str) -> Record | None:
        if id in self.raw:
            return self.raw[id]
        versions = self.versions.get(id)
        return versions[-1] if versions else None

    def version(self, id: str, version: int) -> Record | None:
        if id in self.raw:
            return self.raw[id] if version == 1 else None
        prior = self.prior.get((id, version))
        if prior is not None:
            return prior
        versions = self.versions.get(id, ())
        if 1 <= version <= len(versions) and versions[version - 1].version == version:
            return versions[version - 1]
        return next((v for v in reversed(versions) if v.version == version), None)

    def id_in_use(self, id: str) -> bool:
        return id in self.occupied or id in self.raw or id in self.versions or id in self.system


def stale(state: State, ref: VersionRef, fields: tuple[str, ...] | None) -> bool:
    """登记、批末校验和读取共享的失效规则；缺失通过键是否存在与 null 区分。"""
    current = state.current(ref.id)
    if current is None or (isinstance(current, SemanticRecord) and current.superseded):
        return True
    recorded = state.version(ref.id, ref.version)
    if recorded is None or recorded.data_type != ref.data_type:
        return True
    if fields is None:
        return ref.version != current.version
    return any(
        (key in recorded.payload) != (key in current.payload)
        or canonical_json(recorded.payload.get(key)) != canonical_json(current.payload.get(key))
        for key in fields
    )


class Abort(Exception):
    def __init__(self, conflict: Conflict) -> None:
        super().__init__(conflict.message)
        self.conflict = conflict


class Transaction:
    """在状态副本上执行一批操作。

    遇到冲突抛 Abort；请求错误抛 InvalidRequest / PermissionDenied。
    """

    def __init__(
        self,
        state: State,
        registry: Registry,
        module: str,
        context: Context,
        now: datetime,
        batch_id: str,
        *,
        authorize_inputs: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        self.s = state
        self.registry = registry
        self.module = module
        self.context = context
        self.now = now
        self.batch_id = batch_id
        self.authorize_inputs = authorize_inputs
        self.used_input_types: frozenset[str] = frozenset()
        self.system_writes: list[SystemRecord] = []
        self.results: list[OpResult] = []
        self.events: list[Event] = []
        self.touched: dict[str, int] = {}  # 本批写过的有版本对象 → 最后一条相关操作的序号
        # 与 touched 分开：无变化 Update 仍是重算结果，不能绕过依赖检查。
        self.recomputed: dict[tuple[str, int], int] = {}
        self.registered: dict[tuple[str, int], int] = {}
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
        raise Abort(Conflict(self.index, reason, message, data_type, id, details))

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
            if isinstance(op, PutSystemRecord) and self.results[-1].outcome == "written":
                self.system_writes.append(self.s.system[op.id])
            if isinstance(op, Create | Update):
                version = self.results[-1].version
                assert version is not None
                self.recomputed[(op.id, version)] = self.index
        self.check_constraints()
        self.check_dependencies()

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
        # 先合并/拒绝重复输入，再做新鲜度检查，不能用后一条静默覆盖前一条。
        refs: dict[str, DependencyInput] = {}
        for item in op.inputs():
            previous = refs.get(item.id)
            if previous is not None:
                if (previous.data_type, previous.version) != (item.data_type, item.version):
                    self.invalid(f"同一对象 {item.id} 的重复依赖类型或版本不同")
                fields = (
                    None
                    if previous.fields is None or item.fields is None
                    else tuple(sorted(set(previous.fields) | set(item.fields)))
                )
            else:
                fields = item.fields
            clean = VersionRef(data_type=item.data_type, id=item.id, version=item.version)
            refs[item.id] = DependencyInput(clean, fields)

        for item in refs.values():
            target = self.s.current(item.id)
            if target is None:
                self.conflict(
                    "not_found",
                    f"被依赖的 {item.data_type} {item.id} 不存在",
                    data_type=item.data_type,
                    id=item.id,
                )
            if target.data_type != item.data_type:
                self.invalid(f"{item.id} 的类型是 {target.data_type}，不是 {item.data_type}")
            if self.registry.is_core(op.dependent_type) and not self.registry.is_core(
                item.data_type
            ):
                self.invalid(f"核心类型 {op.dependent_type} 不能依赖插件类型 {item.data_type}")
            self.require_fresh(item.ref, item.fields)

        key = (op.dependent_id, version)
        self.registered[key] = self.index  # unchanged/空集也算本批显式完整登记。
        old = self.s.deps.get(key)
        old_set = (
            set()
            if old is None
            else {
                (d.depends_on.data_type, d.depends_on.id, d.depends_on.version, d.fields)
                for d in old.dependencies
            }
        )
        new_set = {(r.data_type, r.id, r.version, r.fields) for r in refs.values()}
        if old is not None and old_set == new_set:
            self.result("unchanged", op.dependent_type, op.dependent_id, version)
            return
        dependent = VersionRef(data_type=op.dependent_type, id=op.dependent_id, version=version)
        records = tuple(
            DependencyRecord(
                dependent=dependent,
                depends_on=item.ref,
                registered_by=self.module,
                registered_at=self.now,
                **({} if item.fields is None else {"fields": item.fields}),
            )
            for item in sorted(refs.values(), key=lambda r: r.id)
        )
        self.s.deps[key] = DependencySet(dependent, records, self.module, self.now)
        old_targets = set() if old is None else {d.depends_on.id for d in old.dependencies}
        for target_id in old_targets - refs.keys():
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

    def require_fresh(self, ref: VersionRef, fields: tuple[str, ...] | None) -> None:
        if not stale(self.s, ref, fields):
            return
        target = self.s.current(ref.id)
        superseded = isinstance(target, SemanticRecord) and target.superseded
        self.conflict(
            "stale_dependency",
            f"{ref.data_type} {ref.id} 的登记版本 {ref.version} 已过时",
            data_type=ref.data_type,
            id=ref.id,
            registered_version=ref.version,
            current_version=None if target is None else target.version,
            target_superseded=superseded,
            fields=fields,
        )

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

    def check_dependencies(self) -> None:
        for key, index in self.recomputed.items():
            record = self.s.version(*key)
            assert record is not None
            if (
                self.registry.get(record.data_type).requires_dependencies
                and key not in self.registered
            ):
                self.index = index
                self.invalid(f"{record.data_type} {record.id} 版本 {key[1]} 缺少同批完整依赖登记")

        # 只检查本批登记/重算且最终仍 current active 的结果。其他已有 stale 边是正常状态。
        freshness_groups: list[tuple[int, DependencySet]] = []
        for key, index in (self.recomputed | self.registered).items():
            current = self.s.current(key[0])
            if (
                not isinstance(current, SemanticRecord)
                or current.superseded
                or current.version != key[1]
            ):
                continue
            group = self.s.deps.get(key)
            if group is not None:
                freshness_groups.append((index, group))
        # Authorize ALL business freshness inputs before the first stale result.
        # This includes reused old sets, but excludes replaced/terminal versions
        # and unrelated edges loaded solely for the internal whole-graph DAG check.
        inputs = frozenset(
            dep.depends_on.data_type for _, group in freshness_groups for dep in group.dependencies
        )
        if self.authorize_inputs is not None:
            self.authorize_inputs(inputs)
        self.used_input_types |= inputs
        for index, group in freshness_groups:
            self.index = index
            for dep in group.dependencies:
                self.require_fresh(dep.depends_on, dep.fields)
        self.check_dependency_cycles()

    def check_dependency_cycles(self) -> None:
        # MemoryStorage 从空图开始，每次提交都维护 DAG 不变量；无需公开旧状态迁移接口。
        # 历史登记保持可读，但只有当前有效依赖方的出边参与当前对象图。
        active = {id: vs[-1] for id, vs in self.s.versions.items() if not vs[-1].superseded}
        graph: dict[str, tuple[str, ...]] = {}
        for id, record in active.items():
            group = self.s.deps.get((id, record.version))
            graph[id] = (
                ()
                if group is None
                else tuple(
                    dep.depends_on.id for dep in group.dependencies if dep.depends_on.id in active
                )
            )
        done: set[str] = set()
        for start in graph:
            if start in done:
                continue
            path = [start]
            positions = {start: 0}
            stack = [iter(graph[start])]
            while stack:
                target = next(stack[-1], None)
                if target is None:
                    node = path.pop()
                    done.add(node)
                    del positions[node]
                    stack.pop()
                elif target in positions:
                    cycle = [*path[positions[target] :], target]
                    self.index = max(
                        (
                            self.registered[(id, active[id].version)]
                            for id in cycle
                            if (id, active[id].version) in self.registered
                        ),
                        default=self.index,
                    )
                    self.conflict(
                        "dependency_cycle",
                        "当前有效对象的最终依赖图成环",
                        data_type=active[target].data_type,
                        id=target,
                        path=[
                            {
                                "data_type": active[id].data_type,
                                "id": id,
                                "version": active[id].version,
                            }
                            for id in cycle
                        ],
                    )
                elif target not in done:
                    positions[target] = len(path)
                    path.append(target)
                    stack.append(iter(graph[target]))

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
