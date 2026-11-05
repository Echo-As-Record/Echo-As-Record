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

import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from ear_contracts import (
    DEFAULT_REGISTRY,
    DataTypeSpec,
    DependencyRecord,
    Event,
    Record,
    Registry,
    SemanticRecord,
    SystemRecord,
    UnknownDataTypeError,
    new_id,
    utcnow,
)

from ear_storage_client.context import Context
from ear_storage_client.engine import (
    Abort as _Abort,
)
from ear_storage_client.engine import (
    State as _State,
)
from ear_storage_client.engine import (
    Transaction as _Txn,
)
from ear_storage_client.engine import (
    _copy,
    canonical_json,
)
from ear_storage_client.engine import (
    stale as _stale,
)
from ear_storage_client.errors import InvalidRequest, PermissionDenied
from ear_storage_client.ops import (
    WriteOp,
)
from ear_storage_client.protocol import EventHandler, Reader
from ear_storage_client.queries import DependencySet, Page, Query, QueryItem, StaleDependency
from ear_storage_client.results import WriteResult

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

    def type_revision(self, data_type: str) -> int:
        self._spec(data_type)
        return self._s.type_revisions.get(data_type, 0)

    def get(self, id: str) -> Record | None:
        record = self._s.current(id)
        return None if record is None else _copy(record)

    def get_version(self, id: str, version: int) -> Record | None:
        record = self._s.version(id, version)
        return None if record is None else _copy(record)

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
                group = self._s.deps.get(key)
                if group is None:
                    continue
                for dep in group.dependencies:
                    if dep.depends_on.id == target_id and _stale(
                        self._s, dep.depends_on, dep.fields
                    ):
                        stale.append(
                            StaleDependency(
                                dependent=_copy(dep.dependent),
                                depends_on=_copy(dep.depends_on),
                                current_version=target.version,
                                target_superseded=superseded,
                                fields=dep.fields,
                            )
                        )
        return stale

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        if version is None:
            versions = self._s.versions.get(dependent_id)
            if not versions:
                return None
            version = versions[-1].version
        group = self._s.deps.get((dependent_id, version))
        if group is None:
            return None
        return DependencySet(
            _copy(group.dependent),
            tuple(_copy(d) for d in group.dependencies),
            group.registered_by,
            group.registered_at,
        )

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        group = self.dependency_set(dependent_id, version)
        return [] if group is None else list(group.dependencies)

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        found: list[DependencyRecord] = []
        for key in sorted(self._s.deps_by_target.get(target_id, frozenset())):
            if only_current_dependents and not self._current_dependent(*key):
                continue
            group = self._s.deps.get(key)
            if group is not None:
                found.extend(_copy(d) for d in group.dependencies if d.depends_on.id == target_id)
        return found

    def is_processed(self, consumer: str, event_id: str) -> bool:
        return (consumer, event_id) in self._s.processed


# ---------------------------------------------------------------------------
# 写入


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

    def type_revision(self, data_type: str) -> int:
        return self._reader().type_revision(data_type)

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

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        return self._reader().dependency_set(dependent_id, version)

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
