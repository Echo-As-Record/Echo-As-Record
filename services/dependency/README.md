---
audience: human
doc-type: reference
based-on: 设计 v1.2 §8；信号补充 §3.4；工作分解 v1.2 ⑤节；origin/dev c5c31f8（该基线 services/dependency 没有代码，只有文档与脚本）；79e8dc3 中的内存路径与 e2e 记录（作者读代码，见跑通计划第 2.1、2.2 节）；任务状态文件 /tmp/ear-mvp-dependency-state.json（非仓库文件，写于 2026-11-11T19:52Z）；第二批（工作区未提交）：services/dependency 源码（remote.py、authority.py、journal.py、secure_files.py、causal_fields.py、service.py，作者读源码，不运行测试）、services/storage 的 scheduler_access.py 与 core_deployment.py（只读核对）；验证来源为仓库外文件，作者读取、测试结果未复跑：最终验收 /tmp/ear-mvp-second-final.{log,exit,xml} 与 ear-mvp-second-final-frozen-source.json（作者重新对照源码哈希）、/tmp/ear-mvp-coverage-fix/verification.json，早期摘要 /tmp/ear-mvp-dependency-second.json、/tmp/ear-mvp-scheduler-compat.json、/tmp/ear-mvp-storage-second.json；代码基线 integration/mvp-runnable = 79e8dc3；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核，本文不新增授权记录；未填写或改动任何 reviewed 字段
---

# ⑤ 依赖分析模块

输入变化后找出确实过时的结果，等相关上游完成，再请求所属模块重新判断；全程记录传播，供追溯、展示和论文实验统计。⑤是“请求重新判断”事件的唯一发出方，只写传播记录，不代写任何业务结果或插件登记。

- 目标行为：[设计 v1.2 第 8 节](../../docs/design/design-v1.2.md)、[信号补充 §3.4 与 §4](../../docs/design/signals-supplement.md)
- 职责与验收：[工作分解 v1.2 ⑤节](../../docs/design/work-breakdown-v1.2.md)
- 对外接口提案（传播记录、按钮反馈、①权限，**proposed**）：[⑤接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)
- 实现设计与任务拆分（**草案**）：[实现设计](ai-docs/design.md)
- 用到的①接口：[① SDK 指南](../storage/ai-docs/interface.md)
- 第二批的整体交接与验证来源：[MVP 运行时交接](../../ai-docs/plans/mvp-runtime-handoff.md)

## 状态

**⑤ 的内存路径在 79e8dc3（首批）；远程路径是第二批，只在工作区：未提交、未提 PR、未部署。两者都只是切片，不是完整的 ⑤，也不是 MVP 完成。第二批的最终四开关验收已完成（合成数据，见下面的“验证”），但仍未提交，且没有 CI 结果。** 状态词按[跑通计划第 8 节](../../ai-docs/plans/mvp-runnable.md)分开写。设计与接口文档的状态没有变：[实现设计](ai-docs/design.md)仍是草案，[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)仍是 proposed。

