---
audience: ai
doc-type: reference
generated-by: Claude (AI)
generated-at: 2026-11-11
based-on: 设计 v1.2 第 6 节与信号补充 §3.2、§4（accepted，仅表示目标已采纳）；工作分解 v1.2 ③节；integration/mvp-runnable 工作区 services/attribution 的实际源码与测试（基线 c5c31f8，未提交，本文作者读代码核对）；任务记录 /tmp/ear-mvp-attribution-state.json（非仓库文件，写于 2026-11-11T19:10Z）；2026-11-12 增补依据：transactions.py、service.py 源码，services/storage/tests/test_attribution_runtime.py 与 tests/e2e/test_core_memory_loop.py 源码（均读源码核对），仓库外日志 /tmp/ear-mvp-acceptance.{sh,log,exit}、/tmp/ear-mvp-attribution-{recovery-red,real-final}.log，以及本次对相关文件的定向复跑；2026-11-12 journal 硬化增补依据：secure_files.py、transactions.py、tests/test_journal_safety.py 源码，仓库外记录 /tmp/ear-mvp-precommit-review.json、/tmp/ear-mvp-journal-fix.json、/tmp/ear-mvp-journal-*.{log,exit} 与 /tmp/ear-mvp-final-local.log（非仓库文件，作者读取并核对哈希与时间），以及作者的 pytest 复跑（见跑通计划第 2.3 节）；来源分支 origin/attribution/initial-implementation 617fbc5（git show 只读）；2026-11-12 第二批增补依据：create_place.py、feedback.py 与 ⑥ 的 attribution_results.py 源码（只读核对），仓库外摘要 /tmp/ear-mvp-api-place.json（此前读取）与 /tmp/ear-mvp-eight-steps.json（作者读取并核对退出码文件与 junit 用例名），测试结果均未复跑；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核，本文不新增授权记录，也不增加用户决定；未填写或改动任何 reviewed 字段
---

# ③ 归属：实现说明（首批切片）

> 记录工作区里**已有的代码**，不是设计，也不是完成报告。目标行为以[设计 v1.2 第 6 节](../../../docs/design/design-v1.2.md)和[信号补充 §3.2、§4](../../../docs/design/signals-supplement.md)为准；职责与验收见[工作分解 v1.2 ③节](../../../docs/design/work-breakdown-v1.2.md)。人读入口见[模块 README](../README.md)，任务与门槛见[跑通计划](../../../ai-docs/plans/mvp-runnable.md)。本文里的 payload 形状、错误码、阈值参数都是本切片的**工程选择**，未经负责人确认，不是公共契约。

## 1. 范围与状态

- 首批切片：匹配、未命名人物与未知地点、五种反馈、重判完成协议、用户确认保护、依赖登记、远程写入的持久重放。代码已在 79e8dc3（PR 16 为 OPEN，其 CI 成功只属于 79e8dc3）。**第二批（工作区，未提交、未提 PR、未部署）**：`create_place`（第 6 节），以及 ⑥ 对 `attribution_feedback_result` 的只读投影（[⑥入口](../../api/README.md)）。下面带“未提交”“工作区”的旧句子是各自时点的记录，现状以带“第二批”的条目为准。
- 不是完整的 ③：`reattribute_range` 等未做（第 12 节）；**在 `DEFAULT_REGISTRY` 上不能运行**（第 9 节；第二批里 ① 的 `core_mvp_registry()` 与 `bootstrap-core` 只对显式迁移过的空库提供这些类型和能力，旧库不自动迁移）；统一 worker 宿主 `tools/mvp_worker.py` 在工作区（需显式阈值，见[交接第 4 节](../../../ai-docs/plans/mvp-runtime-handoff.md)），没有 Compose 部署、没有真实数据运行。
- 代码与任务记录的一致性：本文作者把记录里列出的 15 个文件（`pyproject.toml`、`src/ear_attribution/*.py` 9 个、`tests/*.py` 5 个）的 sha256 与工作区当前文件逐一比对，全部一致，说明代码自 2026-11-11T19:10Z 的任务记录后没有再变。这只说明代码没变，**不代表测试在当前环境里重新通过**。（2026-11-11 晚的核对；此后 `service.py`、`transactions.py` 又有改动，见下一条。）
- 2026-11-12 更新：重启恢复**不再仅靠总线重投**（`subscribe()` 先 `recover_pending()`，第 3、14 节）；真实 PostgreSQL/NATS/① HTTP 的临时容器运行已有 5 项（第 11 节），但登记表仍是夹具里显式构造的、身份仍由 `AuthOperator` 签发，`DEFAULT_REGISTRY` 上仍不能运行 ③；仍未提交、未提 PR、未部署。
- 2026-11-12 更新（journal 安全硬化，第 15 节）：journal 目录经可写祖先被替换会让 SQLite 打开新路径的问题已修复（仅 Linux；0700/0600 与祖先身份要求；已有 WAL 拒绝，不转换、不删除；验证失败不发送）。新增 `secure_files.py`、`tests/test_journal_safety.py`，修改 `transactions.py`，模块单元现为 143 项。新增的两个文件**未跟踪**，`transactions.py` 的修改未暂存；提交时须一并纳入。四开关整体验收的 2485 passed 是此修复之前的基线，不覆盖这三个文件。

## 2. 代码导航

路径相对 `services/attribution/`。

