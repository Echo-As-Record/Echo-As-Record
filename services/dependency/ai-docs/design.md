---
audience: ai
doc-type: guide
status: proposed
generated-by: Claude (AI)，应模块负责人要求起草
generated-at: 2026-11-10
based-on: docs/design/design-v1.2.md §4、§8；docs/design/signals-supplement.md §2–§4、§6.2–§6.3；docs/design/work-breakdown-v1.2.md ⑤节；docs/design/plugins-mvp.md；ai-docs/plans/mvp-implementation.md P4/P5；origin/dev 8750a83 的 contracts（reevaluation-requested、reevaluation-result）与①代码（ear_storage_client protocol/ops/context/completion/remote，ear_storage service.py 读授权、history_scheduler.py，tests/test_plugin_chain_e2e.py）
---

# ⑤ 依赖分析模块：实现设计（草案，未审）

本文是⑤的工程实现方案：模块结构、调度算法、持久化与恢复、测试计划和交给 Codex 的任务拆分。**目标语义不在这里定义**，以[设计 v1.2 第 8 节](../../../docs/design/design-v1.2.md)、[信号补充](../../../docs/design/signals-supplement.md)和[工作分解⑤节](../../../docs/design/work-breakdown-v1.2.md)为准；本文只做工作分解里写明由“实施方决定”的选择（调度算法、轮询间隔、去重/合并策略、安全限额、恢复细节）。

跨模块可见的格式（传播记录、按钮反馈）和需要①补的权限另写在[⑤接口提案](../../../docs/proposals/dependency-scheduler-interfaces.md)（`status: proposed`），未采纳前本文对应部分只是候选方案。

## 0. 当前状态

- `services/dependency/` 在 dev 8750a83 上只有 `.gitkeep`，没有任何代码。
- ①侧需要的基础能力已随 PR #8 合入 dev：字段依赖与过时查询、当前图防环、动态登记表、`PublishReevaluation`、按所属方验证的完成记录 `get_reevaluation_result`、多起因 `additional_causes` 与请求关联、P5 历史重判接口。
- 本轮**不管③④⑥是否完成**：用合成所属方和测试插件验证⑤；原八步第 6、8 步与 D1 场景等③④接入后再验。

## 1. ⑤用到的①接口（签名以代码为准）

| 用途 | 接口 | 注意 |
|---|---|---|
| 收变更 | `subscribe("dependency.changes", handler, event_types=["data_changed"])` + 远程 `poll_once` / `run_until_idle` | 至少一次；处理结果与 `MarkProcessed` 同批 |
| 动态类型 | `client.registry`（远程每次请求 `/v1/registry`） | 只返回⑤可读的类型；所属方 `writer`、`supports_reevaluation` 来自这里，不另建所有权表 |
| 找下游 | `dependents_of(id)`、`stale_dependencies(ids)`、`dependencies_of(id)`、`dependency_set(id)` | 服务端会**静默丢弃**⑤无读权限的边（`service.py` 读授权过滤），见第 8 节 A2 |
| 读新旧版本 | `get`、`get_version`、`history`、`get_many`、`snapshot()` | 一次决策内的多次读取放在同一 snapshot；snapshot 有 TTL，不能长持 |
| 发请求 | `PublishReevaluation(target_type, target_id, cause, change_description, additional_causes, event_id)` | Context 用 `.deeper()`，depth ≥ 1；与传播记录同批写入 |
| 查完成 | `completion.reevaluation_result(reader, request_event_id)` | 只认登记了 `reevaluation_result` 能力的结果类型；`None` = 尚未完成 |
| 写自己的记录 | `PutSystemRecord`（`propagation_record`，writer=`dependency`） | 用 `expected_revision` 做乐观并发 |
| 历史补处理 | `list_history_reevaluations`、`attach_history_request` | 只接受真实已提交的请求 |
| 关联模型调用 | `Query("llm_call", where={"request_event_id": ...})` | ⑤需要 `llm_call` 读权限 |

## 2. 模块结构

```text
services/dependency/
  README.md                     人读入口
  pyproject.toml                包名 ear-dependency，入口 ear-dependency
  src/ear_dependency/
    config.py                   轮询间隔、超时、限额（显式配置，有默认值）
    domain.py                   Node / Cause / Trigger / Step 等纯数据类型
    gating.py                   输入门控：哪些 data_changed / feedback 会成为触发
    graph.py                    工作图：前向闭包、节点状态、就绪判定（纯逻辑，不做 I/O）
    causes.py                   起因合并、主起因选择、change_description 生成
    planner.py                  给定工作图与读结果，产出“要发的请求 / 要写的记录”（纯函数）
    records.py                  propagation_record 的读写与恢复
    service.py                  DependencyService：事件 handler、轮询循环、提交批次
    history.py                  P5 历史补处理接入
    export.py                   传播记录与关联调用的导出、统计
    cli.py                      serve / export / stats
    testing/                    合成所属方、测试插件、故障注入（仅测试）
  tests/
```

