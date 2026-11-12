---
audience: human
doc-type: reference
based-on: 设计 v1.2 §8；信号补充 §3.4；工作分解 v1.2 ⑤节；origin/dev c5c31f8（该基线 services/dependency 没有代码，只有文档与脚本）；工作区未提交的 services/dependency 源码与测试（作者读代码并用 pytest --collect-only 核对项数，未运行测试）；任务状态文件 /tmp/ear-mvp-dependency-state.json（非仓库文件，写于 2026-11-11T19:52Z）；2026-11-12 增补依据：根 pyproject.toml 的已暂存改动、仓库外整体验收日志 /tmp/ear-mvp-acceptance.{sh,log,exit}（作者读日志）、tests/e2e/test_core_memory_loop.py 与 services/dependency 源码（只读核对，另见跑通计划第 2.2 节）；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核；未填写或改动任何 reviewed 字段
---

# ⑤ 依赖分析模块

输入变化后找出确实过时的结果，等相关上游完成，再请求所属模块重新判断；全程记录传播，供追溯、展示和论文实验统计。⑤是“请求重新判断”事件的唯一发出方，只写传播记录，不代写任何业务结果或插件登记。

- 目标行为：[设计 v1.2 第 8 节](../../docs/design/design-v1.2.md)、[信号补充 §3.4 与 §4](../../docs/design/signals-supplement.md)
- 职责与验收：[工作分解 v1.2 ⑤节](../../docs/design/work-breakdown-v1.2.md)
- 对外接口提案（传播记录、按钮反馈、①权限，**proposed**）：[⑤接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)
- 实现设计与任务拆分（**草案**）：[实现设计](ai-docs/design.md)
- 用到的①接口：[① SDK 指南](../storage/ai-docs/interface.md)

## 状态

**首片已在工作区，未提交、未提 PR。它不再是纯文档草案，但仍只是首片：不是完整的 ⑤，不支持远程①，也不是 MVP 完成。** 状态词按[跑通计划第 8 节](../../ai-docs/plans/mvp-runnable.md)分开写：这里只能说“已实现（工作区）、内存替身上验证通过（任务记录）”，不是“已提 PR”“已合并”或生产验收。设计与接口文档的状态没有变：[实现设计](ai-docs/design.md)仍是草案，[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)仍是 proposed。

