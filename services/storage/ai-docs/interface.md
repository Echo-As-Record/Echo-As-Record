---
audience: ai
doc-type: guide
based-on: docs/modules/storage.md；contracts/；libs/ear_storage_client 与 libs/ear_contracts（origin/dev 48d3c51）；原接口说明 v0.2 的职责拆分；设计v1.2同步（代码仍为原基线）；P0a 修复（基于 6cea0b7，首个提交 a6a1c5a）；P0b 字段依赖与登记表能力（cf337d1）；P1 实际代码（提交 d7c9919；services/storage/src/ear_storage，ear_storage_client 的 engine/wire/remote/remote_errors）；P2/P3 后端提交 d0d8875（relay.py、consumers.py、consumer_broker.py、consumer_wire.py 与远程 subscribe；management.py、management_input.py、credentials.py、reconfiguration.py）；合并提交 b7307e9；P4 集合守护 f747e89、重判完成记录 106cd2e（completion.py）；多起因（causality.py 等）提交 daa767f；⑦调用记录（ear_contracts/calls.py、ear_storage_client/model_body.py 等）已在本分支实现，提交状态以提交历史为准；均在 PR8，未合并
---

# ① SDK 使用与实现导航

面向开发代理的工程指南。产品职责、跨模块保证和未决语义见[人的接口说明](../../../docs/modules/storage.md)，字段以 contracts、当前调用签名以代码为准；遇到不一致按[审议清单](../../../docs/proposals/review-agenda.md)处理，不自行改义；原审议清单现为决定结果导航。

## 0. 当前代码与新目标不能混淆

本页示例描述现有 SDK，不是 v1.2 的完整接口。P0b（cf337d1）：字段依赖、按能力强制登记、批末新鲜度与当前图无环（内存）。P1（d7c9919）：PostgreSQL 后端、可信 token 身份与读授权、请求 ID 重放、有界 snapshot、HTTP `/v1` 与同步 `RemoteStorageClient`（工程细节见[P1 工程指南](persistent-service-plan.md)，状态见 [progress](progress.md)）。P2（d0d8875）：有序 Outbox Relay → JetStream、服务端授权的持久消费者（经 HTTP `/v1/consumers/*` 代理）与远程 `subscribe`。P3 后端（d0d8875）：管理反馈 `/v1/management/requests` 与 `manage-worker`；凭据动作 `rotate_credentials` / `revoke_credentials` 与一次性领取 `POST /v1/management/credentials/{feedback_id}/claim`（schema revision 7）；订阅范围变更由①经管理反馈做完整重配置（revision 8：新增类型只收切换后的事件，原有积压保留），模块不能靠重新注册扩大订阅。规则与验证见 [progress](progress.md)。以上均在 PR8，**未合并**、未经合并前审核。P4 ①侧集合守护（schema revision 9，f747e89）已提交，见第 4 节；重判完成记录（106cd2e）与多起因（daa767f）已提交，⑦调用记录 `llm_call` 及正文策略已提交 e9391dd，均见第 6 节。P5 ①侧插件历史恢复（schema revision 14）已在本分支实现（提交状态以提交历史为准），见第 1、6 节。**仍未实现**：⑥管理页（P3 余项）；P4 的⑦网关服务与实际模型调用、③④⑤⑦业务接入；P5 的⑤实际调度、④业务接入与真实多层示例。跟进[实施计划](../../../ai-docs/plans/mvp-implementation.md)，不要因下文旧接口可以调用就跳过新目标。

远程客户端调用规则（以 `libs/ear_storage_client/src/ear_storage_client/remote.py`、`remote_errors.py` 为准）：

