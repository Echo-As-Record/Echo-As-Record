---
audience: ai
doc-type: guide
based-on: docs/modules/storage.md；contracts/；libs/ear_storage_client 与 libs/ear_contracts（origin/dev 48d3c51）；原接口说明 v0.2 的职责拆分；设计v1.2同步（代码仍为原基线）；P0a 修复（基于 6cea0b7，首个提交 a6a1c5a）；P0b 字段依赖与登记表能力（本分支，提交以分支历史为准）
---

# ① SDK 使用与实现导航

面向开发代理的工程指南。产品职责、跨模块保证和未决语义见[人的接口说明](../../../docs/modules/storage.md)，字段以 contracts、当前调用签名以代码为准；遇到不一致按[审议清单](../../../docs/proposals/review-agenda.md)处理，不自行改义；原审议清单现为决定结果导航。

## 0. 当前代码与新目标不能混淆

本页示例描述现有 SDK，不是 v1.2 的完整接口。①独立服务、PostgreSQL同库多schema、JetStream/Outbox、通用完成记录、动态插件/权限/管理反馈消费已获批准，**但未实现**。字段依赖、按能力强制登记、批末新鲜度与当前图无环已在内存实现中完成（P0b，本分支，未经合并前审核），持久化版本未实现；P1 计划见[持久化服务工程方案](persistent-service-plan.md)。跟进[实施计划](../../../ai-docs/plans/mvp-implementation.md)，不要因下文旧接口可以调用就跳过新目标。

## 1. 先定位代码

| 内容 | 路径（仓库根相对） |
|---|---|
| 公共读取/写入/订阅接口 | `libs/ear_storage_client/src/ear_storage_client/protocol.py` |
| 写操作与参数 | `libs/ear_storage_client/src/ear_storage_client/ops.py` |
| 结果、冲突、异常 | `results.py`、`errors.py`（同包） |
| 查询与分页 | `queries.py`（同包） |
| Context 与反馈传播 | `context.py`（同包） |
| 参考实现、进程内事件队列 | `memory.py`（同包） |
| 合成数据工具 | `testing.py`（同包，仅开发/测试） |
| 公共模型与注册表 | `libs/ear_contracts/src/ear_contracts/` |
| 行为测试 | `libs/ear_storage_client/tests/test_memory.py`；P0a 回归 `test_memory_regressions.py`；P0b `test_p0_sdk_models.py`、`test_p0_memory_dependencies.py`（同目录）；契约模型 `libs/ear_contracts/tests/test_p0_contract_models.py` |

业务模块依赖 `StorageClient`，不要依赖 `MemoryStorage` 的内部状态。正式后端尚未实现，不要假设存在 HTTP 路由、生产总线或 SQL 表。

## 2. 最小可运行示例（合成数据）

```python
from ear_contracts import DataChanged, Event
from ear_storage_client import AppendRaw, Context, MarkProcessed, MemoryStorage
from ear_storage_client.testing import make_raw

storage = MemoryStorage(duplicate_delivery=True)
adapter = storage.client("adapter")
consumer = storage.client("attribution")
seen = []

def on_event(event: Event) -> None:
    if not isinstance(event, DataChanged) or event.data_type != "voiceprint":
        return
    current = consumer.get(event.data_id)
    assert current is not None
    result = consumer.write(
        [MarkProcessed("attribution.demo", event.event_id)], Context.of(event)
    )
    if result.conflict is not None and result.conflict.reason == "already_processed":
        return
    result.check()
    seen.append(current.id)

consumer.subscribe("attribution.demo", on_event)
record = make_raw("voiceprint", {"sample": "synthetic"}, start="2026-10-29T06:05:12Z")
adapter.write([AppendRaw(record)], Context.system()).check()
storage.run_until_idle()
assert seen == [record.id]
```

这里只演示内存消费标记，不执行归属算法。真实消费方应把业务写入和 `MarkProcessed` 放在同一批；外部副作用不能由这一批提供原子保证。

## 3. 写入调用