| 文件 | 内容 |
|---|---|
| `src/ear_attribution/__init__.py` | 导出 `AttributionService`、`BusinessPlanner`、`CommitJournal`、`MatchConfig`、`Planner` |
| `service.py` | `AttributionService`（事件路由、快照内规划、原子提交；`subscribe()` 先启动恢复再订阅）、`Planner` 协议、`business()` 工厂（先检查登记表） |
| `planning.py` | `BusinessPlanner.new_record / feedback / reevaluation`；`_match` 负责匹配结果、未命名人物与未知地点的写入与依赖 |
| `matching.py` | `MatchConfig`（显式阈值）、`vector`、`coordinates`、`distance_m`、`match`、`reference_dependency` |
| `payloads.py` | `PAIRS`、`DomainError`、`raw_subject`、`entity`、`references`、`current_attribution`、`read_attribution`、`attribution_payload` |
| `state.py` | `STATE_ID`、`derived_id`、`Fence`、`read_fence`：单例候选集合栅栏 |
| `feedback.py` | `SUPPORTED`、`feedback_result_id`、`plan_feedback`：五种反馈及持久回执 |
| `completion.py` | `finish()`：构造 `FinishReevaluation` |
| `transactions.py` | `CommitJournal`（私有 SQLite 日志；`pending`、`pending_ids`、`prepare`、`complete`；文件与目录安全委托给 `JournalFiles`，第 15 节）、`Transactions`（读-规划-提交循环与有界重试；`recover_pending` 启动恢复；每次远程写入前 `journal.validate()`） |
| `secure_files.py` | 2026-11-12 新增，**未跟踪**：`JournalFiles`（祖先与身份校验、目录与 DB fd 钉住、`worker.lock` 与 DB 双 `flock`、`/proc/self/fd` 的 SQLite URI、WAL 与旁文件拒绝），仅 Linux，第 15 节 |
| `tests/support.py` | 测试用登记表（`registry()`）与夹具；**核心登记表没有这些类型** |
| `tests/test_service.py` | 来源分支的 11 项框架测试（部分已改为真实持久化的请求与 Finish） |
| `tests/test_business.py`、`test_business_edges.py`、`test_recovery_and_integration.py` | 业务、边界、恢复与跨模块形状 |
| `tests/test_journal_safety.py` | 2026-11-12 新增，**未跟踪**，76 项：journal 的祖先、权限、旁文件、WAL、替换与初始化失败等合成文件系统攻击，不用私有数据与外部服务；覆盖清单见第 15 节 |
| `services/storage/tests/test_attribution_runtime.py`（仓库根相对路径，模块外） | 真实 PostgreSQL、NATS、① HTTP 的 5 项，需两个开关；含 journal 启动恢复 2 项 |
| `tests/e2e/test_core_memory_loop.py`（仓库根相对路径，模块外） | ②③④⑤ 真实类在 `MemoryStorage` 上的纠正闭环 2 项，**不是八步验收** |

## 3. 事件处理流程

`AttributionService.handle(event)`（`service.py`）：

| 事件 | 路由 |
|---|---|
| `DataChanged` 且 `operation == "created"`、类型为 `voiceprint` 或 `gps_point` | `new_record`，上下文 `Context.of(event)` |
| `DataChanged`、`created`、类型 `feedback` | `feedback`，上下文 `Context.feedback(反馈 id)`（根源是该反馈自身，depth 0） |
| `ReevaluationRequested` 且 `target_type == "attribution"` | `reevaluation`，上下文 `Context.of(event)`（原样带 root、depth、`request_event_id`、额外起因） |
| 其余 | 忽略，不记已处理 |

每次处理都在一个快照里完成：先 `is_processed("attribution", event_id)`，再按事件里的 id 读当前记录（不存在则抛 `LookupError`，让总线重试或进死信，不静默确认）；原始数据事件要求当前记录类型与事件类型一致；重判目标必须是未被整体替代的归属，否则按第 7 节直接完成为"不适用"。规划返回的操作序列后面再追加 `MarkProcessed`，与业务写入同批原子提交。重判规划如果没有包含 `request_event_id` 等于本事件的 `FinishReevaluation`，服务抛错，**不允许只写去重标记冒充完成**。

`Transactions.run` 在可重试冲突时重读重算，默认最多 5 次（`attempts`）。规划函数会被重复调用，因此只读、不写库、不发外部请求。规划返回 `None` 表示事件与本模块无关（例如其他模块的反馈）；返回空序列表示已处理但无写入，同样记已处理。

**启动恢复（2026-11-12，未提交）**：`AttributionService.subscribe()` 在注册订阅之前先调用 `Transactions.recover_pending(limit=1000)`，**恢复不再仅靠总线重投**。对私有 journal 里每条未决项（按插入顺序，`CommitJournal.pending_ids`），用保存的 `request_id`、操作和上下文调用 `write(..., request_id=...)`，**不重新规划**；调用返回后删除该条（返回冲突结果也删除，之后靠事件重投重新规划；这一点按源码，没有单独测试）；调用抛异常（结果未知、鉴权、传输）则保留条目，异常传出 `subscribe()`，不订阅。`limit` 须是 1–10000 的整数，未决项超过 `limit` 抛 `ValueError("journal recovery bound exceeded; explicit recovery required")`，**目前没有提供显式恢复入口**。返回值是已处理的回执数，不是成功的业务数；没有 journal 时返回 0。原因：提交已成功而回执丢失时，服务端已有 `MarkProcessed`，重投的事件不会再进入处理函数，只靠重投清不掉这条日志。只有 `services/storage/tests/test_attribution_runtime.py` 的 `test_subscription_restart_resolves_pending_without_handler_redelivery`（回执在提交前丢失、提交后丢失两种）覆盖这条路径；它在进程内重开 journal 并换新的 `RemoteStorageClient`，**不是 OS 强杀**，模块单元测试不覆盖。journal 文件的安全约束，以及每次远程写入前的重新验证（验证失败则不发送），见第 15 节。

## 4. 匹配