- 模块身份只来自部署签发的 token；不读环境变量，没有隐藏重试。token 由部署内 `AuthOperator` 签发，不要在业务代码里签发或保存明文。
- `write(ops, context, request_id=None)` 默认生成 UUIDv7。遇到 `CommitUnknown` 时**不要重新计算**，用 `e.request_id` 与相同 ops、context 重放；`write_with_retry` 不会替你重放。冲突仍按第 3 节处理。
- `snapshot()` 创建有界持久租约（默认 TTL 300 s，上限见指南），超限报 `ResourceLimit`，过期/不可用报 `SnapshotUnavailable`；不要依赖长时间持有。
- 远程 `subscribe`（P2，d0d8875；签名以 `remote.py` 为准）向服务注册持久 consumer；事件只在调用 `poll_once()` 或 `run_until_idle(max_deliveries=100)`（1–10 000，也限制轮询轮数）时投递，是调用方驱动的有界泵，没有后台线程替你拉取。
- 只投递有读权限且带 `can_subscribe` 的类型的 `data_changed`；`reevaluation_requested` 只投给该类型的 writer 且类型 `supports_reevaluation`。注册默认**只收未来事件**；模块重新注册不能改变已有订阅配置，扩大或收缩订阅须经①管理反馈做完整重配置（P3，schema revision 8：新增类型只收切换后的事件，原有积压保留）。
- 语义是有限去重的**至少一次**：同一事件可能重投，handler 必须把业务写入与 `MarkProcessed` 放同一批；consumer 名必须属于本 principal。租约过期的确认得到确定的 `DeliveryExpired`（HTTP 410），之后会重投同一事件；超过最大尝试次数入死信，`replay` 只放回同一 consumer。
- 读取须有显式 grant（包括读本模块类型）；无权与不存在不可区分。

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
| 远程 SDK（P1，d7c9919；订阅为 P2 d0d8875） | `remote.py`、`remote_errors.py`、`wire.py`、`consumer_wire.py`；共享校验逻辑 `engine.py`（同包） |
| 服务实现（P1 d7c9919；P2/P3 后端 d0d8875） | `services/storage/src/ear_storage/`（`schema`、`repository`、`backend`、`auth`、`service`、`snapshots`、`reader`、`http`、`cli`、`relay`、`consumers`、`consumer_broker`、`management`、`management_input`、`credentials`、`reconfiguration`）；测试 `services/storage/tests/`（Relay：`test_relay*.py`；消费者：`test_consumer*.py`、`test_marker_identity.py`、`test_p2b_mustfix.py`；管理：`test_management*.py`；凭据：`test_credentials*.py`；重配置：`test_reconfiguration.py`） |
| 公共模型与注册表 | `libs/ear_contracts/src/ear_contracts/` |
| 调用记录（P4，e9391dd） | 模型 `libs/ear_contracts/src/ear_contracts/calls.py`；正文策略 `libs/ear_storage_client/src/ear_storage_client/model_body.py`；契约 `contracts/records/llm-call-record.schema.json`；测试 `services/storage/tests/test_llm_calls.py`、`test_model_bodies.py` |
| 历史恢复（P5，本分支） | `services/storage/src/ear_storage/history.py`（授权、范围、切换、`job_status`）、`history_execution.py`（所属方 `claim` / `commit`）、`history_scheduler.py`（供⑤的 `list_reevaluations` / `attach` / `claim_assignment`）；管理输入 `management_input.py`；迁移 `schema.py` `DDL_V11`–`DDL_V14`；SDK `libs/ear_storage_client/src/ear_storage_client/remote.py`；测试 `services/storage/tests/test_history*.py`、`test_recovery_fixes.py` |
| 行为测试 | `libs/ear_storage_client/tests/test_memory.py`；P0a 回归 `test_memory_regressions.py`；P0b `test_p0_sdk_models.py`、`test_p0_memory_dependencies.py`（同目录）；契约模型 `libs/ear_contracts/tests/test_p0_contract_models.py` |

业务模块依赖 `StorageClient`，不要依赖 `MemoryStorage` 的内部状态。PostgreSQL 服务与 HTTP 路由已提交（d7c9919，PR8 未合并），实际路由、表、Relay 与限制见[工程指南](persistent-service-plan.md)；远程持久消费者由服务经 HTTP 代理（P2，d0d8875），业务模块不直接连接 NATS；部署用 NATS 环境由 I 提供。业务代码不要依赖服务内部表。

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