入口为 `client.write(ops, context)`。操作类型是 `AppendRaw`、`Create`、`Update`、`Supersede`、`RegisterDependencies`、`PutSystemRecord`、`PublishReevaluation`、`MarkProcessed`，准确签名见[操作定义](../../../libs/ear_storage_client/src/ear_storage_client/ops.py)。

- `Context.system()`：系统行为；`Context.feedback(id)`：反馈执行；`Context.of(event)`：沿用触发上下文；仅⑤构造请求时用 `.deeper()`。
- `skip_history` 在每个 Create/Update/Supersede 上设置，不放 Context。
- 新对象 ID 由 `ear_contracts.new_id()` 生成。重试计划不要无意改变本应稳定的 ID 或去重键。
- `Update` 传完整 payload 和必填 expected_version；`PutSystemRecord` 用 expected_revision；不能盲目重试旧计算。
- `WriteResult.results` 按操作对应；outcome 为 written/unchanged/deduplicated。`conflict` 表示整批拒绝；`.check()` 才会把冲突转成异常。
- 常见冲突：version_mismatch、object_superseded、not_found、dedup_key_exists、unique_field_taken、member_taken、stale_dependency、revision_mismatch、id_exists、already_processed、dependency_cycle（P0b 新增，不可重试：需改变依赖关系本身）。以 `results.py` 的 retryable 定义为准；不认识的原因按不可重试处理。
- `write_with_retry` 做快照读取、重新计算、有限重试，**最终仍可能返回 conflict**。消费 handler 不得忽略结果后正常退出，否则内存总线会当成成功。
- 权限/请求错误为 `PermissionDenied` / `InvalidRequest`，不是盲重试能够解决的问题。

## 4. 读取与查询

`get`、`get_version`、`history` 读取对象；`get_many` 批量读取，缺失项不在字典中；系统记录用 `get_system_record`。`history` 对原始数据返回空列表，原始数据用 `get_version(id, 1)`。

`Query` 支持 time、where、contains、unreferenced_by、source_user、include_superseded、order、limit、cursor。当前批量分页使用 offset，不应在实时变化的数据上假设跨页一致；需要稳定视图时使用 snapshot。

| 调用表达式 | 用途 |
|---|---|
| `Query("voiceprint", unreferenced_by=("attribution", "subject_id"))` | 待归属声纹 |
| `Query("gps_point", time=rng, unreferenced_by=("attribution", "subject_id"))` | 配合 count 做提取前置检查 |
| `Query("attribution", time=rng)` | 时间内的归属关系 |
| `Query("attribution", where={"target_id": pid})` | 归属某个人物的数据 |
| `Query("person", contains={"references": vid})` | 声纹是哪位人物的参考 |
| `Query("extraction_run", where={"status": "succeeded"})` | 成功提取记录 |

归属关系的事件时间由①从 `subject_id` 指向的原始数据复制，调用方不要显式填写；活动、产物等有时间的对象由写入方填写。

当前内存实现的写入提交后立即可读，事件在提交后入队；消费者读取时可能已看到更新的版本，复核事件版本应使用 get_version。get_many 的各项基于同一时刻，snapshot 内的多次读取基于同一视图；这不表示持久化后端已实现这些保证。

表中 rng/pid/vid 为调用方变量。涉及的 payload 字段应与③④确认，不将示例当成已定的完整 payload schema。

## 5. 依赖登记：当前实现的调用注意事项