- **代码在哪**：`services/dependency/` 有模块 `pyproject.toml`、5 个源码模块（`src/ear_dependency/`）和 2 个测试模块（`tests/`）；入口是 `DependencyService` 的 `handle`、`tick`、`records`。只通过现有存储 SDK 读写，**不写任何业务数据**。调度状态存成 `dependency` 拥有的 `propagation_record`，用**私有的版本化格式**（带 `schema_tag`），不是已批准的 Schema，也没有提升接口提案。
- **能做什么**：数据修改或整体替代的事件先查当前状态再按历史门控筛选（新原始数据、规则编辑、`skip_history` 不触发）；触发记录与 `MarkProcessed` 原子提交；有界地展开当前前向依赖图（刚变新的边也作为栅栏，跨 root 的在途上游不造成全局停止）；按所属方和是否支持重判决定能否发请求，发布请求前先持久化意图；用权威的 `get_reevaluation_result`（不是队列标记或事件沉默）判断完成，区分 changed/unchanged/failed/not_applicable，受保护或不可用会阻塞下游，已终止则交下游所属方决定；保留完整起因并合并多起因，新起因在途时下游等待间接上游；worker 可由存活的存储记录重建，请求超时后只轮询迟到的 Finish，不重发；发布前后的 `CommitUnknown` 故障注入下只对账或冻结，不盲目新发；超出限额失败关闭，不截断；带合成多层与分叉汇合插件夹具。
- **当前不支持远程①**：构造时要求显式 `full_visibility=True` 且只接受内存客户端 `MemoryClient`，`RemoteStorageClient` 等一律拒绝（`remote execution refused`）。原因是没有传输层 `request_id`（`intent_id` 只是本地标识，不是服务端回执，不伪造），也没有调度权威与完整图投影。`MemoryStorage` 本身不持久，所以它证明的是“存储记录还在时 worker 可以重建”，不是数据库或进程重启后的持久性。
- **单 worker**：没有租约或围栏，单 worker 是使用前提，部署不会强制；乐观版本检查只保护单次写入，不是分布式租约。
- **只实现了第一个入口**：数据被修改或整体替代时找下游过时结果并请求重判。第二个入口（“重新判断其他相关记录”反馈，按钮或 MCP）**没做**，收到属于 `dependency` 的反馈时显式记为 `blocked`（`feedback_not_implemented`），不冒充处理；形式 A/B、纠正生效证明、历史列表等都没接。
- **没做的其他部分**：`RemoteStorageClient` 支持；①侧调度权威与完整图投影；身份签发与动态授权、生命周期（`owner_disabled`、`owner_unauthorized`、撤权与恢复，目前只有通用的 `publish_denied`）；真实 PostgreSQL/NATS 与进程或存储丢失后的恢复、远程回执重放；未知意图的对账与重放操作入口（只写了意图、尚无已请求状态时冻结为 `commit_unknown`）；与真实 ③④、⑥、⑦ 的集成，以及对核心登记表重判能力的修改；多 worker 租约与分布式单飞；依赖图并发改线的读集守护（测试覆盖的是已知当前 DAG 加后续事件扩展，不是全局可串行化的调度事务）；命令行或后台轮询、部署、`llm_call` 导出、可观测性与保留策略、生产规模；失败、受保护、不可用或溢出阻塞后的自动重试（旧结果保留，不伪造成功）。
- **根工作区**：2026-11-11 晚本文写下时，pyright 的 `include` 原本就含 `services/dependency`，但 uv workspace 成员、dev 依赖和 pytest `testpaths` 尚未包含它，⑤ 的根接入由协调者进行中。**2026-11-12 更新**：根 `pyproject.toml` 的已暂存改动已含 `services/dependency` 的 workspace 成员、dev 依赖、`tool.uv.sources` 与 `testpaths`，`uv.lock` 同步；本文作者没有改这些文件。
- **验证**：任务记录（2026-11-11T19:52Z）称模块本地 34 项内存测试通过，ruff、format、pyright（`--project services/dependency`）通过；本文作者只用 `--collect-only` 核对到 34 项，**未执行也未复跑**，并核对了记录里 8 个文件的 sha256 与当前工作区一致。演练维度：输入合成｜模型不适用｜外部不适用｜平台替身（内存）。没有真实服务，也没有跑全仓检查（记录里写明不在其范围内）。全仓统一检查由协调者在根接入完成后执行，结果出来前本文不记录“全绿”。
- **2026-11-12 更新（验证与边界）**：仓库外的整体验收脚本 `/tmp/ear-mvp-acceptance.sh`（退出码 0，2026-11-11T23:50:55Z 启动、2026-11-12T00:12:56Z 结束）静态检查通过（ruff、format、pyright 0 errors、`uv lock --check --offline`），四个门控开关全开的 pytest 为 2485 passed、11 skipped、无失败；本模块 34 项（作者收集到 34 项）在其收集范围内，且 `services/dependency` 在该次运行期间没有被修改（按文件修改时间核对）。另有 `tests/e2e/test_core_memory_loop.py`（2 项；该文件在整体验收启动后又被修改，当前内容由 2026-11-12 的定向复跑 2 项通过）把真实的 `DependencyService` 与 ②③④ 在 `MemoryStorage` 上联调一次纠正闭环：产生 2 个重判请求、结果 changed 与 unchanged 各一、重建 scheduler 与否都收敛、重复投递不再次调用模型；模型是确定性的合成替身，不经 ⑦ 与 HTTP，⑥ 只用到其反馈输入模型，**不是完整八步**。**没有变化的限制**：构造仍只接受内存客户端，`RemoteStorageClient` 被拒绝；`DEFAULT_REGISTRY` 仍没有 `attribution_state`、`attribution_feedback_result` 等核心新类型，`attribution`、`artifact` 的重判与强制依赖能力仍为 false，e2e 与测试夹具各自显式启用；核心登记表与 `contracts/` 没有改。以上仍是工作区未提交、未提 PR、未部署；这些验证没有使用真实数据，没有真实模型调用，也没有真实日历写入。汇总见[跑通计划第 2.2 节](../../ai-docs/plans/mvp-runnable.md)。

部署还没有⑤的身份，插件类型的读权限也待①确认，见[提案第 4 节](../../docs/proposals/dependency-scheduler-interfaces.md)。①侧基础能力已在 dev 上（PR #8）；③④的完成协议首片都只在工作区；与 ⑤ 只有上面所说的内存合成联调，没有真实服务联调，见[跑通计划第 2.1 节](../../ai-docs/plans/mvp-runnable.md)。

## 两个入口

| 入口 | 行为 |
|---|---|
| 已有数据被修改或整体替代（经历史门控筛选） | 找依赖它的当前有效、确实过时的结果，按所属方和是否支持重判发请求 |
| “重新判断其他相关记录”反馈（⑥按钮或 MCP） | 引用纠正反馈 id 时先等纠正生效；按纠正前后对象找过时归属并请③重判。无效引用明确拒绝，重复请求不会重复重判 |

新原始数据、规则编辑本身、被历史门控拦截的参考变化都不触发重判。

上表是目标行为。工作区首片只覆盖第一个入口，第二个入口尚未实现，见上面的“状态”。

## 等待③确定的字段

按钮入口要读被纠正数据的当前归属，用到③归属 payload 的字段名（`subject_id`、`target_id` 等），这些在③那边仍标“待确认”。在③确定前，⑤按①草案的字段名实现，并标注“待③确认”。首片还没有实现按钮入口，因此尚未用到这些字段。

检查③是否已经确定：

```bash
uv run python services/dependency/scripts/check_attribution_fields.py
```

它会 `git fetch origin`，然后只读地检查 `origin/dev` 和③分支上的几个信号：登记表注释、归属 Schema、③代码合入、③开发文档的 P1 状态。退出码 0 表示有进展需要核对，1 表示仍在等待，2 表示检查出错。加 `--json` 输出机器可读结果。