- **已是参考者优先**：数据 id 已出现在某个人物或地点的 `references` 里，直接归该对象，不看相似度。一个 id 出现在两个对象的参考里，抛 `reference_owner_conflict`。
- **声纹**：`payload.model_version`（非空字符串）和 `payload.embedding.values/shape`（一维、有限、非零）。向量先按最大绝对值缩放再归一化，防止溢出。只与**同一 `model_version`、同一维度**的参考向量比余弦；`>= cosine_threshold` 才算候选，取相似度最高者，平局按对象 id 稳定排序。
- **GPS**：`payload.latitude/longitude`（度，WGS84）。用 haversine 球面距离（平均地球半径 6371008.8 m），`<= gps_threshold_m` 才算候选。这是球面近似，不是椭球大地线。
- **阈值必须显式配置**：`MatchConfig(cosine_threshold, gps_threshold_m)` 没有默认值，非有限数或越界会在构造时报错。阈值没有校准，D2 认人准确率没有验证。
- **认不出时**：声纹新建未命名人物（`name: null`、`references: [数据 id]`、单调递增的 `display_number`），并同批写归属；GPS 归全局唯一的未知地点（无参考、`is_unknown: true`，不参与匹配）。自动匹配本身不增加参考；只有新建未命名人物时把这条声纹设为其首个参考。
- 输入畸形（向量非法、坐标越界等）抛 `DomainError`，**不当作"未知人物"成功处理**。

## 5. 本切片使用的 payload 形状（候选，未进入核心登记）

| 对象 | 字段 |
|---|---|
| `attribution` | `subject_type`、`subject_id`、`target_type`、`target_id`、`user_protected`（布尔；缺失表示旧数据）、`protection_feedback_id`；更新时保留未知扩展字段 |
| `person` | `name`、`display_number`、`references` |
| `place` | `name`、`references`、`is_unknown` |
| `attribution_state`（系统记录，单例） | `schema_version=1`、`unknown_place_id`、`next_person_number`、`reference_revision` |
| `attribution_feedback_result`（系统记录） | `schema_version=1`、`feedback_id`、`command_kind`、`status`（`succeeded`/`rejected`）、`error_code`、`completed_at`，以及视情况的 `subject_id`、`previous_target_id`、`new_target_id`、`attribution_id`、`before_version`、`after_version` |

`attribution_state` 的 id 由 `derived_id("state:v1", …)` 固定推出（带锚点时间戳的 UUIDv7，不是 UUIDv5）；回执 id 由反馈 id 按 `feedback_result:v1` 域分隔推出，重复投递同一反馈得到同一回执 id。

## 6. 反馈命令

模块局部格式，字段来自 ⑥ 的草案模型（`services/api/src/ear_api/models.py` 的 `PENDING_FIELDS`）；这里读取的是①里**已保存**的反馈，因此可选字段的 `null` 默认值是合法的。反馈 `payload.module != "attribution"` 的直接忽略。

| kind | 字段 | 行为 |
|---|---|---|
| `correct_attribution` | `subject_id`、`previous_target_id`、`new_target_id`，可选 `expected_version` | 从原对象参考移除该数据、加入新对象参考、更新归属并设 `user_protected=true`；`previous_target_id` 与当前归属不符则拒绝 |
| `confirm_attribution` | `subject_id`、`target_id`，可选 `expected_version` | 加入当前对象参考，设保护；目标须等于当前归属；重复确认不产生新版本 |
| `add_reference` / `remove_reference` | `subject_id`、`target_id`，可选 `rejudge_history`（布尔，默认 false）、`expected_version` | 只改对象参考；不重算归属、不刷新旧匹配依赖；`rejudge_history` 为 false 时参考修改带 `skip_history` |
| `rename` | `target_id`、`name`（非空、≤256 字符），可选 `expected_version` | 只改名字，走正常传播；未知地点不可改名 |
| `create_place`（第二批，工作区） | `name`（非空、≤256 字符）、`gps_ids`（1–100 个、无重复、UUIDv7，须为 `gps_point` 原始数据且坐标有效）；`expected_version` 须为空（非空返回 `invalid_expected_version`） | 在**一个受围栏的批次**里：对涉及的旧地点逐个 `Update` 移除这些参考（先移除、再建新参考所有者，存储强制互斥），新建一个 `place`（`references` 为所选 GPS，`is_unknown=false`），逐个把所选 GPS 的归属改指向它（沿用已有归属对象的 ID，没有则新建）并登记依赖——只依赖新地点的 `references`、`is_unknown`，**不继承旧候选或 `name` 依赖**。所选数据的当前归属受保护返回 `protected_attribution_conflict`、保护字段缺失返回 `legacy_protection_unknown`；同一个所选 GPS 同时出现在多个地点的参考里返回 `reference_owner_conflict`，地点数据畸形返回 `invalid_candidate`、`invalid_unknown_place`；`name`、`gps_ids` 畸形分别是 `invalid_name`、`invalid_gps_ids`、`duplicate_gps_ids`、`subject_type_mismatch`。成功回执的元数据为 `new_target_id`、`subject_ids`、`attribution_ids`（顺序与 `gps_ids` 一致）。围栏被推进，已规划的自动候选匹配随之失效。代码在 `create_place.py`，由 `feedback.py` 分发 |
| `reattribute_range` | — | **未实现**：写一条 `rejected`、`error_code=unsupported_command` 的回执，不冒充成功，不排后台任务 |

- 纠正与确认伴随的参考修改带 `skip_history=true`（不触发对历史的自动扫描），归属本身的变化仍可传播给下游。
- 保护：纠正或确认写 `user_protected=true`，并保留**首次**的 `protection_feedback_id`；保护**不因后来移除参考而消失**，见第 13 节。
- 回执与业务写入、`MarkProcessed` 同批原子提交。失败的命令也写 `rejected` 回执（含 `error_code`），不会无限重试。主要错误码：`invalid_command`、`invalid_expected_version`、`expected_version_conflict`、`attribution_pending`（该数据还没有归属）、`previous_target_conflict`、`attribution_target_conflict`、`reference_owner_conflict`、`reference_type_mismatch`、`target_unavailable`、`invalid_name`、`unknown_place_cannot_be_renamed`、`unknown_place_cannot_be_reference_target`、`invalid_history_choice`、`unsupported_command`；完整集合以 `DomainError(` 的出现处为准。
- `expected_version` 守护的是命令实际修改的对象（纠正/确认守护当前归属，参考维护与改名守护目标对象），不是巧合的原始数据版本。

## 7. 重判与完成协议

`BusinessPlanner.reevaluation` 对每个请求都提交 `FinishReevaluation`（沿用①已有的结果协议，`completed_at` 取现在与请求时间的较大者，额外起因原样带回）：

