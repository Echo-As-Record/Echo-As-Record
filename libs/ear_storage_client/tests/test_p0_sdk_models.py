"""P0b 首批 SDK 模型：DependencyInput、RegisterDependencies 输入、StaleDependency、
DependencySet、dependency_cycle。
见 docs/proposals/storage-p0-contracts.md 第 2.2、2.7、2.8、2.10 节。

模型兼容及最小内存集成；详细字段/批末行为见 test_p0_memory_dependencies.py。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from ear_contracts import DependencyRecord, VersionRef, new_id
from ear_storage_client import (
    RETRYABLE,
    Conflict,
    Context,
    Create,
    DependencyInput,
    DependencySet,
    InvalidRequest,
    MemoryStorage,
    RegisterDependencies,
    StaleDependency,
)

SYS = Context.system()


def ref(data_type: str = "person", version: int = 1, **extra: Any) -> VersionRef:
    return VersionRef(data_type=data_type, id=new_id(), version=version, **extra)


# DependencyInput -------------------------------------------------------------------


def test_dependency_input_whole_and_aliases() -> None:
    r = ref()
    whole = DependencyInput(r)
    assert whole.fields is None and whole.whole_object
    assert (whole.data_type, whole.id, whole.version) == (r.data_type, r.id, r.version)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (("references",), ("references",)),
        (["name", "references", "name"], ("name", "references")),
        ({"b", "a"}, ("a", "b")),
        (("", "Name", "name"), ("", "Name", "name")),
    ],
)
def test_dependency_input_normalises_fields(fields: Any, expected: tuple[str, ...]) -> None:
    assert DependencyInput(ref(), fields).fields == expected


@pytest.mark.parametrize(
    "fields",
    [(), [], "name", "", b"name", ["name", 1], [b"name"], {"name": True}, (f for f in ["a"])],
)
def test_dependency_input_rejects_bad_fields(fields: Any) -> None:
    with pytest.raises(InvalidRequest):
        DependencyInput(ref(), fields)


def test_dependency_input_rejects_bad_ref() -> None:
    with pytest.raises(InvalidRequest):
        DependencyInput({"data_type": "person", "id": new_id(), "version": 1})  # type: ignore[arg-type]
    with pytest.raises(InvalidRequest, match="DependencyInput"):
        DependencyInput(ref(fields=["name"]), ("name",))


# RegisterDependencies -----------------------------------------------------------------


def test_register_accepts_mixed_inputs_in_order() -> None:
    a, b = ref(), ref("attribution")
    op = RegisterDependencies("artifact", new_id(), [a, DependencyInput(b, ("target_id",))])
    assert op.depends_on == (a, DependencyInput(b, ("target_id",)))
    assert op.inputs() == (DependencyInput(a), DependencyInput(b, ("target_id",)))
    # 旧调用：运行时仍接受任意非字符串可迭代对象（类型标注只承诺 Sequence）、空集
    gen = (r for r in [a])
    assert RegisterDependencies("artifact", new_id(), gen).depends_on == (a,)  # type: ignore[arg-type]
    assert RegisterDependencies("artifact", new_id(), []).inputs() == ()


def test_register_does_not_merge_duplicates() -> None:
    a = ref()
    op = RegisterDependencies("artifact", new_id(), [a, DependencyInput(a, ("name",))])
    assert len(op.inputs()) == 2  # 合并或拒绝由 ① 在写入时执行


@pytest.mark.parametrize(
    "depends_on",
    [
        "0199f0a1-1111-7aaa-9bbb-000000000001",
        ref(),
        DependencyInput(ref()),
        {"x": ref()},
        ["person"],
        [None],
    ],
)
def test_register_rejects_non_sequences_and_bad_items(depends_on: Any) -> None:
    with pytest.raises(InvalidRequest):
        RegisterDependencies("artifact", new_id(), depends_on)


def test_version_ref_with_fields_is_rejected_on_write_path() -> None:
    with pytest.raises(InvalidRequest, match="DependencyInput"):
        RegisterDependencies("artifact", new_id(), [ref(fields=["name"])])
    with pytest.raises(ValueError):  # 旧调用方按 ValueError 捕获仍然有效
        RegisterDependencies("artifact", new_id(), [ref(fields=None)])
    # 读取模型不因此拒绝未知字段
    assert ref(fields=["name"]).model_extra == {"fields": ["name"]}
    assert RegisterDependencies("artifact", new_id(), [ref(other=1)]).inputs()


# 读取结果与冲突原因 ----------------------------------------------------------------------


def test_stale_dependency_keeps_old_signature() -> None:
    dep, on = ref("artifact"), ref()
    old = StaleDependency(dep, on, 2, False)
    assert old.fields is None and old.whole_object
    assert (old.dependent, old.depends_on, old.current_version, old.target_superseded) == (
        dep,
        on,
        2,
        False,
    )
    new = StaleDependency(dep, on, 3, True, ("name",))
    assert new.fields == ("name",) and not new.whole_object


def test_dependency_set_distinguishes_empty() -> None:
    now = datetime(2026, 10, 29, 13, tzinfo=UTC)
    dep = ref("artifact")
    empty = DependencySet(dep, (), "extraction", now)
    assert empty.empty
    record = DependencyRecord(
        dependent=dep, depends_on=ref(), registered_by="extraction", registered_at=now
    )
    assert not DependencySet(dep, (record,), "extraction", now).empty


def test_dependency_cycle_is_not_retryable() -> None:
    conflict = Conflict(0, "dependency_cycle", "成环", details={"path": []})
    assert not conflict.retryable
    assert "dependency_cycle" not in RETRYABLE
    assert "stale_dependency" in RETRYABLE


# 当前内存实现（旧调用方）------------------------------------------------------------------


def _artifact_with(op_factory: Any) -> tuple[MemoryStorage, str]:
    storage = MemoryStorage()
    extraction = storage.client("extraction")
    act, art = new_id(), new_id()
    activity = VersionRef(data_type="activity", id=act, version=1)
    extraction.write(
        [
            Create("activity", act, {"title": "组会"}),
            Create("artifact", art, {"body": "x"}),
            RegisterDependencies("artifact", art, [op_factory(activity)]),
        ],
        SYS,
    ).check()
    return storage, art


@pytest.mark.parametrize("factory", [lambda r: r, DependencyInput], ids=["VersionRef", "whole"])
def test_memory_accepts_whole_object_inputs(factory: Any) -> None:
    storage, art = _artifact_with(factory)
    (record,) = storage.client("extraction").dependencies_of(art)
    assert record.whole_object
    assert "fields" not in record.model_dump()


def test_memory_keeps_fields() -> None:
    storage, art = _artifact_with(lambda r: DependencyInput(r, ("title",)))
    (record,) = storage.client("extraction").dependencies_of(art)
    assert record.fields == ("title",)