- **代码在哪**：`services/dependency/src/ear_dependency/`。`DependencyService`（`service.py`）是内存路径；`RemoteDependencyService`（`remote.py`，工作区）是远程路径，配套 `authority.py`（校验服务端调度状态）、`journal.py` 与 `secure_files.py`（私有传输 journal）、`causal_fields.py`（字段级因果门控）。只通过存储 SDK 读写，**不写任何业务数据**。调度状态存成 `dependency` 拥有的 `propagation_record`，用**私有的版本化格式**（带 `schema_tag`），不是已批准的 Schema，也没有提升接口提案。
- **两条路径的边界**：`DependencyService` 仍只接受内存客户端 `MemoryClient`，且要求显式 `full_visibility=True`；远程客户端会被拒绝（`scheduler_authority_unavailable: remote execution refused`）。它用于合成测试，不是授权或生命周期权威。远程要用 `RemoteDependencyService`：没有 `full_visibility` 选项，也不会回落到内存。
- **两条路径共有的能力**：数据修改或整体替代的事件先查当前状态，再按历史门控筛选（新原始数据、规则编辑、`skip_history` 不触发），触发记录与 `MarkProcessed` 原子提交；有界地展开当前前向依赖图；按所属方和是否支持重判决定能否发请求，发请求前先持久化意图；用权威的 `get_reevaluation_result`（不是队列标记或事件沉默）判断完成，区分 changed/unchanged/failed/not_applicable，受保护或不可用会阻塞下游；保留完整起因并合并多起因，新起因在途时下游等待间接上游；worker 可由存活的存储记录重建，请求超时后只轮询迟到的 Finish，不重发；超出限额失败关闭，不截断。
- **字段级因果门控（工作区）**：只有被证明改变了边所登记字段（或整体替代）的转移才使依赖边生效；缺少历史版本或读权限不被当作“有变化”，也不被当作“无变化”，而是阻塞（`causal_versions_unavailable`、`read_denied`）。
- **远程路径（工作区）**：`RemoteDependencyService(client, journal=CommitJournal(路径, namespace=…))`。先 `subscribe()`（先恢复未决写入，再订阅 `data_changed`），再交替调用客户端的 `poll_once()` 与 `tick()`；循环与生命周期由调用方负责，[统一 worker](../../ai-docs/plans/mvp-runtime-handoff.md) 第 4 节提供了宿主。
  - **服务端权威，覆盖不全即失败关闭**：每个动作、每一步、发布前与回放前都读 `GET /v1/scheduler/state`，严格校验形状（`codec`、`coverage_complete`、覆盖修订、各所属方的 `enabled`/`can_receive`/`revision`，`online` 必须为 null）。覆盖不完整、被撤销、一轮中变化，或所属方停用、没有可接收凭据（`owner_disabled`、`owner_unauthorized`），都会阻塞并保留证据，**不会被当成空图或成功**。这取代了首片“只支持内存，因为过滤读取证明不了完整”的限制：远程已支持，但前提是 ① 处于 strict 轮廓（见[①部署指南第 9 节](../storage/ai-docs/deployment.md)）；旧库上 `scheduler_state()` 被拒绝，⑤ 只会阻塞，不会降级成过滤图。公开的 `records()` 同样在有界读取前后要求当前 strict 覆盖：覆盖被撤销或旧库抛 `PermissionDenied`，其他阻塞（如 `scheduler_state_changed`）抛 `SchedulerBlocked`，不返回空表或部分表。这是失败关闭的工程选择，不是已采纳设计的新语义。
  - **持久与重放**：调度状态是 `propagation_record` 系统记录；本地私有 SQLite journal 只存原始传输意图（规范化的写操作、上下文与 request_id），提交结果未知后用**相同 request_id 与请求体**幂等重放，且在订阅前与每次决策前先恢复，即使 `MarkProcessed` 已抑制重投。未知发布期间权威修订变化时，保留 pending 证据并阻塞，不用新 request_id 重发。
  - **journal 文件安全**：私有 0700 目录、0600 单链接属主文件，祖先链不跟随符号链接，目录与文件身份被钉住并复核，目录、数据库、worker 三层非阻塞 `flock`，拒绝 fork，拒绝 WAL 且不删证据，发送前先校验。一个 journal 目录对应一个部署、身份与 consumer。
- **没做**：
  - 按钮反馈入口：收到属于 `dependency` 的反馈时显式记为 `blocked`（`feedback_not_implemented`），不冒充处理；形式 A/B、纠正生效证明、历史列表等都没接。
  - P5 历史分配、追赶与跳过：没有自动历史扩展。
  - 分布式租约：本地锁不是跨主机围栏，只能单 worker，且所有启动须复用同一 journal 目录与命名空间。
  - 未知发布结果的操作员对账入口，以及用新 ID 的自动重规划。
  - 依赖图并发改线：没有跨整个调度过程的可串行化，只验证了有界小图，生产规模与吞吐未验证。
  - `llm_call` 导出、可观测性与保留策略；失败、受保护、不可用或溢出阻塞后的自动重试（旧结果保留，不伪造成功）。
  - 身份、授权、迁移与核心类型登记不由 ⑤ 创建，由 ① 的 core 初始化提供，同样是工作区未提交的切片。