| 情况 | 结果 |
|---|---|
| 目标归属已被整体替代 | `not_applicable`，`reason_code=target_terminated`，`disposition=terminated` |
| 旧数据没有 `user_protected` 字段 | `not_applicable`，`reason_code=legacy_protection_unknown`，`disposition=kept_protected`；**不从当前参考关系推断保护，不自动迁移** |
| 受保护，且当前目标仍可用 | 不运行匹配；`Update` 原 payload（无新版本）、依赖刷新为当前目标，`unchanged` |
| 受保护，但目标对象已不可用 | `not_applicable`，`protected_target_unavailable`，`disposition=kept_protected` |
| 未保护 | 重新匹配；目标变了则 `changed`，否则 `unchanged`；依赖按实际读到的候选重新登记 |
| 业务输入畸形（`DomainError`） | `failed`，`error_code` 为有界错误码；传输/授权错误则抛出，不写成业务失败 |

`unchanged` 同样刷新完整依赖、写完成结果与 `MarkProcessed`，不新增业务版本，不发业务事件。

## 8. 依赖登记与栅栏

- **依赖按实际读到的输入登记**：匹配时读过的每个候选（包括没有参考、不匹配、被排除的候选）都登记为依赖，字段为 `("references",)`，地点另加 `is_unknown`；**不登记 `name`**，所以改名不会使只靠参考匹配的归属过期。用户纠正/确认的归属只依赖所选目标的 `references`（以及提交后的版本）。
- **候选集合栅栏**：`attribution_state` 单例带版本（`expected_revision`）。凡是新建候选对象、修改参考或分配 `display_number` 的批次都要同批写栅栏；并发的另一个批次会因栅栏版本冲突而重读重算。`reference_revision` 只在参考关系真的变化时加一，改名不加。仅维护参考的反馈对 `attribution` 类型做只读的 `AssertTypeRevision`，不修改该类型。
- **未知地点单例**：首次需要时与栅栏同批创建，去重键 `attribution:unknown_place:v1`；单例创建竞争（`id_exists`）重读而不绕过。已存在栅栏但对象缺失、或未知地点带参考，按 `invalid_attribution_state` / `invalid_unknown_place` 拒绝，不修复。

## 9. 核心登记表与根配置要求

**核心登记表没有本切片需要的类型和能力，也没有被修改。** 本文作者在工作区用只读脚本核对：`DEFAULT_REGISTRY.get("attribution_state")` 与 `get("attribution_feedback_result")` 都抛 `UnknownDataTypeError`；`attribution` 的 `supports_reevaluation` 与 `requires_dependencies` 都是 false；在默认 `MemoryStorage` 上调用 `AttributionService.business(...)` 同样以 `UnknownDataTypeError`（"数据类型 'attribution_state' 没有登记"）失败。不在运行时补登记。因此**不能说 ③ 能在 `DEFAULT_REGISTRY` 或任何真实 ① 上运行**。

需要的核心变更（未做，须 ① 负责人与 I 审核，见[根 README 的公共约定](../../../README.md)）：

| 变更 | 内容 |
|---|---|
| 新增系统记录类型 `attribution_state` | 写者 `attribution`；单例候选集合状态；不发事件 |
| 新增系统记录类型 `attribution_feedback_result` | 写者 `attribution`；每条反馈一个回执；不同于 `reevaluation_result` |
| 修改 `attribution` 的能力 | `requires_dependencies=true`、`supports_reevaluation=true` |
| 保持不变 | `attribution.unique_fields=[subject_id]`、`time_from=subject_id`；`person`/`place` 的 `exclusive_members=[references]`；`attribution_completion` 仍由 `attribution` 写、`reevaluation_result=true` |

登记表见 [`contracts/registry/data-types.json`](../../../contracts/registry/data-types.json)；JSON 与 Python 登记须同步，并需要显式迁移与受控的 worker 身份、读取/订阅/consumer 授权。两个 payload 形状是本地候选，不是已审定的 Schema。

**根工作区接入**：模块 `pyproject.toml` 自带 `tool.pyright` 配置，可用 `-p services/attribution` 单独校验。本文作者于 2026-11-11 晚（UTC）看到根 `pyproject.toml` 的未提交改动已把 `services/attribution` 加入 `tool.uv.workspace.members`、`dev` 依赖组、`tool.uv.sources`、pytest `testpaths` 与 pyright `include`，`uv.lock` 也有未提交改动；这些由协调者维护，本文作者没有改动它们，也没有运行 `uv sync`，不为其正确性背书。**2026-11-12 更新**：上述根配置与 `uv.lock` 改动现已在索引（已暂存）里；仓库外整体验收里 `uv lock --check --offline`、ruff、format、pyright 通过（第 11 节）。本文作者仍没有改这些文件，也没有运行 `uv sync`。

## 10. 来源（provenance）

复用 `origin/attribution/initial-implementation`（617fbc5，作者 yixiang-fan，基于 48d3c51，未合入 dev）的事件处理框架：用 `git show` 取 `service.py`、`__init__.py`、`pyproject.toml`、`test_service.py`，在当前基线上原地改造；**没有 checkout 或 merge，没有改写任何已推送历史**。该分支的 `ai-docs/development.md` 是它的交接文档，P1–P7 在其中全部"待确认"。`.github/CODEOWNERS` 里 `services/attribution/` 的负责人是 yixiang-fan。

| 保留 | 改动 |
|---|---|
| 可注入 `Planner` 协议；按当前状态快照路由；`attribution` 身份检查；业务写入与去重标记原子提交；有界乐观重试；`Context.of` 与反馈根源；JSON 元数据日志；11 项原框架测试（2 项改为真实持久化的请求与 Finish） | 重判一律以 `Finish`+标记收尾，重判规划不能只写标记；显式的 `business()` 工厂；远程写入的可选持久日志；单例/回执的 `id_exists` 竞争改为重读而不是绕过 |

