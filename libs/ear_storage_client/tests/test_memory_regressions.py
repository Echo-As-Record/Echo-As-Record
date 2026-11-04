"""P0a 回归测试：内存实现中违反既有规则的四处边界。

1. 投递次数达到上限时不丢事件；
2. 同一 consumer 同时只处理一个事件（多个 runner、handler 内嵌套调用），重投保持顺序；
3. 空区间 [t, t) 不命中任何数据；
4. 系统记录更新与新建一样经模型校验（UTC），非法时间整批回滚。

总线会吞掉 handler 里的 Exception（计为失败并重投），所以 handler 内只记录，断言都在主线程做；
线程用 Event 同步、join 带超时，线程里的异常回传主线程。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from ear_contracts import Event, new_id
from ear_storage_client import (
    AppendRaw,
    Context,
    InvalidRequest,
    MemoryStorage,
    PutSystemRecord,
    Query,
    StorageClient,
    TimeRange,
)
from ear_storage_client.testing import make_raw, ts

SYS = Context.system()
TIMEOUT = 5.0


def _voiceprints(storage: MemoryStorage, n: int = 2) -> list[str]:
    """写入 n 条声纹，返回对应 data_changed 的 event_id（按发布顺序）。"""
    adapter = storage.client("adapter")
    for i in range(n):
        rec = make_raw("voiceprint", {"local_speaker": "C"}, start=f"2026-10-29T06:0{i}:00Z")
        adapter.write([AppendRaw(rec)], SYS).check()
    return [e.event_id for e in storage.events]


class _Runner:
    """在线程里调用 target；join 带超时，并把线程里的异常交回主线程。"""

    def __init__(self, target: Callable[[], int]) -> None:
        self.result: int | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()

    def _run(self, target: Callable[[], int]) -> None:
        try:
            self.result = target()
        except BaseException as exc:
            self.error = exc

    def join(self) -> None:
        self._thread.join(TIMEOUT)
        assert not self._thread.is_alive(), "线程超时未结束（可能死锁或持锁调用 handler）"
        if self.error is not None:
            raise self.error


class _Probe:
    """记录 handler 的进入顺序和同时在处理的最大数量。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.log: list[str] = []
        self.errors: list[str] = []

    @contextmanager
    def enter(self, event_id: str) -> Iterator[None]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.log.append(event_id)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


# 1. 投递上限 --------------------------------------------------------------------------


def test_delivery_limit_keeps_undelivered_events() -> None:
    storage = MemoryStorage()
    seen: list[str] = []

    def on_event(event: Event) -> None:
        seen.append(event.event_id)

    storage.client("api").subscribe("api", on_event)
    ids = _voiceprints(storage)

    with pytest.raises(RuntimeError):
        storage.run_until_idle(max_deliveries=0)
    assert (seen, storage.bus.pending()) == ([], 2)

    with pytest.raises(RuntimeError):
        storage.run_until_idle(max_deliveries=1)
    assert (seen, storage.bus.pending()) == (ids[:1], 1)

    # 恰好用完上限时正常返回；之前没投出去的事件按原顺序补投
    assert storage.run_until_idle(max_deliveries=1) == 1
    assert (seen, storage.bus.pending()) == (ids, 0)