**集合守护（P4，schema revision 9）**：需要“某类型的集合在提交时未变”时，在同一 snapshot 内执行 `Query` 并调用 `type_revision(data_type)`（两次独立的实时调用不构成一致的一对），再在写入批中加入 `AssertTypeRevision(data_type, expected_revision)`。守护只在提交期于批开始与批末原子校验，不产生事件或依赖边；不匹配返回可重试冲突 `collection_changed`，应在新 snapshot 中重查重算。实际业务写入与终止版本使 revision 增加；unchanged、去重、依赖登记刷新、重放与回滚不增加。需要读权限而非所有权，未知类型与无权类型失败相同。守护不扫描历史，规则编辑不会自动重判历史。revision 8 时的旧 snapshot 缺 revision 元数据时 `type_revision` 明确失败，不返回当前计数。验证见 [progress](progress.md)。

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

`subscribe(consumer, handler, event_types=None, from_beginning=False)` 的 consumer 必须为本模块名或以“模块名.”开头。handler 抛异常触发重投，超过次数进入 dead_letters。以上是内存实现；远程订阅的规则见第 0 节，状态见 [progress](progress.md)。

handler 内写入的固定处理方式（见第 2 节示例）：先单独判断 `already_processed`（说明之前已提交，正常 return）；其余情况调用 `result.check()`，把冲突转成异常以触发重投。其他冲突应重算或显式失败，不能默默确认。总线无法发现 handler 忽略了 `write()` 的返回值；与本 consumer 无关的事件可以直接 return。

内存辅助功能：`duplicate_delivery=True`、`max_attempts`、`run_until_idle()`、`dead_letters`、`storage.events`、from_beginning 重放。它们不证明跨进程持久化或外部动作恰好执行一次。`run_until_idle` 中，同一 consumer 同时只处理一个事件：多个 runner 或 handler 内嵌套调用会跳过正在处理的 consumer（不等待），由持有它的 runner 继续投递；超过 `max_deliveries` 时抛出 RuntimeError，未投递的事件留在队列中（P0a 修复，状态见 [progress](progress.md)）。

**重判完成记录（P4，106cd2e）**：完成以①的权威索引验真（请求与所属方），不信任调用方自报；只有登记表显式声明 `reevaluation_result` 能力的类型受完成协议保护，名称带普通后缀不会自动受保护，旧同名类型保持兼容。④现有的入队 marker 只表示已入队，不能当业务完成。调用签名以 `ear_storage_client/completion.py` 为准，规则与验证见 [progress](progress.md)。

**多起因（daa767f，以代码为准）**：`cause` / `root` / `depth` 保持主起因含义；额外来源放在 `additional_causes`，每项（`TracedCause`）只有 `cause`、`root_feedback_id`、`depth` 三个字段，没有 `request_event_id` 或 `trigger_request_event_id`。请求关联在别处：`request_event_id` 位于 Context、语义记录与 `DataChanged`；`ReevaluationRequested.trigger_request_event_id` 指向上游请求。关联须是真实已提交的请求，不能指向自身。处理中新增的来源须先在 Context 预合并，并与 Finish 一致，后续事件才会带上。①逐 origin 复核读权限（含历史、结果、消费者投递与旧消费者代的待确认消息）。完整真实运行 1168 passed / 5 skipped，见 [progress](progress.md)。