2026-11-12 补充：`Transactions` 的读-规划-提交循环与有界乐观重试沿用来源框架（代码注释亦如此写）；持久日志（`CommitJournal`）及其后来的安全硬化（`secure_files.py`、`tests/test_journal_safety.py`，第 15 节）是本切片新增，**不来自 617fbc5 分支**，也没有改变来源框架的业务语义。

## 11. 测试与验证记录

下表区分**谁运行的**。2026-11-11 晚写下前五行时，本文作者没有运行任何测试，只做了收集计数；"任务记录"来自 `/tmp/ear-mvp-attribution-state.json`，不是仓库文件。2026-11-12 追加的行写明来源，其中“作者运行”的是文档任务实际执行的命令，其余是读日志。

| 项目 | 结果 | 来源 |
|---|---|---|
| 模块本地 pytest（`PYTHONPATH=services/attribution/src .venv/bin/python -m pytest services/attribution/tests -q`） | 67 项通过，退出码 0 | 任务记录（2026-11-11T19:10Z），**文档作者未复跑** |
| 收集计数 | 67 项（`test_service.py` 11 项） | 文档作者 `pytest --collect-only`（只收集，未执行），与记录一致 |
| 模块本地 ruff、format、pyright（`-p services/attribution`） | 均通过，pyright 0 errors、0 warnings | 任务记录，未复跑 |
| 根目录检查（当时） | ruff 1 项、pyright 3 项错误、pytest 8 项失败，**均在并行修改中的 ④ 文件与测试里**；当时根 `testpaths` 不含本模块 | 任务记录，历史结果，**不是当前状态**，本文不据此宣称全绿或全红 |
| 全仓统一检查 | （2026-11-11 晚）待协调者最终统一检查后补记；**2026-11-12 已有整体验收，见下面各行** | 见下 |
| 整体验收（仓库外 `/tmp/ear-mvp-acceptance.sh`，退出码 0；启动 2026-11-11T23:50:55Z，结束 2026-11-12T00:12:56Z） | 静态：ruff、format（209 个文件）、pyright 0 errors、`uv lock --check --offline`、`check_docs.py --base origin/dev`（43 个 Markdown）、`git diff --check`、node 37 项均通过；三个临时镜像构建成功；四个门控开关全开：`2485 passed, 11 skipped, 2 warnings, 1287.40s`。当时 `test_attribution_runtime.py` 为 3 项 | 日志读取，作者未重放。**不覆盖** `service.py`、`transactions.py`、`test_attribution_runtime.py`、`tests/e2e/test_core_memory_loop.py` 在 23:57–23:58Z 的修改（修改时间晚于脚本启动，pytest 会话起点约 23:51:29Z；按 pytest 在会话开始时导入模块推断，未另行证明） |
| 修改后的真实存储运行（任务日志 `/tmp/ear-mvp-attribution-recovery-red.log`、`/tmp/ear-mvp-attribution-real-final.log`） | 23:58:09Z 新增的启动恢复 2 项实现前为红（2 failed、3 deselected）；23:59:11Z `test_attribution_runtime.py` 5 passed，31.15s | 任务日志，作者读取 |
| 定向复核（2026-11-12T00:2xZ–00:3xZ） | 4 个后改文件 `ruff check`、`ruff format --check`、`pyright` 通过；`tests/e2e/test_core_memory_loop.py` 加 `services/attribution/tests` 共 69 passed；`test_attribution_runtime.py` 在 `EAR_RUN_POSTGRES_TESTS=1`、`EAR_RUN_NATS_TESTS=1` 下 5 passed，29.61s | **作者运行**（`-p no:cacheprovider`）。输入合成｜模型：真实存储运行不涉及，e2e 为确定性合成替身｜外部不适用｜平台：真实存储运行为临时 PostgreSQL/NATS/① HTTP，单元与 e2e 为内存 |
| journal 硬化后的验证（2026-11-12，UTC；详见第 15 节与[跑通计划第 2.3 节](../../../ai-docs/plans/mvp-runnable.md)） | ③ 模块单元 **143 passed**（67 + 新增 76，0.93 s）；`test_attribution_runtime.py`（两个开关）**5 passed**（29.90 s）；默认全量（未设开关）**1921 passed、653 skipped、2 warnings**（33.55 s）；ruff、`ruff format --check`（211 个文件）、pyright（0 errors）通过 | 修复任务日志 `/tmp/ear-mvp-journal-{attribution,runtime,default,ruff,format,pyright}.log`，退出码均 0，晚于三个文件的最后修改，三个文件的 sha256 与记录一致；作者读日志，并**复跑**了 143 项单元（143 passed，0.90 s）、真实 PG/NATS 5 项（5 passed，29.81 s）与默认全量（1921 passed、653 skipped、2 warnings，33.42 s），均在 2026-11-12T01:08Z–01:15Z；核对 `--collect-only`：单元 143（新文件 76）、真实存储运行文件 5；静态检查（ruff、format、pyright）未复跑。输入合成｜模型不适用｜外部不适用｜平台：journal 单元在本机真实 Linux 文件系统与 SQLite 上，远程写入为替身；5 项运行为临时 PostgreSQL/NATS/① HTTP |
| 修复前后对比（2026-11-12） | 新测试对旧 `transactions.py`：2 failed（0777 共享祖先；`sqlite3.connect` 时目录替换）；修复后同文件 76 passed | `/tmp/ear-mvp-journal-red.log`、`/tmp/ear-mvp-journal-safety-first.log`，作者读日志 |
| 修复前基线（**不是修复后的验收**） | 四开关整体验收 2485 passed、11 skipped、2 warnings；修复前默认全量 1845 passed、653 skipped、2 warnings（`/tmp/ear-mvp-final-local.log`，审查记录观察） | 上面“整体验收”行。1921 − 1845 = 76，与新增测试数一致；两组开关不同，2485/11 与 1921/653 不能相加、相减或互相替代；修复后没有重跑四开关全量 |