# 2. 同一 consumer 串行 ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["ok", "retry", "dead_letter"])
def test_concurrent_runners_serialize_consumer(mode: str) -> None:
    """runner A 处理 X 的第一个事件时，runner B 不得并发处理 X，但照常投递独立的 Y。"""
    storage = MemoryStorage(max_attempts=2)
    probe = _Probe()
    entered, release = threading.Event(), threading.Event()
    first: list[str] = []
    y_seen: list[str] = []

    def on_x(event: Event) -> None:
        with probe.enter(event.event_id):
            if not first:
                first.append(event.event_id)
                entered.set()
                if not release.wait(TIMEOUT):
                    probe.errors.append("release 超时")
                if mode != "ok":
                    raise RuntimeError("planned failure")
            elif mode == "dead_letter" and event.event_id == first[0]:
                raise RuntimeError("planned failure")

    def on_y(event: Event) -> None:
        y_seen.append(event.event_id)

    storage.client("attribution").subscribe("attribution", on_x)
    storage.client("extraction").subscribe("extraction", on_y)
    e1, e2 = _voiceprints(storage)

    a = _Runner(storage.run_until_idle)
    try:
        assert entered.wait(TIMEOUT)
        b = _Runner(storage.run_until_idle)
        b.join()  # B 能结束，说明 A 的 handler 没有持有总线锁
        assert b.result == 2  # 只投递了 Y 的两个事件
        assert y_seen == [e1, e2]
    finally:
        release.set()
        a.join()

    expected = {"ok": [e1, e2], "retry": [e1, e1, e2], "dead_letter": [e1, e1, e2]}[mode]
    assert probe.log == expected
    assert probe.max_active == 1
    assert probe.errors == []
    dead = [(d.consumer, d.event.event_id) for d in storage.dead_letters]
    assert dead == ([("attribution", e1)] if mode == "dead_letter" else [])
    assert storage.bus.pending() == 0


def test_nested_runner_skips_busy_consumer() -> None:
    """handler 内嵌套调用 run_until_idle：不重入自己的 consumer，不死锁，独立 consumer 照常投递。"""
    storage = MemoryStorage()
    probe = _Probe()
    nested: list[int] = []
    y_seen: list[str] = []

    def on_x(event: Event) -> None:
        with probe.enter(event.event_id):
            if not nested:
                nested.append(-1)
                nested[0] = storage.run_until_idle()

    def on_y(event: Event) -> None:
        y_seen.append(event.event_id)

    storage.client("attribution").subscribe("attribution", on_x)
    storage.client("extraction").subscribe("extraction", on_y)
    e1, e2 = _voiceprints(storage)

    outer = _Runner(storage.run_until_idle)
    outer.join()
    assert nested == [2]  # 嵌套调用只投递了 Y 的事件
    assert outer.result == 2  # 外层投递 X 的两个事件
    assert (probe.log, probe.max_active) == ([e1, e2], 1)
    assert y_seen == [e1, e2]
    assert storage.bus.pending() == 0


class _Stop(BaseException):
    pass


def test_base_exception_releases_consumer() -> None:
    """handler 以非 Exception 的 BaseException 退出后，consumer 不被永久占用。"""
    storage = MemoryStorage()
    seen: list[str] = []

    def on_event(event: Event) -> None:
        seen.append(event.event_id)
        if len(seen) == 1:
            raise _Stop

    storage.client("api").subscribe("api", on_event)
    e1, e2 = _voiceprints(storage)
    with pytest.raises(_Stop):
        storage.run_until_idle()
    assert storage.run_until_idle() == 1
    assert seen == [e1, e2]


# 3. 空时间区间 ------------------------------------------------------------------------


def test_empty_time_range_matches_nothing() -> None:
    t = ts("2026-10-29T06:00:00Z")
    minute = timedelta(minutes=1)
    empty = TimeRange(t, t)  # 允许构造，只是什么都不命中
    assert not empty.overlaps(t, t)
    assert not empty.overlaps(t - minute, t + minute)
    assert not empty.overlaps(t, t + 5 * minute)

    adapter = MemoryStorage().client("adapter")
    spans = [(t, t), (t - minute, t + minute), (t, t + 5 * minute), (t - 5 * minute, t)]
    for start, end in spans:
        adapter.write([AppendRaw(make_raw("gps_point", start=start, end=end))], SYS).check()

    assert adapter.count(Query("gps_point", time=empty)) == 0
    assert adapter.query(Query("gps_point", time=empty)).items == ()
    # 对照：非空区间照常命中包含 t 的三条（[t-5m, t) 不含 t）
    assert adapter.count(Query("gps_point", time=TimeRange(t, t + timedelta(seconds=1)))) == 3


# 4. 系统记录更新的时间校验 ------------------------------------------------------------