原则：`graph`、`causes`、`planner`、`gating` 是纯逻辑，可以用表驱动测试穷举；`service` 只负责读、调用 planner、提交批次和处理冲突。生产代码只依赖 `StorageClient` 协议，不依赖 `MemoryStorage` 内部状态。

## 3. 输入与门控（`gating.py`）

| 输入 | 处理 |
|---|---|
| `data_changed`，`operation` 为新建/追加（`old_version is None`） | 不是触发：新对象不会让已有依赖过时。原始数据只追加，同样忽略 |
| `data_changed`，`skip_history=true` | 不是触发（历史门控）。只作用于该对象的这次变化，不扩散到同批其他对象 |
| `data_changed`，类型 `kind=rule` | 不是触发：规则编辑不主动重算历史；规则变化由所属方在执行重判时作为额外起因记录 |
| `data_changed`，普通更新或整体替代 | 触发，起因为 `(data_type, data_id, old_version, new_version)`，沿用事件的 root/depth/additional_causes |
| `data_changed`，类型 `feedback`，payload 的 `module == "dependency"` | 按钮反馈入口，见第 6 节 |
| `reevaluation_requested` | ⑤不消费（⑤是发出方） |

每个被接受的触发，在处理事件的同一批里写一条 `trigger` 传播记录并 `MarkProcessed`。之后的展开都可以从这条记录恢复，所以消费标记提交后崩溃也不会丢工作。

## 4. 调度算法（`graph.py`、`planner.py`）

### 4.1 工作图

⑤在内存里维护一张全局工作图，节点是对象 id，**所有活跃触发共用这一张图**（多个触发因此天然合并）。

1. **展开**：对触发对象 X，按 `dependents_of`（全部当前依赖方，不只过时的）做有界 BFS，得到“可能受影响的前向闭包” G(X)。边方向是“上游 → 下游”。①保证当前图无环，所以 G 是 DAG。
2. **节点状态**：`pending`（待决定）→ `requested`（已发请求，等完成）→ `done`（变了 / 没变 / 不适用）；或者 `unaffected`（上游都已定且自己不过时，本路径停止）、`blocked`（失败、超时、停用、限额、目标已替代等，带原因）。
3. **就绪**：节点在 G 中的所有上游都已离开 `pending` / `requested` 后才做决定。这就是“已知相关上游可并行、下游等它们完成”：同一层互不依赖的节点同时就绪、并行发请求；菱形汇合点等所有分支都完成。
4. **决定**：在同一 snapshot 里对就绪节点 D 调 `stale_dependencies(D 的上游 ∩ 已变化集合)`，只取 `dependent.id == D` 的过时边：
   - 有过时边 → 目标类型 `supports_reevaluation` 为真时发请求，转 `requested`；为假则 `blocked(not_reevaluable)`。
   - 没有过时边 → `unaffected`，这条路径停止，但 D 的下游如果另有过时上游仍会被决定。
   - D 已被整体替代 → `blocked(target_superseded)`，不发请求。
5. **完成推进**：轮询到结果后，`changed` 使 D 的下游在下一轮决定时看到新版本；`unchanged` / `not_applicable` 释放等待，但不会凭空让下游过时；`failed` 使 D `blocked(failed)`，其下游中只因 D 而等待的节点 `blocked(upstream_blocked)`，旧结果保留、标记为可能过时。

### 4.2 合并起因

- 一个节点在发请求前收到的所有起因（不同上游、不同触发、不同 root）合并成**一次请求**：主起因按（depth 最小，其次 occurred_at 最早，其次 id 字典序）确定，作为 `cause` 和 Context 的 root/depth；其余全部进 `additional_causes`（`TracedCause`，保留各自 root/depth）。不丢任何起因。
- 节点已 `requested` 时又来新起因：不能追加到在途请求。记为该节点的**后续轮次**；在途请求完成后重新查 `stale_dependencies`：仍过时就再发一次（带上这些起因），已不过时（所属方执行时已读到新输入）就把这些起因记为“被在途请求吸收”，写进传播记录。
- `cause.old_version` 取依赖登记里的版本，`new_version` 取当前版本；整体替代的版本对按事件。