测试环境（模块本地 67 项）：全部使用合成声纹与 GPS；存储是内存实现 `MemoryStorage`。覆盖的跨模块形状包括：② 的 `capture_records` 合成 Evidence/Embedding、⑥ 的 `FeedbackInput.normalized_command`、④ 的 `segmentation.inputs` 新旧字段、`MemoryStorage` 的约束与 Finish 与跨起因协议、两个 worker 线程共享同一 `MemoryStorage`、真实 `RemoteStorageClient` 编解码配合 `MockTransport` 与内存回执测试台（模拟提交前/后丢失响应）。

**没有验证（2026-11-12 更新）**：2026-11-11 晚列为未验证的真实 PostgreSQL/NATS 与运行中的 ① HTTP 服务，现在有 `test_attribution_runtime.py` 5 项在测试夹具起的临时容器上的运行（回环、合成数据、夹具里显式构造的登记表，不是部署，也不是 `DEFAULT_REGISTRY`）。其中权限只覆盖“同名身份不能订阅”这一项 `PermissionDenied`，撤权没有覆盖。**仍没有**：真实声纹、GPS、人名；真实模型；D2 认人准确率；阈值校准；经 HTTP/PostgreSQL 的 ③→⑤→④ 全链路传播（e2e 是内存）；`DEFAULT_REGISTRY` 与正式身份签发下的运行；部署；对 worker 或 ① 的 OS 强杀恢复（journal 恢复测试是进程内重开）。

**没有验证（2026-11-11 晚原写法，保留为历史，已部分被上面取代）**：真实 PostgreSQL/NATS；运行中的 ① HTTP 服务及其权限、撤权；真实声纹、GPS、人名；真实模型；D2 认人准确率；阈值校准；全链路 ③→⑤→④ 传播。"远程"测试是假传输加内存台，**不是真实服务**。

## 12. 未实现

- 按时间范围重新归属 `reattribute_range`（含进度持久化的批量处理）。`create_place` 已在第二批（工作区，未提交）实现，见第 6 节。
- 空的未命名人物整体替代、"#N 还有 M 段声音，可能也是 X"提示数据。
- 命令行或 worker 的轮询启动配置、经授权的初始缺失归属补扫、运行健康与死信处理流程、日志待重放项的枚举与清理（2026-11-11 晚写“目前只靠总线重投”；现在 `subscribe()` 启动时会先重放未决回执，但仍没有枚举、超出 1000 条时的显式恢复与清理入口）。
- 核心登记、Schema、迁移，以及可信 worker 身份和读取/订阅/consumer 授权（第 9 节）。
- API 反馈状态投影：第二批里 ⑥ 经 `attribution_feedback_result` 只读投影 ③ 的回执（没有回执仍是 `pending`；回执畸形或有歧义时 ⑥ 返回 503，见[⑥入口第 3.4 节](../../api/README.md)）；`dependency` 命令仍无消费方。
- ③→⑤→④ 完整传播、持久化 PostgreSQL/NATS 与真实 HTTP 的权限/撤权测试；部署；真实音频；D2。
- 2026-11-12：journal 的 WAL 受控恢复或迁移工具、既有 journal 的权限修复或迁移工具；journal 硬化只有 Linux 实现；没有跨 UID 攻击进程、worker 强杀、断电与 NFS 持久性的测试（第 15 节）。

## 13. 实现选择与待确认项

来源分支交接文档的 P1–P7 仍是"待确认"。本切片为写出可测试代码对 P1–P6 做了具体选择，**选择被实现不等于被采纳**，由负责人逐项确认。

| 编号 | 本切片的选择 | 状态 |
|---|---|---|
| P1 | 第 5 节的 payload 形状，新增 `user_protected`、`protection_feedback_id` 以及两个系统记录类型 | 待确认；涉及 `contracts/` 须 ① 负责人与 I 审核 |
| P2 | 声纹向量内联在 `payload.embedding`（`values`、`shape`）与 `model_version`；GPS 为 `latitude`、`longitude` 度；阈值显式配置 | 待确认，须与 ② 对齐；未校准 |
| P3 | 第 6 节的命令字段，沿用 ⑥ 草案模型 | 待 ⑥ 与 ③ 确认；⑥ 侧仍记 `pending_contract` |
| P4 | 未知地点 id 不是常量，而是首次创建时生成并记录在 `attribution_state.unknown_place_id`；未命名人物编号 `display_number` 由同一栅栏分配 | 待确认 |
| P5 | 见下 | 见下 |
| P6 | 依赖按对象版本加 `fields` 登记，不登记 `name`；完成走现有 `FinishReevaluation`；无变化也提交 | 待确认；`fields` 的接口位置设计仍待契约同步 |
| P7 | 无启动/补扫/健康入口；② 之外没有正式身份签发 | 未做 |

**P5 用户确认保护。** 原交接文档把"移除参考后是否仍受保护"列为待确认。已采纳设计已经回答了这一点：[设计 v1.2 §6.2](../../../docs/design/design-v1.2.md)规定用户纠正或确认过的数据，**任何重新归属均不得改变其归属结果**，上游删除或整体替代时用户确认的归属保护也不被推翻。因此：

- 本切片的保护在移除参考后仍然存在，符合已采纳设计；**它不是待决的产品选择，也没有"解除保护"的配置开关或命令**，也不应在跑通计划里作为待决项列出。
- 仍属于工程选择、待确认的只有形状与兼容：保护用 `user_protected` 与 `protection_feedback_id` 两个字段持久化（上面 P1）；旧数据缺字段时一律按"保留并标记 `legacy_protection_unknown`"处理，不自动迁移（第 7 节）；用户随后明确的纠正仍然生效。
- 本切片没有任何"取消保护"的行为。若产品之后要增加这类行为，那是新增的产品语义，须另走提案，不属于本切片，也不阻塞跑通。

## 14. 运行约束