**调用记录 `llm_call`（P4，已提交 e9391dd；签名以 `calls.py`、`ops.py` 为准）**：只有⑦网关（`llm_gateway`）能写，用 `PutSystemRecord` 为每个逻辑调用写一次终态（`success` / `failed`）`LLMCallRecord`，envelope id 即 call_id，写后不可替换；系统记录不发事件，不要等它的通知。`caller_module` / `model` 是网关断言，不能当作已认证的调用方或模型身份。带 `request_event_id` 时须指向真实已提交请求，①保存该请求的全部起因，并在每次读取时复核全部起因类型的读权限。按调用方或根反馈查询用现有 `Query("llm_call", where={"caller_module": ..., "root_feedback_id": ...})` 与 `count`。默认只存 metadata：`request_text` / `response_text`（严格字符串，空串也算提供）只在部署方显式开启 `ModelBodyPolicy` 且调用方在范围内时可写（默认关闭、两项累计 UTF-8 上限 64 KiB、`allowed_callers=None` 全部允许、`()` 全部拒绝），写请求本身不能开启；只放调用方显式提供的文本，不得含凭据或 API key。关闭后旧正文仍按类型/起因 ACL 可读，不删除、无 TTL；读取没有逐字段 ACL。已有库须显式 `bootstrap` 才有 `llm_call`。规则与验证见 [progress](progress.md)。

**插件历史恢复（P5 ①侧，本分支已实现，提交状态以提交历史为准；签名以 `remote.py`、`http.py` 为准）**：恢复与补处理只由可信管理员经管理反馈发起（`resume_choice` + `expected_revision`，或 `history_backfill` + `rangeStart` / `rangeEnd` + `input_types`），插件不能自行发起。插件所属方用 `history_job(job_id)`（`GET /v1/history/jobs/{job_id}`）查状态；`claim_history_job(job_id)`（`POST /v1/history/jobs/{job_id}/claim`，无参数）领取一个候选，带 snapshot、租约与 claim token/fence；`commit_history_job(job_id, claim_token, fence, ops, context, *, request_id, outcome="completed", error_code=None)`（`POST /v1/history/jobs/{job_id}/commit`）使业务写入与进度原子提交，租约或 fence 失效即拒绝，须重新领取。候选来自对象当前状态，不是旧事件重放；skip 不改旧结果和依赖、不产生完成。供⑤：`list_history_reevaluations(job_id=None, *, limit=50, cursor=None)`（`POST /v1/history/reevaluations/list`）、`attach_history_request(job_id, item_id, request_event_id)`（`POST /v1/history/jobs/{job_id}/requests`，须为真实已提交请求，复核权限与起因覆盖）；所属方再用 `claim_history_assignment(job_id, *, consumer)`（`POST /v1/history/jobs/{job_id}/assignments/claim`；consumer 须已登记或预留给本 principal）领取分配并自行 Finish，至少一次交接，以完成索引与同批 `MarkProcessed` 判重。完成索引与历史依赖按 `commit_seq` 验新鲜；作业终态后冻结（`history_job_final`）。⑤尚未实际调用这些接口。规则与验证见 [progress](progress.md)。

## 7. 验证与已知边界

```bash
uv run pytest libs/ear_storage_client/tests/test_memory.py libs/ear_storage_client/tests/test_memory_regressions.py
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && uv run python tools/check_docs.py
```

真实 PostgreSQL/NATS 测试需同时设 `EAR_RUN_POSTGRES_TESTS=1` 与 `EAR_RUN_NATS_TESTS=1`，夹具只起本机临时 Docker 容器（loopback 随机端口）；设置 `EAR_POSTGRES_TEST_DSN` 或 `EAR_NATS_TEST_DSN` 会被拒绝，不要指向任何外部库。

当前实现以排序键的 json.dumps 比较 payload，不是 RFC 8785/JCS 实现。修改比较算法时注意数值、数组、字符串及事件时间的一致性。

已知静态审查关注项（run_until_idle 在检查投递上限前出队、并发 runner 的 consumer 串行边界、空时间查询区间、系统记录更新路径的时间校验）已在本分支补回归测试（`test_memory_regressions.py`）并修复，提交与合并状态以分支历史和 PR 为准，见 [progress](progress.md)。正式后端、故障测试与已采纳语义的实现差距也见[进度](progress.md)。
