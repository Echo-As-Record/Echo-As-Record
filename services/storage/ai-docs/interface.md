---
generated-by: Claude (AI)，应 aytony 要求
generated-at: 2026-11-01
based-on: docs/design/design-v1.1.md (reviewed-at 2026-10-30), docs/design/signals-supplement.md (reviewed-at 2026-10-29), docs/design/work-breakdown-v1.1.md (reviewed-at 2026-10-30), docs/glossary.md, contracts/, ai-docs/proposals/module-internal-structure.md, 分支 storage/contracts-and-sdk
---

# ① 存储与事件：接口说明 v0.2（未审定）

> 写给 ②～⑦ 的负责人。字段以 `contracts/` 为准，Python 接口以 `libs/ear_storage_client` 为准，本文只解释怎么用、承诺什么。与已审定文档冲突时以已审定文档为准。

## 0. 先看这个

```python
from ear_contracts import DataChanged, VersionRef, new_id
from ear_storage_client import (
    AppendRaw, Context, Create, MarkProcessed, MemoryStorage, Query, Update,
)
from ear_storage_client.testing import make_raw

storage = MemoryStorage()                      # 开发、测试用的内存实现
adapter = storage.client("adapter")            # 每个模块用自己的模块名取客户端
attribution = storage.client("attribution")

def on_event(event):                           # 收到事件后先查当前状态再决定
    if isinstance(event, DataChanged) and event.data_type == "voiceprint":
        ...
        attribution.write([..., MarkProcessed("attribution", event.event_id)], Context.of(event))

attribution.subscribe("attribution", on_event)
adapter.write([AppendRaw(make_raw("voiceprint", {...}, start="2026-10-29T06:05:12Z"))],
              Context.system())
storage.run_until_idle()                       # 内存实现里同步投递事件
```

各模块的代码只依赖 `StorageClient`（`protocol.py`），不要依赖 `MemoryStorage`。正式实现（数据库 + 总线）提供同样的接口，换实现时调用方不用改。

## 1. 数据怎么存

| 分类（术语表） | 存成 | 契约 | 写入方式 |
|---|---|---|---|
| 原始数据 `raw_record` | 一条记录，`version` 固定为 1 | `contracts/records/raw-record` | `AppendRaw`，只追加 |
| 语义信息 `semantic`、规则 `rule` | 一个 id 下的一串版本 | `contracts/records/semantic-record` | `Create`、`Update`、`Supersede` |
| 系统记录 `system_record` | 一条可覆盖的记录，带 `revision` | `contracts/records/system-record` | `PutSystemRecord`，不发事件 |
| 依赖登记 | 「依赖方版本 → 被依赖方版本」 | `contracts/records/dependency-record` | `RegisterDependencies`，不发事件 |

- **数据类型登记表** `contracts/registry/data-types.json`：每种 `data_type` 属于哪一类、由哪个模块写。① 按它检查写入权限，不在代码里写死类型。扩展模块用 `DEFAULT_REGISTRY.extend(...)` 登记新类型。
- **id 由写入方生成**（`ear_contracts.new_id()`，UUIDv7）。所以同一批里可以先建对象再引用它，重试时 id 也不变。
- **时间**一律 UTC。原始数据的时间段由写入方填；活动、产物等有时间的语义信息由写入方填 `event_start/event_end`；归属关系的时间由 ① 从 `payload.subject_id` 指向的原始数据复制，写入方不填。
- **整体替代**：对象不再使用时（重新提取的旧活动、没有声纹的未命名人物、作废的建议），用 `Supersede`。① 新增一个 `status = superseded` 的版本 n+1（内容同 n），之后不能再写入。读当前版本时看 `record.superseded`。

### 1.1 ① 依赖的 payload 字段（待 ③ 确认）

| 类型 | 字段 | ① 的规则 |
|---|---|---|
| `attribution` | `subject_id`（原始数据 id） | 同一个 `subject_id` 最多只有一个未被替代的归属关系；时间从它复制；`unreferenced_by=("attribution", "subject_id")` 查「还没有归属对象」 |
| `person`、`place` | `references`（原始数据 id 数组） | 同一个 id 在所有未被替代的人物（地点）里最多出现一次 |

其余 payload 字段由负责写它的模块约定，① 不解析。建议归属关系的 payload 写成 `{subject_type, subject_id, target_type, target_id}`。`references` 这类没有顺序含义的数组，请**排序后再写**，否则顺序变化会被当成内容变化。

## 2. 写入：一批操作，原子提交

```python
result = client.write([op1, op2, ...], context)
```