- **所有 ③ 的候选创建、参考修改、归属终止路径必须使用同一个栅栏。** 演示或其他以 `attribution` 身份写入的代码会绕过这个模块内栅栏，生产环境不能在未协调的情况下启用它们。
- 改名不改 `reference_revision`；仅维护参考的反馈守护 `attribution` 的类型版本，且有意不刷新旧匹配依赖。
- 已有的栅栏或未知地点的采纳、迁移不是自动的；缺栅栏但保留的未知地点去重键已被占用时保持阻塞，不绕过。
- 持久日志是**单个 POSIX 进程**、一个私有目录加一个部署命名空间，不是多机协调；重启必须复用同一目录；`subscribe()` 先重放未决回执，被释放的事件再靠总线重投重新规划（第 3 节）；未决项超过 1000 条会使 `subscribe()` 报错，须显式恢复，目前没有对应入口。远程客户端没有日志时构造即被拒绝。日志里待重放的操作可含业务名称和 id，必须放在私有存储，不含凭据与原始向量；目录 0700、文件 0600 与祖先身份要求自 2026-11-12 起由 `secure_files.py` 强制检查（仅 Linux；不符即拒绝，不自动修复；已有 WAL 拒绝，不转换、不删除，第 15 节）。
- GPS 距离是球面近似；阈值与 D2 都未验证。
- 畸形的来源或参考输入一律失败关闭，不当成未知人物；`unsupported_command` 是显式拒绝，不是功能完成。

## 15. journal 文件安全（2026-11-12，工作区未提交；仅 Linux）

> 代码：`src/ear_attribution/secure_files.py`（`JournalFiles`）与 `transactions.py` 的 `CommitJournal`、`Transactions`；测试：`tests/test_journal_safety.py`（76 项）。本节记录**实际行为**，不是设计；这些要求是本模块的工程选择，不涉及 `contracts/`。仓库外依据：`/tmp/ear-mvp-precommit-review.json`（提交前审查，2026-11-12T00:50Z）与 `/tmp/ear-mvp-journal-fix.json`（修复，01:03Z，状态 `scoped_fix_complete_tests_passed_not_committed`）。验证数字见第 11 节。

### 15.1 起因与边界

审查报告一项 P2：journal 目录可经可写的非 sticky 祖先被改名替换，`sqlite3.connect` 随即打开新路径，而 `worker.lock` 仍锁着已脱离的旧目录。复现脚本在仓库外；观察到目录外被创建了权限 0644 的库、写入 1 条待重放项、锁已脱离。前置条件是不可信方能改名该目录，整条祖先链可信时未复现。这是针对本机其他用户与共享可写祖先的加固，**不是防恶意 root 的沙箱**，也不是多机协调。修复只写了三个文件（`secure_files.py`、`transactions.py`、`tests/test_journal_safety.py`），sha256 前 12 位依次为 d36993a03f11、7ccf404d1480、06cfd0a759df（与记录一致，作者核对）。

### 15.2 行为（以源码为准）

| 要求 | 实现 | 位置 |
|---|---|---|
| 平台 | SQLite 经 `file:/proc/self/fd/<目录 fd>/commits.sqlite3?mode=rw`（`uri=True`）打开；依赖 `/proc/self/fd`、本地 POSIX 属主与权限、`flock`；没有 `/proc` 的系统不可用 | `JournalFiles.uri` |
| 路径 | 相对路径以 `Path.cwd()` 补全，**不解析符号链接**；含 `..` 报 `unsafe journal path`。从 `/` 起逐级 `os.open(…, O_NOFOLLOW｜O_DIRECTORY, dir_fd=…)`，任一级是符号链接即失败 | `JournalFiles.__init__`、`_directory` |
| 祖先目录 | 属主须为 root 或当前 euid；mode 含 group/world 写位（`0o022`）即拒绝，唯一例外是 root 所有的 sticky 目录（如 `/tmp`），用户自有的 sticky 目录也拒绝；错误 `untrusted journal ancestor` | `_trusted_directory` |
| 终点目录 | 须 euid 所有且权限恰为 **0700**，错误 `journal directory must be private and owned (0700)`；不存在的各级目录（含缺失的中间级）以 `mkdir(mode=0o700)` 创建，已存在的只检查，**不 chmod** | `_trusted_directory`、`_directory` |
| 文件 | `commits.sqlite3`、`worker.lock` 以 `O_NOFOLLOW`、`O_NONBLOCK` 打开并请求 0600；打开后 `fstat` 须为常规文件、euid 所有、权限恰为 **0600**、硬链接数 1；`-journal` 旁文件同一标准；错误 `journal file must be private, regular, owned and singly linked (0600)`。**不符一律拒绝，不修复、不改 umask、不删除**；创建时请求的 0700/0600 仍受进程 umask 约束，更严的 umask 会导致拒绝而不是放宽 | `private_file`、`JournalFiles.__init__` |
| 锁 | 对 `worker.lock`（与旧 worker 互斥）和实际 DB 文件都 `flock(LOCK_EX｜LOCK_NB)`，拿不到抛 `BlockingIOError`；替换或别名 `worker.lock` 绕不过 DB 上的锁 | `JournalFiles.__init__` |
| 身份 | 记录祖先链每一级与终点目录的 `(st_dev, st_ino)`，持有目录 fd 与两个文件 fd。`validate()` 重新沿路径遍历并比对整条链，目录 fd 仍为 0700，每个文件 fd 与按名 `stat(follow_symlinks=False)` 的身份一致且仍私有，并检查 `-journal`、`-wal`、`-shm` 与 DB 头部；fork 出的子进程使用该实例被拒绝。错误：`journal directory identity changed`、`journal file identity changed`、`journal sidecar identity changed`、`journal closed or inherited across fork` | `JournalFiles.validate` |
| 验证时机 | `pending`、`pending_ids`、`prepare`、`complete` 前后各验证一次；事务在 `BEGIN IMMEDIATE` 之后、提交前、提交后验证；连接后、任何 SQL 之前立即验证；`Transactions` 的正常路径与 `recover_pending` 在解码保存的请求之后、**每次远程 `write` 之前**验证，**验证失败则不发送** | `CommitJournal._checked`、`_transaction`、`Transactions._run`、`recover_pending` |
| 失败处理 | 任何验证或 SQLite 异常都会关闭连接与持有的 fd；`close()` 幂等；该实例此后不可用，须重新构造。初始化中途失败（连接、命名空间、建表、DB 消失）同样释放 fd、连接与锁，之后可重开 | `CommitJournal.close`、`__init__` |
| SQLite 设置 | 要求 `journal_mode=delete`（否则 `journal requires rollback DELETE; explicit recovery required`）、`synchronous=FULL`、`trusted_schema=OFF`；`mode=rw` 而非 `rwc`，文件消失时不会新建另一个库；命名空间不符报 `journal deployment namespace mismatch` | `CommitJournal.__init__` |
| 回滚旁文件 | `-journal` 的身份被跟踪；见到后再消失报 `journal sidecar missing`，只在热回滚恢复后、`BEGIN IMMEDIATE` 之后与提交之后的验证里允许消失（`journal_removed=True`），其余验证里消失即报错 | `JournalFiles.validate` |
| 已有 WAL | DB 头部的读写版本字节为 WAL（2），或存在 `-wal`、`-shm`，在 `sqlite3.connect` 之前就报 `WAL journal unsupported; preserve files for explicit recovery`。**不转换日志模式、不 checkpoint、不删除任何旁文件**；DB 与旁文件字节级保留（测试对比），由运维人员受控恢复或迁移 | `JournalFiles.validate` |