- 每次 `RegisterDependencies` **整体替换该依赖方版本的完整依赖集合**，必须提交全部依赖，不是增量追加；漏传旧依赖会移除它们及其反向索引。
- `depends_on` 接受 `VersionRef` 与 `DependencyInput(ref, fields)` 的混合序列。`VersionRef` 等同 `DependencyInput(ref)`，依赖整个对象。带 `fields` 额外字段的 `VersionRef` 以 `InvalidRequest` 拒绝（以前被静默丢弃），字段依赖必须用 `DependencyInput`。
- `fields` 是被依赖方 payload 的顶层键集合，任意字符串（含空串），自动去重排序；空集合、单个字符串、非字符串元素拒绝。同一对象同版本多次出现时合并（任一项为整个对象则按整个对象，否则取并集）；版本或类型不同拒绝。
- 默认 dependent_version 为同批前面操作执行后的当前版本。可显式传入预期依赖方版本。
- 推荐把 Update/Create 与登记放在一批。Update 不自动继承依赖。登记表中 `requires_dependencies=true` 的类型，Create/Update（含 unchanged）必须同批登记结果版本（可为显式空集），否则整批 `InvalidRequest`；Supersede 免登记。**内置核心类型目前全部为 false**，即当前不强制，启用需调用方先迁移。
- 批末检查：本批登记或重算、且最终仍为当前有效版本的依赖按最终状态复查新鲜度（`stale_dependency`）；当前有效对象图成环返回 `dependency_cycle`（`details.path` 给出环路径）；核心类型依赖插件类型为 `InvalidRequest`。其他对象已有的过时依赖不影响提交。核心/插件由构造 `MemoryStorage` 时的登记表决定（`extend` 为插件、`extend_core` 为受信核心），不是运行时动态注册。
- 内容无变化时也可刷新依赖登记，不产生新的业务版本或 data_changed。
- 整个对象模式只允许登记未整体替代的当前输入版本；字段模式允许登记较旧版本，只要所选字段的值（`canonical_json` 比较，`1`≠`1.0`、缺失≠null）与当前版本相同且未终止。历史重判使用当前规则已获批准；旧规则失效应替代产物，不把原历史版本偷偷登记为最新版本。此页只说明代码现状，规则业务尚未实现。
- `dependency_set(id, version=None)` 返回完整依赖集：从未登记为 None，显式空集为 `empty=True`；同集重复登记为 unchanged，不改 `registered_at`。`dependencies_of` 查输入（无法区分空集与未登记）；`dependents_of` 查反向依赖；`stale_dependencies` 查过时依赖（整个对象按版本、字段模式按字段值，整体替代始终过时），默认只看有效的当前依赖方，结果带 `fields`。

## 6. 事件与测试工具

两种事件的模型位于 [ear_contracts/events.py](../../../libs/ear_contracts/src/ear_contracts/events.py)；未知事件由 parse_event 返回 None，调用方忽略。不要新增未经决策的第三种事件。

`subscribe(consumer, handler, event_types=None, from_beginning=False)` 的 consumer 必须为本模块名或以“模块名.”开头。handler 抛异常触发重投，超过次数进入 dead_letters。

handler 内写入的固定处理方式（见第 2 节示例）：先单独判断 `already_processed`（说明之前已提交，正常 return）；其余情况调用 `result.check()`，把冲突转成异常以触发重投。其他冲突应重算或显式失败，不能默默确认。总线无法发现 handler 忽略了 `write()` 的返回值；与本 consumer 无关的事件可以直接 return。

内存辅助功能：`duplicate_delivery=True`、`max_attempts`、`run_until_idle()`、`dead_letters`、`storage.events`、from_beginning 重放。它们不证明跨进程持久化或外部动作恰好执行一次。`run_until_idle` 中，同一 consumer 同时只处理一个事件：多个 runner 或 handler 内嵌套调用会跳过正在处理的 consumer（不等待），由持有它的 runner 继续投递；超过 `max_deliveries` 时抛出 RuntimeError，未投递的事件留在队列中（P0a 修复，状态见 [progress](progress.md)）。

## 7. 验证与已知边界

```bash
uv run pytest libs/ear_storage_client/tests/test_memory.py libs/ear_storage_client/tests/test_memory_regressions.py
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && uv run python tools/check_docs.py
```

当前实现以排序键的 json.dumps 比较 payload，不是 RFC 8785/JCS 实现。修改比较算法时注意数值、数组、字符串及事件时间的一致性。

已知静态审查关注项（run_until_idle 在检查投递上限前出队、并发 runner 的 consumer 串行边界、空时间查询区间、系统记录更新路径的时间校验）已在本分支补回归测试（`test_memory_regressions.py`）并修复，提交与合并状态以分支历史和 PR 为准，见 [progress](progress.md)。正式后端、故障测试与已采纳语义的实现差距也见[进度](progress.md)。