- **要么全部生效，要么全部不生效。** 生效后，每条有变化的原始数据、语义信息、规则各发一条「数据库被修改」，同一批事件的 `batch_id` 相同。
- `context = Context(root_feedback_id, depth)`：整批共用，① 原样写进每个版本和每条事件。
  - 系统发起：`Context.system()`；执行用户反馈 F：`Context.feedback(F)`；按事件或「请求重新判断」写入：`Context.of(event)`；⑤ 发请求：`Context.of(event).deeper()`。
  - ⑥ 写用户反馈时，先生成反馈 id，再用 `Context.feedback(那个 id)` 写入，这条反馈的事件根源就是它自己。
- **`skip_history` 按每条操作设置**（`Create/Update/Supersede(..., skip_history=True)`），因为同一次纠正里参考要标记、归属关系不标记。

### 2.1 操作一览

| 操作 | 谁用 | 要点 |
|---|---|---|
| `AppendRaw(record)` | ②、⑥ | 按 `(data_type, adapter_version, dedup_key)` 去重；已存在 → `deduplicated`，返回已有 id，不发事件 |
| `Create(type, id, payload, dedup_key=None, ...)` | ③④ | 版本 1。`dedup_key` 可选，同类型内永久唯一，例如 `unnamed_person:<声纹 id>` |
| `Update(type, id, expected_version, payload, ...)` | ③④ | 乐观锁；payload 是完整新内容；内容没变 → `unchanged`，不产生版本，不发事件 |
| `Supersede(type, id, expected_version)` | ③④ | 整体替代；已替代 → `unchanged` |
| `RegisterDependencies(type, id, depends_on)` | ③④ | 见第 4 节 |
| `PutSystemRecord(type, id, payload, expected_revision=None, ...)` | ④⑤ | 新建或整体覆盖；不发事件 |
| `PublishReevaluation(target_type, target_id, cause, description)` | 只有 ⑤ | depth ≥ 1；和传播记录放同一批 |
| `MarkProcessed(consumer, event_id)` | 所有消费方 | 已记录过 → 整批冲突 `already_processed`，见第 5 节 |

### 2.2 结果、冲突与异常

`WriteResult.results[i]` 与第 i 条操作对应，`outcome` 是 `written`、`unchanged`、`deduplicated` 之一。

**冲突不抛异常**，返回 `result.conflict`（只报第一处），整批不生效。需要异常时用 `result.check()`。

| reason | 含义 | 怎么办 |
|---|---|---|
| `version_mismatch` | 当前版本不是 `expected_version` | 重新读取、按当前状态重新判断 |
| `object_superseded` | 对象已整体替代 | 同上 |
| `not_found` | 要修改或引用的对象不存在 | 同上 |
| `dedup_key_exists` | 语义对象的去重键已被用（`details.existing_id`） | 同上，通常说明别人已经做了 |
| `unique_field_taken` | 例如同一声纹已有归属关系 | 同上 |
| `member_taken` | 例如同一声纹已是别人的参考 | 同上；纠正时把「移出」和「加入」放同一批 |
| `stale_dependency` | 登记依赖时被依赖方已有新版本 | 用新版本重新计算 |
| `revision_mismatch` | 系统记录的 revision 已变 | 重新读取 |
| `id_exists` | id 已被使用 | 通常说明已经做过，不要重试 |
| `already_processed` | 事件已处理过 | 直接确认事件，什么也不做 |

`write_with_retry(client, plan, context)` 封装了「快照读取 → 计算 → 写入 → 可重试冲突时重来」。

**请求本身有错才抛异常**，重试也没用：`PermissionDenied`（不归你写、不能发这种事件、consumer 名不对）、`InvalidRequest`（类型未登记、操作不适用、payload 不是 JSON、时间区间错等）。

## 3. 读取

| 方法 | 说明 |
|---|---|
| `get(id)` | 当前版本（原始数据或有版本的数据），已替代的对象也能读到 |
| `get_version(id, v)`、`history(id)` | 任意历史版本 |
| `get_many(ids)` | 批量读，同一时刻的状态 |
| `query(Query(...))`、`count(...)`、`iter_query(...)` | 按类型查；有版本的数据只看当前版本，默认不含已替代的 |
| `get_system_record(id)` | 系统记录 |
| `with client.snapshot() as r:` | 块内所有读取基于同一时刻 |

`Query` 的条件：`time=TimeRange(start, end)`（半开区间；单点数据 `start <= t < end`）、`where={字段: 值}`、`contains={数组字段: 值}`、`unreferenced_by=(类型, 字段)`、`source_user`、`include_superseded`、`order`、`limit`（≤ 1000）、`cursor`。