不变的部分：部署命名空间校验、保存的请求 ID 与请求体的原样重放、`recover_pending`、业务规划与冲突语义（`id_exists` 单例竞争与回执竞争仍按第 8 节重读重算）。

### 15.3 未知结果不重发（口径）

- ③ 的远程写入结果未知（传输、协议异常）：journal 条目保留，不删除、不重新规划，之后只用保存的**同一 request_id 与请求体**原样重放（`recover_pending` 或事件重投时）；这是幂等重放，不是新请求。
- 发送前验证失败（例如目录被替换）：**不发送**，条目留在 journal，实例被关闭，重启后再走上面的重放。
- 这一节只管 ③ 的 journal。④ 的模型调用结果未知时停止、不换新 `call_id`、不自动重发，见 [④ 实现说明](../../extraction/ai-docs/implementation.md)与跑通计划第 2.1 节；本次修复没有触及 ④。

### 15.4 测试覆盖（`tests/test_journal_safety.py`，76 项；合成文件系统攻击，不用私有数据与外部服务）

| 场景 | 测试函数 | 项数 |
|---|---|---|
| 0777 共享祖先、`sqlite3.connect` 时目录被替换，都不会在目录外创建或写入 | `test_connect_directory_swap_never_writes_outside` | 2 |
| 祖先与终点目录是符号链接 | `test_directory_symlinks_rejected` | 2 |
| 0777、0775、用户自有 sticky 祖先在创建前即拒绝 | `test_untrusted_ancestor_rejected_before_creation` | 3 |
| DB、`worker.lock`、`-journal`、`-wal`、`-shm` 为符号链接、硬链接、公开权限、FIFO、目录时拒绝，不调用 SQLite、不修复 | `test_unsafe_files_rejected_without_sqlite_or_repair` | 25 |
| 终点目录 0755、0750、0770、1700 被拒绝且权限不被改 | `test_private_final_directory_required_without_repair` | 4 |
| 错误属主（合成 stat 结果，没有跨 UID 进程） | `test_wrong_owners_rejected` | 1 |
| `worker.lock` 被替换仍绕不过 DB 锁；旧 worker 的锁仍互斥 | `test_worker_lock_replacement_cannot_bypass_database_lock`、`test_legacy_worker_lock_still_excludes` | 2 |
| 重开、命名空间不符、私有权限、相对路径、重复 close、相同待重放项原样重放 | `test_reopen_namespace_private_modes_and_relative_path` | 1 |
| 事务之间替换目录、DB、`worker.lock`、权限、祖先（4 个方法 × 5 类） | `test_replacement_between_transactions_fails_closed` | 20 |
| 事务期间替换目录、DB、旁文件，或删除旁文件，阻止提交 | `test_change_during_transaction_blocks_commit` | 4 |
| 事务前注入的旁文件不被跟随 | `test_sidecar_injected_before_transaction_not_followed` | 3 |
| 解码后、正常与恢复路径的远程写入前替换目录，阻止发送 | `test_directory_change_before_remote_send_blocks_write` | 2 |
| 连接、命名空间、建表、DB 消失导致初始化失败，释放 fd、连接与锁并可重开 | `test_initialization_failures_release_fds_connections_and_locks` | 4 |
| WAL 头与未 checkpoint 的真实 WAL：拒绝、不调用 `connect`、目录字节级不变，独立检查原状态仍在 | `test_wal_rejected_without_losing_state` | 2 |
| 真实子进程非干净退出后，热回滚恢复保留待重放项且完整性检查通过 | `test_hot_rollback_recovery_preserves_pending` | 1 |

### 15.5 限制与升级影响

- **仅 Linux 本地文件系统**；不是多机协调，不保证 NFS 或断电后的持久性。
- 不抵抗恶意 root，也不保证抵抗同 UID 进程的任意竞态；替换类测试是确定性注入，不是并发穷举证明。属主检查用合成 stat 结果，没有真正的跨 UID 攻击进程。
- 热回滚恢复只覆盖真实子进程非干净退出一次；没有 worker 强杀端到端、断电或文件系统持久性测试。
- **对已有 journal 的影响**：目录不是 0700、文件不是 0600、属主不符、硬链接、WAL，或祖先目录组/其他人可写、路径上有符号链接的，现在都会拒绝启动。代码不会替运维修复；是否修权限、恢复或迁移由运维人员决定，动手前先保留原样（备份 DB 与全部旁文件）。本任务没有迁移或删除任何真实 journal，也没有改过全局权限或 umask。
- 仍没有：命令行或常驻启动入口、日志枚举与清理、超出 1000 条的显式恢复入口、WAL 的受控恢复或迁移工具、既有 journal 的权限修复工具。