T0, T1 = ts("2026-10-30T01:00:00Z"), ts("2026-10-30T02:00:00Z")
START, END = ts("2026-10-28T13:00:00Z"), ts("2026-10-29T13:00:00Z")
CST = timezone(timedelta(hours=8))
ZERO = timezone(timedelta(0), "Z0")  # 零偏移但不是 UTC 对象，存储时应规范化为 UTC


def _clocked() -> tuple[MemoryStorage, StorageClient, list[datetime], str]:
    now = [T0]
    storage = MemoryStorage(clock=lambda: now[0])
    extraction = storage.client("extraction")
    rid = new_id()
    op = PutSystemRecord("extraction_run", rid, {"status": "running"}, None, START, END)
    extraction.write([op], SYS).check()
    now[0] = T1
    return storage, extraction, now, rid


@pytest.mark.parametrize(
    ("start", "end", "payload"),
    [
        pytest.param(
            START.replace(tzinfo=None), END.replace(tzinfo=None), {"status": "done"}, id="naive"
        ),
        pytest.param(
            START.replace(tzinfo=CST), END.replace(tzinfo=CST), {"status": "done"}, id="cst"
        ),
        # 与现有时间是同一时刻、payload 也相同：不能借“无变化”短路跳过校验
        pytest.param(
            START.astimezone(CST), END.astimezone(CST), {"status": "running"}, id="cst-same-instant"
        ),
    ],
)
def test_put_system_update_rejects_non_utc(
    start: datetime, end: datetime, payload: dict[str, str]
) -> None:
    storage, extraction, _, rid = _clocked()
    before = extraction.get_system_record(rid)
    events = len(storage.events)
    other = new_id()
    batch = [
        PutSystemRecord("extraction_run", other, {"status": "queued"}),
        PutSystemRecord("extraction_run", rid, payload, 1, start, end),
    ]
    with pytest.raises(InvalidRequest):
        extraction.write(batch, SYS)
    after = extraction.get_system_record(rid)
    assert after == before and after is not None and after.revision == 1
    assert extraction.get_system_record(other) is None  # 同批前面的操作一并回滚
    assert len(storage.events) == events


def test_put_system_update_with_invalid_clock_rolls_back() -> None:
    _, extraction, now, rid = _clocked()
    before = extraction.get_system_record(rid)
    now[0] = T1.replace(tzinfo=None)
    with pytest.raises(InvalidRequest):
        extraction.write([PutSystemRecord("extraction_run", rid, {"status": "done"}, 1)], SYS)
    assert extraction.get_system_record(rid) == before


def test_put_system_update_keeps_meaning_and_normalizes() -> None:
    _, extraction, now, rid = _clocked()
    now[0] = T1.replace(tzinfo=ZERO)
    new_start = ts("2026-10-29T00:00:00Z").replace(tzinfo=ZERO)
    new_end = ts("2026-10-30T00:00:00Z").replace(tzinfo=ZERO)
    done = {"status": "succeeded"}

    result = extraction.write(
        [PutSystemRecord("extraction_run", rid, done, 1, new_start, new_end)], SYS
    ).check()
    assert (result[0].outcome, result[0].version, result.events) == ("written", 2, ())
    record = extraction.get_system_record(rid)
    assert record is not None
    assert (record.revision, record.created_at, record.updated_at) == (2, T0, T1)
    assert (record.event_start, record.event_end, record.payload) == (new_start, new_end, done)
    stamps = (record.created_at, record.updated_at, record.event_start, record.event_end)
    assert all(s is not None and s.tzinfo is UTC for s in stamps)

    # 无变化：省略时间或给出同样的 UTC 时间都不加 revision、不动 updated_at、不发事件
    now[0] = ts("2026-10-30T03:00:00Z")
    for op in (
        PutSystemRecord("extraction_run", rid, done, 2),
        PutSystemRecord("extraction_run", rid, done, 2, new_start.astimezone(UTC), new_end),
    ):
        same = extraction.write([op], SYS).check()
        assert (same[0].outcome, same[0].version, same.events) == ("unchanged", 2, ())
    assert extraction.get_system_record(rid) == record