- **验证（来源、日期与口径；合成数据，没有真实模型、真实日历，没有访问常驻实例）**：
  - **最终验收（当前结论）**：2026-11-12T12:03–12:58Z，`/tmp/ear-mvp-second-final.*`，退出码 0，四开关均为 1，本地构建的镜像：全量 3659 passed、11 skipped、2 warnings，3198.40 s。本模块 `services/dependency/tests` 231 项全部通过；`services/storage/tests/test_dependency_runtime.py`（真实回环 HTTP、临时 PostgreSQL 17 与 NATS 2.11）18 项通过（475.2 s）。源码冻结清单经重新对照无变化。逐项与边界见[交接第 6.1 节](../../ai-docs/plans/mvp-runtime-handoff.md)；这不是 Compose 部署，不是真实数据，一次全绿也不证明不再偶发。
  - **覆盖撤销后的 `records()`**：此前 07:47–08:40Z 的全量验收里，`test_dependency_runtime.py` 的 `[coverage]` 用例红（撤销覆盖后 `records()` 不拒绝）；08:48–09:04Z 修复并加了 2 个回归（该文件 16 项变为 18 项），红 3 failed（真实运行）与 17 failed（单元），绿 231 passed（单元）与 18 passed（548.67 s，真实运行）。来源 `/tmp/ear-mvp-coverage-fix/verification.json`，见[交接第 6.2 节](../../ai-docs/plans/mvp-runtime-handoff.md)。
  - **历史，保留（已被上面取代）**：2026-11-12T02:12Z（dependency-second 摘要，基线 79e8dc3 加当时工作区）模块单元 137 passed，`test_dependency_runtime.py` 的 16 项一次连续通过（604.02 s），覆盖多所属方菱形与多层链、changed 与 unchanged 的 Finish、相关上游等待与重启后不重复发布、各阶段相同请求重放、撤销覆盖或所属方凭据后阻塞、新进程重启，另有在 `CommitUnknown` 前后让子进程 `os._exit` 的 5 项边界批次。
  - **“16 项冲突”（历史，已解释）**：2026-11-12T03:50Z（scheduler-compat 摘要）称 ① 改为只由完整 core 轮廓选择 strict 后，这 16 项在整仓 PG+NATS 运行中失败（夹具只登记了部分类型，被判 `coverage_denied`）；诊断运行（完整轮廓，仓库外插件）16 passed（676.17 s）。原因是夹具与收紧后的 strict 不一致，不是生产旁路；夹具修复后通过，现为 18 项并在最终验收里通过。
  - 首批（79e8dc3）的内存路径与 ②③④⑤ 合成联调的记录见[跑通计划第 2.1、2.2 节](../../ai-docs/plans/mvp-runnable.md)。
- **历史（保留）**：2026-11-11 的首片只支持内存客户端，并记有“不支持 `RemoteStorageClient`”“根配置尚未含 `services/dependency`”等限制。根接入已在 79e8dc3 的 `pyproject.toml` 中；远程限制由上面的远程路径取代，但远程路径尚未提交，也没有部署。

① 的 core 轮廓初始化（`init-core-identities`，新库专用、工作区）会签发 `dependency` 身份并预留 `dependency.changes`；插件类型对 ⑤ 的读授权仍是只做显式逐类授权，自动授权（提案中的 A2）未决，见[提案第 4 节](../../docs/proposals/dependency-scheduler-interfaces.md)。③④ 的完成协议首片在 79e8dc3，④ 的 v1.2 对齐在工作区；工作区另有 `services/storage/tests/test_mvp_core_runtime.py`：经真实认证的回环服务 HTTP 与临时 PG/NATS 的核心回路，该文件 6 项（含 4 项网络夹具回归）在最终验收里通过。这是合成数据上的核心回路，不是完整联调或部署，见[跑通交接第 6.1 节](../../ai-docs/plans/mvp-runtime-handoff.md)。

## 两个入口

| 入口 | 行为 |
|---|---|
| 已有数据被修改或整体替代（经历史门控筛选） | 找依赖它的当前有效、确实过时的结果，按所属方和是否支持重判发请求 |
| “重新判断其他相关记录”反馈（⑥按钮或 MCP） | 引用纠正反馈 id 时先等纠正生效；按纠正前后对象找过时归属并请③重判。无效引用明确拒绝，重复请求不会重复重判 |

新原始数据、规则编辑本身、被历史门控拦截的参考变化都不触发重判。

上表是目标行为。已实现的两条路径都只覆盖第一个入口，第二个入口尚未实现，见上面的“状态”。

## 等待③确定的字段

按钮入口要读被纠正数据的当前归属，用到③归属 payload 的字段名（`subject_id`、`target_id` 等），这些在③那边仍标“待确认”。在③确定前，⑤按①草案的字段名实现，并标注“待③确认”。首片还没有实现按钮入口，因此尚未用到这些字段。

检查③是否已经确定：

```bash
uv run python services/dependency/scripts/check_attribution_fields.py
```

它会 `git fetch origin`，然后只读地检查 `origin/dev` 和③分支上的几个信号：登记表注释、归属 Schema、③代码合入、③开发文档的 P1 状态。退出码 0 表示有进展需要核对，1 表示仍在等待，2 表示检查出错。加 `--json` 输出机器可读结果。