常用查询：

```python
Query("voiceprint", unreferenced_by=("attribution", "subject_id"))        # ③ 待归属的声纹
Query("gps_point", time=rng, unreferenced_by=("attribution", "subject_id"))  # ④ 前置检查（count）
Query("attribution", time=rng)                    # ④ 活动时间内的全部归属关系
Query("attribution", where={"target_id": pid})    # 归属到某人物的全部数据
Query("person", contains={"references": vid})     # 这条数据是谁的参考
Query("extraction_run", where={"status": "succeeded"})
```

写入成功后立即可读。事件一定在提交之后才发出，所以收到事件再读，一定能读到事件里的版本（也可能更新）。

## 4. 依赖登记与查询

```python
RegisterDependencies("artifact", art_id, [VersionRef(data_type="attribution", id=a, version=3), ...])
```

- 登记的是**依赖方当前版本**的依赖，整体替换这个版本之前的登记。只有负责写依赖方的模块能登记。
- **被依赖方必须是当前版本且未被替代**，否则冲突 `stale_dependency`。这样不会出现「⑤ 已经处理完修改，之后才登记了过时的依赖」而漏查。
- **和依赖方的写入放在同一批**，避免「产物写了、依赖还没登记」。
- **重新判断后结果不变，也要用新的被依赖版本重新登记**（`Update` 返回 `unchanged` 不影响登记），否则它会一直被查成过时。

查询（⑤ 使用）：

```python
client.stale_dependencies([changed_id])     # 依赖它且登记版本已过时的；默认只看依赖方当前版本
client.dependents_of(target_id)            # 依赖它的全部登记，不论是否过时
client.dependencies_of(dependent_id)       # 某个依赖方当前版本的全部依赖
```

被依赖方整体替代时版本号也会加 1，所以 `stale_dependencies` 同样能查出来（`target_superseded=True`）。

## 5. 事件与重复投递

- 两种事件，格式见 `contracts/events/`，Python 类型 `DataChanged`、`ReevaluationRequested`。`DataChanged` 新增了可选字段 `writer`、`batch_id`。
- `operation`：`created`（新增）、`updated`（新版本，旧版本随之被替代，**不另发** superseded）、`superseded`（整体替代，`new_version` 是新增的替代版本）。
- 订阅：`client.subscribe(consumer, handler, event_types=None)`，`consumer` 是模块名或「模块名.xxx」。每个 consumer 各收到一份。
- **至少投递一次**：同一事件可能送达多次；handler 抛异常会重投，超过次数进死信。
- 保证只生效一次的推荐写法：把处理结果和 `MarkProcessed(consumer, event.event_id)` 放进同一批；收到 `already_processed` 就直接结束。不写 ① 的消费方（如 ⑥ 执行外部动作）用 `is_processed` 加 `MarkProcessed` 单独记录。
- 不认识的事件类型、不认识的字段一律忽略：`parse_event` 遇到未知类型返回 `None`。

内存实现的测试工具：`MemoryStorage(duplicate_delivery=True)` 让每个事件再投递一次；`max_attempts`、`dead_letters` 测失败重投；`subscribe(..., from_beginning=True)` 重放已有事件；`storage.events` 是全部已发事件。

## 6. 和设计文档的对应

| 工作分解 ① 的验收 | 实现 | 测试（`libs/ear_storage_client/tests/test_memory.py`） |
|---|---|---|
| 同一原始数据写多次只存一条 | 去重键 | `test_raw_dedup` |
| 写入成功就发事件，失败就不发 | 原子批次；正式实现用 outbox | `test_batch_is_atomic_*` |
| 内容没变不产生版本、不发事件 | 规范化 JSON 比较，字符串逐字 | `test_unchanged_content` |
| 能读出任意历史版本 | 版本链 | `test_history_and_supersede` |
| 依赖查询不遗漏 | 登记时要求当前版本 + 版本比较 | `test_stale_dependencies` 等 |

## 7. 还没做的

- 正式实现：PostgreSQL + outbox + 总线 + relay，选型写成 ADR。接口不变。
- ⑤ 的分发登记表形式由 ⑤ 决定；它可以直接读 `contracts/registry/data-types.json` 的 `writer` 字段。
- 设计文档空缺（只报告）：重新判断后结果不变时 ⑤ 收不到信号（可以让 ③④ 写一条系统记录）；大模型调用记录存在 ① 还是 ⑦ 未定。
