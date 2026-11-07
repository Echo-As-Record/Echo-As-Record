"""P0b：字段依赖、同批完整登记及最终当前对象 DAG。只验证进程内事务。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, VersionRef, new_id
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    MemoryStorage,
    PutSystemRecord,
    RegisterDependencies,
    StorageClient,
    Supersede,
    Update,
    WriteOp,
    WriteResult,
    write_with_retry,
)
from ear_storage_client.testing import make_raw, ts

SYS = Context.system()


def ref(id: str, version: int = 1, data_type: str = "activity", **extra: Any) -> VersionRef:
    return VersionRef(data_type=data_type, id=id, version=version, **extra)


def seeded(*, required: bool = False) -> tuple[MemoryStorage, StorageClient, str, str]:
    registry = DEFAULT_REGISTRY
    if required:
        registry = registry.extend_core(
            DataTypeSpec("derived", "semantic", "extraction", requires_dependencies=True)
        )
    storage = MemoryStorage(registry)
    client = storage.client("extraction")
    up, down = new_id(), new_id()
    client.write(
        [
            Create("activity", up, {"name": "A", "references": ["v"]}),
            Create("activity", down, {"body": "x"}),
        ],
        SYS,
    ).check()
    return storage, client, up, down


def register(
    client: StorageClient,
    down: str,
    up: str,
    fields: tuple[str, ...] | None = None,
    *,
    version: int = 1,
) -> WriteResult:
    return client.write(
        [RegisterDependencies("activity", down, [DependencyInput(ref(up, version), fields)])], SYS
    )


# JSON 值真值表：同一组向量同时覆盖登记旧版本和 stale 查询。
@pytest.mark.parametrize(
    ("before", "after", "equal"),
    [
        ({}, {}, True),
        ({}, {"": None}, False),
        ({"": None}, {}, False),
        ({"": None}, {"": None}, True),
        ({"": 1}, {"": 1.0}, False),
        ({"": True}, {"": 1}, False),
        ({"": {"a": 1, "b": [2]}}, {"": {"b": [2], "a": 1}}, True),
        ({"": [1, 2]}, {"": [2, 1]}, False),
        ({"": {"n": 1}}, {"": {"n": 1.0}}, False),
        ({"": "中文"}, {"": "中文"}, True),
        ({"": "é"}, {"": "e\u0301"}, False),
    ],
)
def test_field_value_truth_table(
    before: dict[str, Any], after: dict[str, Any], equal: bool
) -> None:
    storage = MemoryStorage()
    client = storage.client("extraction")
    up, down, other = new_id(), new_id(), new_id()
    client.write(
        [
            Create("activity", up, {**before, "other": 0}),
            Create("activity", down, {}),
            Create("activity", other, {}),
        ],
        SYS,
    ).check()
    register(client, down, up, ("",)).check()
    client.write([Update("activity", up, 1, {**after, "other": 1})], SYS).check()
    stale = client.stale_dependencies([up, up])
    assert bool(stale) is not equal
    if stale:
        assert stale[0].fields == ("",) and stale[0].current_version == 2
    result = register(client, other, up, ("",))
    assert result.ok is equal
    if not equal:
        assert result.conflict is not None and result.conflict.reason == "stale_dependency"
        assert result.conflict.details["fields"] == ("",)
        assert client.dependency_set(other) is None


def test_field_a_b_a_and_termination_always_stale() -> None:
    _, client, up, down = seeded()
    register(client, down, up, ("name",)).check()
    client.write([Update("activity", up, 1, {"name": "B"})], SYS).check()
    assert len(client.stale_dependencies([up])) == 1
    client.write([Update("activity", up, 2, {"name": "A"})], SYS).check()
    assert client.stale_dependencies([up]) == []
    register(client, down, up, ("name",)).check()
    client.write([Supersede("activity", up, 3)], SYS).check()
    assert client.stale_dependencies([up])[0].target_superseded
    result = register(client, down, up, ("name",))
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"


@pytest.mark.parametrize("explicit", [False, True])
def test_whole_object_stays_strict(explicit: bool) -> None:
    _, client, up, down = seeded()
    client.write([Update("activity", up, 1, {"name": "A", "other": 1})], SYS).check()
    item = DependencyInput(ref(up)) if explicit else ref(up)
    result = client.write([RegisterDependencies("activity", down, [item])], SYS)
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    assert not register(client, down, up, ("missing",), version=9).ok


@pytest.mark.parametrize("whole", [False, True])
def test_duplicate_inputs_union_or_whole_wins(whole: bool) -> None:
    _, client, up, down = seeded()
    inputs: list[VersionRef | DependencyInput] = [
        DependencyInput(ref(up), ("references", "name")),
        DependencyInput(ref(up), ("", "name")),
    ]
    if whole:
        inputs.insert(1, ref(up))
    client.write([RegisterDependencies("activity", down, inputs)], SYS).check()
    (dep,) = client.dependencies_of(down)
    assert dep.fields == (None if whole else ("", "name", "references"))
    assert client.dependents_of(up) == [dep]
    assert client.dependency_set(down).dependencies == (dep,)  # type: ignore[union-attr]
    assert (
        client.write([RegisterDependencies("activity", down, inputs[::-1])], SYS)[0].outcome
        == "unchanged"
    )


@pytest.mark.parametrize("wrong_type", [False, True])
def test_duplicate_versions_or_types_are_invalid_before_freshness(wrong_type: bool) -> None:
    storage, client, up, down = seeded()
    before = storage.events
    first = ref(up)
    second = ref(up, 1 if wrong_type else 2, "rule" if wrong_type else "activity")
    with pytest.raises(InvalidRequest, match=r"重复|同一"):
        client.write([RegisterDependencies("activity", down, [first, second])], SYS)
    assert storage.events == before and client.dependencies_of(down) == []


def test_fields_metadata_snapshots_and_copy_isolation() -> None:
    now = [ts("2026-10-30T00:00:00Z")]
    storage = MemoryStorage(clock=lambda: now[0])
    client = storage.client("extraction")
    up, down = new_id(), new_id()
    client.write([Create("activity", up, {}), Create("activity", down, {})], SYS).check()
    assert client.dependency_set(down) is None
    assert client.dependency_set(new_id()) is None
    client.write([RegisterDependencies("activity", down, [])], SYS).check()
    empty = client.dependency_set(down)
    assert empty is not None and empty.empty and empty.registered_by == "extraction"
    assert empty.registered_at == now[0]
    now[0] += timedelta(hours=1)
    assert client.write([RegisterDependencies("activity", down, [])], SYS)[0].outcome == "unchanged"
    assert client.dependency_set(down) == empty
    with client.snapshot() as snapshot:
        register(client, down, up, ("z", "a")).check()
        assert snapshot.dependency_set(down) == empty
        stored = client.dependency_set(down)
        assert stored is not None and stored.registered_at == now[0]
        assert stored.dependencies[0].fields == ("a", "z")
        vars(stored.dependent)["version"] = 99
        vars(stored.dependencies[0].depends_on)["version"] = 99
        assert client.dependencies_of(down)[0].depends_on.version == 1
        assert client.dependency_set(down).dependent.version == 1  # type: ignore[union-attr]
    now[0] += timedelta(hours=1)
    assert register(client, down, up, ("a",)).check()[0].outcome == "written"
    assert client.dependency_set(down).registered_at == now[0]  # type: ignore[union-attr]
    client.write([Update("activity", down, 1, {"v": 2})], SYS).check()
    assert client.dependency_set(down) is None
    assert client.dependency_set(down, 1) is not None
    assert client.dependency_set(down, 0) is None
    assert storage.reader().dependency_set(down, 1) == client.dependency_set(down, 1)


@pytest.mark.parametrize("action", ["create", "changed", "unchanged"])
def test_requires_dependencies_missing_rolls_back_everything(action: str) -> None:
    storage, client, _, _ = seeded(required=True)
    down, system_id, event_id = new_id(), new_id(), new_id()
    if action != "create":
        client.write(
            [Create("derived", down, {"v": 1}), RegisterDependencies("derived", down, [])], SYS
        ).check()
    before = storage.events
    storage.client("api").subscribe("api.audit", lambda e: None)
    op: WriteOp = (
        Create("derived", down, {})
        if action == "create"
        else Update("derived", down, 1, {"v": 1 if action == "unchanged" else 2})
    )
    with pytest.raises(InvalidRequest, match="依赖"):
        client.write(
            [
                PutSystemRecord("extraction_run", system_id, {}),
                MarkProcessed("extraction", event_id),
                op,
            ],
            SYS,
        )
    assert storage.events == before and storage.bus.pending() == 0
    assert client.get_system_record(system_id) is None
    assert not client.is_processed("extraction", event_id)
    current = client.get(down)
    assert current is None if action == "create" else current is not None and current.version == 1


def test_required_unchanged_refresh_and_supersede_exemption() -> None:
    storage, client, up, _ = seeded(required=True)
    down = new_id()
    client.write(
        [Create("derived", down, {"v": 1}), RegisterDependencies("derived", down, [])], SYS
    ).check()
    result = client.write(
        [Update("derived", down, 1, {"v": 1}), RegisterDependencies("derived", down, [ref(up)])],
        SYS,
    ).check()
    assert [r.outcome for r in result.results] == ["unchanged", "written"]
    assert result.events == ()
    before = client.dependency_set(down)
    client.write([Supersede("derived", down, 1)], SYS).check()
    assert client.dependency_set(down) is None and client.dependency_set(down, 1) == before
    assert storage.events[-1].new_version == 2  # type: ignore[union-attr]


def test_register_old_dependent_then_update_requires_final_registration() -> None:
    _, client, _, _ = seeded(required=True)
    down = new_id()
    client.write(
        [Create("derived", down, {}), RegisterDependencies("derived", down, [])], SYS
    ).check()
    with pytest.raises(InvalidRequest, match="依赖"):
        client.write(
            [RegisterDependencies("derived", down, []), Update("derived", down, 1, {"v": 2})], SYS
        )
    result = client.write(
        [Update("derived", down, 1, {"v": 2}), RegisterDependencies("derived", down, [])], SYS
    ).check()
    assert result[1].version == 2
    assert client.dependency_set(down, 1) is not None


@pytest.mark.parametrize(
    ("fields", "payload", "accepted"),
    [
        (None, {"name": "A", "other": 1}, False),
        (("name",), {"name": "A", "other": 1}, True),
        (("name",), {"name": "B"}, False),
    ],
)
def test_batch_final_freshness(
    fields: tuple[str, ...] | None, payload: dict[str, Any], accepted: bool
) -> None:
    storage, client, up, down = seeded()
    before = storage.events
    system_id, event_id = new_id(), new_id()
    result = client.write(
        [
            RegisterDependencies("activity", down, [DependencyInput(ref(up), fields)]),
            PutSystemRecord("extraction_run", system_id, {}),
            MarkProcessed("extraction", event_id),
            Update("activity", up, 1, payload),
        ],
        SYS,
    )
    assert result.ok is accepted
    if not accepted:
        assert result.conflict is not None and result.conflict.reason == "stale_dependency"
        assert result.conflict.index == 0 and result.conflict.details["fields"] == fields
        assert storage.events == before and client.dependency_set(down) is None
        assert client.get_system_record(system_id) is None
        assert not client.is_processed("extraction", event_id)


def test_unrelated_old_stale_does_not_block_but_recomputed_result_is_checked() -> None:
    _, client, up, down = seeded()
    register(client, down, up).check()
    client.write([Update("activity", up, 1, {"name": "B"})], SYS).check()
    client.write([Create("activity", new_id(), {})], SYS).check()
    result = client.write([Update("activity", down, 1, {"body": "x"})], SYS)
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    # 同一登记先被更新的当前依赖集替换，不得仍检查已覆盖的边。
    client.write([RegisterDependencies("activity", down, [ref(up, 2)])], SYS).check()


@pytest.mark.parametrize("terminate_dependent", [False, True])
def test_batch_termination_freshness_scope(terminate_dependent: bool) -> None:
    _, client, up, down = seeded()
    ops: list[WriteOp] = [
        RegisterDependencies("activity", down, [DependencyInput(ref(up), ("name",))]),
        Supersede("activity", up, 1),
    ]
    if terminate_dependent:
        ops.append(Supersede("activity", down, 1))
    result = client.write(ops, SYS)
    assert result.ok is terminate_dependent
    if terminate_dependent:
        assert client.dependency_set(down, 1) is not None
        assert client.dependents_of(up) == []
        assert len(client.dependents_of(up, only_current_dependents=False)) == 1
    else:
        assert result.conflict is not None and result.conflict.reason == "stale_dependency"


@pytest.mark.parametrize("length", [1, 2, 3])
def test_final_cycles_reject_atomically_with_path(length: int) -> None:
    storage = MemoryStorage()
    client = storage.client("extraction")
    ids = [new_id() for _ in range(length)]
    client.write([Create("activity", id, {}) for id in ids], SYS).check()
    system_id, event_id = new_id(), new_id()
    before = storage.events
    ops: list[WriteOp] = [
        PutSystemRecord("extraction_run", system_id, {}),
        MarkProcessed("extraction", event_id),
    ]
    ops += [
        RegisterDependencies("activity", id, [ref(ids[(i + 1) % length])])
        for i, id in enumerate(ids)
    ]
    result = client.write(ops, SYS)
    assert result.conflict is not None and result.conflict.reason == "dependency_cycle"
    assert not result.conflict.retryable and result.results == () and result.events == ()
    path = result.conflict.details["path"]
    assert path[0] == path[-1] and {p["id"] for p in path} == set(ids)
    assert all(p["data_type"] == "activity" and p["version"] == 1 for p in path)
    assert storage.events == before and client.get_system_record(system_id) is None
    assert not client.is_processed("extraction", event_id)
    assert all(client.dependency_set(id) is None for id in ids)


def test_temporary_cycle_removed_in_final_graph_is_valid() -> None:
    _, client, a, b = seeded()
    register(client, a, b).check()
    client.write(
        [RegisterDependencies("activity", b, [ref(a)]), RegisterDependencies("activity", a, [])],
        SYS,
    ).check()
    # 即使临时自环，只要最终删除也不应过早拒绝。
    client.write(
        [RegisterDependencies("activity", a, [ref(a)]), RegisterDependencies("activity", a, [])],
        SYS,
    ).check()
    # 本批重复登记，最后一次才是最终集合；先登记的 whole 边不得泄漏进批末检查。
    client.write(
        [
            RegisterDependencies("activity", a, [ref(b)]),
            RegisterDependencies("activity", a, []),
            Update("activity", b, 1, {"body": "new"}),
        ],
        SYS,
    ).check()


def test_history_edges_do_not_create_current_cycle() -> None:
    _, client, a, b = seeded()
    register(client, a, b).check()
    client.write(
        [Update("activity", a, 1, {"v": 2}), RegisterDependencies("activity", b, [ref(a, 2)])], SYS
    ).check()
    assert client.dependencies_of(a) == [] and len(client.dependencies_of(a, 1)) == 1
    client.write([Supersede("activity", b, 1)], SYS).check()
    assert client.dependents_of(a) == []
    assert len(client.dependents_of(a, only_current_dependents=False)) == 1


def test_core_plugin_boundary_uses_registration_not_writer_name() -> None:
    registry = DEFAULT_REGISTRY.extend(
        DataTypeSpec("plugin_result", "semantic", "extraction"),
        DataTypeSpec("other_plugin", "semantic", "extraction"),
    ).extend_core(DataTypeSpec("trusted_result", "semantic", "extraction"))
    client = MemoryStorage(registry).client("extraction")
    core, plugin, other = new_id(), new_id(), new_id()
    client.write(
        [
            Create("trusted_result", core, {}),
            Create("plugin_result", plugin, {}),
            Create("other_plugin", other, {}),
        ],
        SYS,
    ).check()
    with pytest.raises(InvalidRequest, match=r"核心|插件"):
        client.write(
            [
                RegisterDependencies(
                    "trusted_result", core, [ref(plugin, data_type="plugin_result")]
                )
            ],
            SYS,
        )
    client.write(
        [
            RegisterDependencies("plugin_result", plugin, [ref(core, data_type="trusted_result")]),
            RegisterDependencies("other_plugin", other, [ref(plugin, data_type="plugin_result")]),
        ],
        SYS,
    ).check()


def test_raw_target_fields_and_snapshot_stale_copies() -> None:
    storage, client, _, down = seeded()
    raw = make_raw("voiceprint", {"": [True, None]}, start="2026-10-30T00:00:00Z")
    storage.client("adapter").write([AppendRaw(raw)], SYS).check()
    client.write(
        [
            RegisterDependencies(
                "activity", down, [DependencyInput(ref(raw.id, data_type="voiceprint"), ("",))]
            )
        ],
        SYS,
    ).check()
    assert client.stale_dependencies([raw.id]) == []
    _, client, up, down = seeded()
    register(client, down, up, ("name",)).check()
    with client.snapshot() as snapshot:
        client.write([Update("activity", up, 1, {"name": "B"})], SYS).check()
        assert snapshot.stale_dependencies([up]) == []
    stale = client.stale_dependencies([up])[0]
    vars(stale.depends_on)["version"] = 99
    assert client.dependencies_of(down)[0].depends_on.version == 1
    client.write([Update("activity", down, 1, {"body": "v2"})], SYS).check()
    assert client.stale_dependencies([up]) == []
    assert client.stale_dependencies([up], only_current_dependents=False)[0].fields == ("name",)


def test_long_chain_nonrecursive_cycle_check() -> None:
    client = MemoryStorage().client("extraction")
    ids = [new_id() for _ in range(1101)]
    client.write([Create("activity", id, {}) for id in ids], SYS).check()
    client.write(
        [RegisterDependencies("activity", a, [ref(b)]) for a, b in pairwise(ids)],
        SYS,
    ).check()
    result = client.write([RegisterDependencies("activity", ids[-1], [ref(ids[0])])], SYS)
    assert result.conflict is not None and result.conflict.reason == "dependency_cycle"
    assert len(result.conflict.details["path"]) == len(ids) + 1


def test_two_threads_opposite_edges_one_rejected() -> None:
    # 仅证明 MemoryStorage 的全局锁串行提交，不是数据库并发证明。
    _, client, a, b = seeded()
    barrier = threading.Barrier(2)

    def add_edge(pair: tuple[str, str]) -> WriteResult:
        barrier.wait(timeout=5)
        return client.write([RegisterDependencies("activity", pair[0], [ref(pair[1])])], SYS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add_edge, pair) for pair in [(a, b), (b, a)]]
        results = [future.result(timeout=10) for future in futures]
    assert sum(r.ok for r in results) == 1
    rejected = next(r for r in results if not r.ok)
    assert rejected.conflict is not None and rejected.conflict.reason == "dependency_cycle"
    assert len(client.dependencies_of(a)) + len(client.dependencies_of(b)) == 1


def test_cycle_default_no_retry() -> None:
    _, client, a, _ = seeded()
    calls = []

    def plan(reader: Any) -> list[WriteOp]:
        calls.append(1)
        return [RegisterDependencies("activity", a, [ref(a)])]

    result = write_with_retry(client, plan, SYS)
    assert result.conflict is not None and result.conflict.reason == "dependency_cycle"
    assert len(calls) == 1


def test_requires_each_result_version_even_before_termination() -> None:
    _, client, _, _ = seeded(required=True)
    down = new_id()
    with pytest.raises(InvalidRequest, match="版本 1"):
        client.write(
            [
                Create("derived", down, {}),
                Update("derived", down, 1, {"v": 2}),
                RegisterDependencies("derived", down, []),
                Supersede("derived", down, 2),
            ],
            SYS,
        )
    assert client.get(down) is None
    client.write(
        [
            Create("derived", down, {}),
            RegisterDependencies("derived", down, []),
            Update("derived", down, 1, {"v": 2}),
            RegisterDependencies("derived", down, []),
            Supersede("derived", down, 2),
        ],
        SYS,
    ).check()
    assert client.dependency_set(down, 1) is not None
    assert client.dependency_set(down, 2) is not None
    assert client.dependency_set(down) is None


def test_required_unchanged_update_and_unchanged_empty_registration() -> None:
    _, client, _, _ = seeded(required=True)
    down = new_id()
    client.write(
        [Create("derived", down, {}), RegisterDependencies("derived", down, [])], SYS
    ).check()
    before = client.dependency_set(down)
    result = client.write(
        [Update("derived", down, 1, {}), RegisterDependencies("derived", down, [])], SYS
    ).check()
    assert [r.outcome for r in result.results] == ["unchanged", "unchanged"]
    assert result.events == () and client.dependency_set(down) == before


def test_unchanged_nonempty_registration_still_checked_at_batch_end() -> None:
    storage, client, up, down = seeded()
    register(client, down, up).check()
    before = client.dependency_set(down), storage.events
    result = client.write(
        [RegisterDependencies("activity", down, [ref(up)]), Update("activity", up, 1, {})], SYS
    )
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    assert result.conflict.index == 0
    assert (client.dependency_set(down), storage.events) == before


def test_registered_dependent_becomes_history_not_current_freshness_subject() -> None:
    _, client, up, down = seeded()
    client.write(
        [
            RegisterDependencies("activity", down, [ref(up)]),
            Update("activity", up, 1, {}),
            Update("activity", down, 1, {}),
        ],
        SYS,
    ).check()
    assert client.dependency_set(down) is None
    assert client.stale_dependencies([up]) == []
    assert len(client.stale_dependencies([up], only_current_dependents=False)) == 1


@pytest.mark.parametrize("whole_first", [False, True])
def test_duplicate_whole_wins_restores_strict_historical_version_check(whole_first: bool) -> None:
    _, client, up, down = seeded()
    client.write([Update("activity", up, 1, {"name": "A", "extra": 1})], SYS).check()
    inputs: list[VersionRef | DependencyInput] = [DependencyInput(ref(up), ("name",)), ref(up)]
    if whole_first:
        inputs.reverse()
    result = client.write([RegisterDependencies("activity", down, inputs)], SYS)
    assert result.conflict is not None and result.conflict.reason == "stale_dependency"
    assert result.conflict.details["fields"] is None


def test_cycle_uses_current_objects_even_when_field_ref_is_historical() -> None:
    _, client, up, down = seeded()
    client.write([Update("activity", up, 1, {"name": "A", "extra": 1})], SYS).check()
    register(client, down, up, ("name",)).check()  # 合法引用 up/v1。
    result = client.write([RegisterDependencies("activity", up, [ref(down)])], SYS)
    assert result.conflict is not None and result.conflict.reason == "dependency_cycle"
    versions = {node["id"]: node["version"] for node in result.conflict.details["path"]}
    assert versions == {up: 2, down: 1}