### 4.3 change_description

按依赖类型生成短文本：字段依赖列出变化的字段名和截断后的新旧值（如 `references: 3 项 → 4 项`），整对象依赖列出变化的顶层键，整体替代写“整体替代”。长度上限可配置（默认 500 字符）。它只是给所属方和模型看的提示，所属方仍须自己读当前状态。

### 4.4 终止与限额

- 没有新输入时：G 有限、每个节点每轮只决定一次、后续轮次只能由新起因产生，所以有限步内全部进入 `done` / `unaffected` / `blocked`。
- 限额（默认值可配置，触发都写传播记录并输出告警日志，**不算成功**）：单个触发的闭包节点数（默认 10 000）、depth（默认 64）、同一目标在同一 root 下的轮次（默认 5）。超限的节点 `blocked(limit_exceeded)`。
- 持续输入下不保证总任务有限，也不做全局停顿。

### 4.5 超时、重试与停用

- 已发请求超过 `request_timeout`（默认 10 分钟）没有结果 → `blocked(timeout)`，下游受阻；**继续低频轮询**，迟到的结果仍然处理并解除阻塞。
- ⑤不自动重发同一请求（总线和所属方负责重投）；超时后的重试由新的输入或运维操作触发。这是 MVP 选择，见提案“待决”。
- 所属方已停用 / 撤权：不发请求，直接 `blocked(owner_disabled)` 或 `blocked(owner_unauthorized)`；恢复后由①的 P5 历史任务交给⑤（第 7 节）。判断依据需要①提供可读的生命周期状态，见第 8 节 A3。
- 可重试的①冲突（`version_mismatch`、`revision_mismatch` 等）在新 snapshot 里重读重算；不可重试的冲突让 handler 抛异常、交给总线重投或进死信，不静默确认。

### 4.6 轮询

MVP 单 worker：一个循环交替做三件事，`poll_once` 收事件、对 `requested` 节点批量查完成、对就绪节点做决定并提交。完成查询间隔默认 2 秒，空闲时指数退避到 30 秒，有新事件立即恢复。后续若需要多 worker，用传播记录的 `expected_revision` 做节点级互斥，本轮不做。

## 5. 持久化与恢复（`records.py`）

⑤的状态只存在①里，格式见[提案第 2 节](../../../docs/proposals/dependency-scheduler-interfaces.md)：

- `trigger` 记录：每个被接受的输入一条，与 `MarkProcessed` 同批；结束时写汇总（节点数、各结局计数、停止原因）。
- `step` 记录：每个节点的每次决定一条。发请求时与 `PublishReevaluation` **同批**写入，请求和记录要么同时存在，要么都不存在；之后用 `expected_revision` 更新结局。

重启恢复：查询所有未关闭的 `trigger` 和未终结的 `step`，按当前依赖图重新展开闭包，已有 `step` 的节点直接还原状态，`requested` 节点继续轮询。内存图只是缓存，丢了可以重建。

## 6. “重新判断其他相关记录”入口

格式与语义见[提案第 3 节](../../../docs/proposals/dependency-scheduler-interfaces.md)：同时接受形式 A（`correction_feedback_ids`，MCP 与推荐）和形式 B（⑥现有草案 `previous_target_id` + `new_target_id`）。执行步骤：

1. 读到 `module == "dependency"`、`kind == "reevaluate_related"` 的反馈 G。
2. **校验**（纯函数，放在 `gating.py`）：形式与字段齐全；形式 A 的每个 id 都能读到、是 `feedback`、命令为纠正归属；两种形式都给时互相一致；id 数不超过上限。任何一项失败：同批写 `trigger(status=rejected, reason=...)` 与 `MarkProcessed`，**不抛异常**，以免无效输入反复重投进死信。
3. 校验通过：同批写 `trigger`（root = G，触发深度 0，请求 depth = 1）与 `MarkProcessed`。
4. **等纠正生效**（只对形式 A）：每条纠正的被纠正数据，当前归属由该纠正反馈写入或已有更新版本才算生效；单条超时则该条 `blocked(correction_not_applied)`，其余继续。
5. 以全部纠正前后对象为种子，查依赖它们的过时归属（排除被纠正数据本身），作为根节点放进工作图；之后与普通触发相同。
6. **不按 id 去重**：重复请求靠第 4 节的“只对当前过时依赖发请求”自然收敛（已完成的再查不到过时，在途的合并或吸收）。需要的测试见第 9 节。
7. 用户确认保护由③在执行时保证，⑤不预先过滤。

