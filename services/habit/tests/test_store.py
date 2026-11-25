"""桶存储：唯一约束、更新、汇总，用内存版 ① 和合成数据。"""

from __future__ import annotations

from ear_contracts import DEFAULT_REGISTRY, SemanticRecord, new_id
from ear_habit.buckets import Utterance, stats_of
from ear_habit.store import (
    MODULE,
    extend_registry,
    find_bucket,
    rollup_ops,
    upsert_bucket_ops,
)
from ear_storage_client import Context, MemoryStorage, Reader

PERSON = new_id()
SYS = Context.system()


def make():
    storage = MemoryStorage(extend_registry(DEFAULT_REGISTRY))
    return storage, storage.client(MODULE)


def put_day(client, period: str, utterances: list[Utterance]):
    with client.snapshot() as reader:
        ops = upsert_bucket_ops(reader, PERSON, "day", period, stats_of(utterances))
    return client.write(ops, SYS).check()


def rollup(client, scale: str, period: str):
    with client.snapshot() as reader:
        ops = rollup_ops(reader, PERSON, scale, period)  # type: ignore[arg-type]
    return client.write(ops, SYS).check() if ops else None


def bucket(reader: Reader, scale: str, period: str) -> SemanticRecord:
    found = find_bucket(reader, f"{PERSON}:{scale}:{period}")
    assert found is not None
    return found


def test_create_then_update_keeps_one_bucket() -> None:
    _, client = make()
    put_day(client, "2026-10-26", [Utterance("就是 就是", 1000)])
    first = bucket(client, "day", "2026-10-26")
    put_day(client, "2026-10-26", [Utterance("就是 就是", 1000), Utterance("然后", 500)])
    second = bucket(client, "day", "2026-10-26")
    assert second.id == first.id and second.version == first.version + 1
    assert second.payload["stats"]["fillers"] == {"就是": 2, "然后": 1}


def test_same_content_is_unchanged() -> None:
    _, client = make()
    put_day(client, "2026-10-26", [Utterance("hello", 100)])
    result = put_day(client, "2026-10-26", [Utterance("hello", 100)])
    assert result.results[0].outcome == "unchanged"


def test_week_and_month_rollup_match_bulk() -> None:
    _, client = make()
    mon, tue = [Utterance("我觉得 hello", 1000)], [Utterance("然后 hello", 2000)]
    put_day(client, "2026-10-26", mon)
    put_day(client, "2026-10-27", tue)
    rollup(client, "week", "2026-10-26")
    rollup(client, "month", "2026-10")
    expected = stats_of(mon + tue)
    assert bucket(client, "week", "2026-10-26").payload["stats"] == expected
    assert bucket(client, "month", "2026-10").payload["stats"] == expected


def test_rollup_follows_a_corrected_day() -> None:
    _, client = make()
    put_day(client, "2026-10-26", [Utterance("hello", 100)])
    rollup(client, "week", "2026-10-26")
    put_day(client, "2026-10-26", [])  # 这段话被纠正走了
    rollup(client, "week", "2026-10-26")
    assert bucket(client, "week", "2026-10-26").payload["stats"] == stats_of([])


def test_empty_week_creates_no_bucket() -> None:
    _, client = make()
    assert rollup(client, "week", "2026-10-26") is None
    assert find_bucket(client, f"{PERSON}:week:2026-10-26") is None


def test_other_week_days_are_not_included() -> None:
    _, client = make()
    put_day(client, "2026-10-26", [Utterance("a", 1)])
    put_day(client, "2026-11-02", [Utterance("b", 1)])  # 下一周的周一
    rollup(client, "week", "2026-10-26")
    assert bucket(client, "week", "2026-10-26").payload["stats"] == stats_of([Utterance("a", 1)])