在⑥接入前，测试直接用 `feedback` 原始记录触发；用合成的纠正反馈覆盖两种形式。

第 2、4、5 步读取的字段仍未确定：纠正反馈的命令字段（⑥草案 `correct_attribution` 的 `subject_id`、`previous_target_id`、`new_target_id`，③开发文档 P3）、归属的 `subject_id`、`target_id` 和人物/地点的 `references`（①草案名，③开发文档 P1）。实现时把这些字段名集中在一处常量里，便于③确认后替换；`scripts/check_attribution_fields.py` 用来检查③是否已经确定，用法见[模块入口](../README.md)。

## 7. 历史补处理（`history.py`）

管理员对恢复的插件选择补处理后，①生成历史任务。⑤轮询 `list_history_reevaluations`，对每个需要真实请求的条目：按第 4 节同样的规则发 `PublishReevaluation` 并写 `step`，再 `attach_history_request(job_id, item_id, request_event_id)`；完成同样靠轮询结果。跳过（skip）的任务⑤不参与。

## 8. 依赖①确认或补齐的事项

| 编号 | 事项 | 现状 | 影响 |
|---|---|---|---|
| A1 | 部署签发 `dependency` 身份：读全部核心类型、`feedback`、各结果类型、`llm_call`，订阅可订阅的核心类型与 `feedback` | `init-identities` 只签发管理员与 manage-worker | 无身份⑤无法在部署中运行；测试可自行 `issue_principal` |
| A2 | 插件类型注册后，⑤自动获得读与订阅；或依赖查询在有边被隐去时明确报出 | `dependents_of` / `stale_dependencies` 静默过滤⑤不可读的边 | 不补就会**静默漏查**插件下游，违反“受影响依赖不漏查” |
| A3 | ⑤可读所属方的生命周期状态（启用 / 停用 / 撤权） | 只有管理员可读的插件目录 | 不补则停用只能表现为超时，无法明确标“停用受阻” |
| A4 | ③④类型（归属、产物、`rule_selection`）登记 `supports_reevaluation=true` | 核心类型能力项目前全为 false | ③④接入前，测试只能用 `extend_core` / 插件类型 |

## 9. 测试计划

- **纯逻辑**（不需要①）：门控表；闭包展开；就绪判定；菱形、链、分叉、多根合并；后续轮次与吸收；限额；超时与迟到结果；主起因选择的确定性。
- **重判反馈**：两种形式；各类无效引用被拒绝且只处理一次、不进死信；纠正未生效时等待与超时；同一纠正重复请求、并发请求、连续纠正同一数据、一次多个 id 与分次调用结果一致，且不产生重复重判。
- **内存①**（`MemoryStorage` + 合成所属方）：所属方按脚本返回 changed / unchanged / not_applicable / failed / 不回；验证请求与 `step` 原子、重复投递只生效一次、崩溃后从记录恢复、无变化释放下游、失败阻塞下游、整体替代上游交下游重判。
- **真实①**（`EAR_RUN_POSTGRES_TESTS=1`、`EAR_RUN_NATS_TESTS=1`，沿用①的本机临时容器夹具）：动态注册测试插件后无需重启⑤即被发现；跨插件和插件内部超过两层的链；并发造环被①拒绝；停用与历史补处理。参照 `services/storage/tests/test_plugin_chain_e2e.py` 的夹具，但用真正的⑤替换其中写死的测试驱动。
- **导出**：按 root 导出完整传播（触发、各步、起因、结局、关联 `llm_call`），统计计数与深度分布。

验收对照工作分解⑤节：除 D1 场景与原八步第 6、8 步（需③④）外，其余条目都应有测试证据。

## 10. 交给 Codex 的任务拆分

按顺序，每步一个 PR 粒度，前一步合格再派下一步：

1. **骨架与纯逻辑**：包结构、`domain` / `gating` / `graph` / `causes` / `planner` 及表驱动测试；不碰 I/O。
2. **服务与持久化**：`service` / `records`，内存①上的端到端测试与故障注入；`propagation_record` 先按提案草案写，提案若改动再跟进。
3. **合成插件与真实①**：`testing/` 下的多层测试插件，真实 PostgreSQL + NATS 测试；A2 未解决前，测试显式给⑤授权并记录这一前提。
4. **按钮入口与历史补处理**：第 6、7 节。
5. **导出与统计、CLI、模块 README 同步**。

每步交付都跑 README 的提交前检查，并如实报告未测项。
