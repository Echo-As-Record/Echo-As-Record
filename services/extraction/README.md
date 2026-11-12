---
audience: human
doc-type: guide
based-on: 设计 v1.2；c5c31f8 的④实现；本轮负责人授权④直接依赖⑦ GatewayClient；79e8dc3 中的④首片（orchestration.py、call_safety.py、domain.py、model_io.py、planning.py、worker.py、demo.py 与相应测试，作者读 git diff 与源码核对）；任务状态文件 /tmp/ear-mvp-extraction-followup.json 与 /tmp/ear-mvp-extraction-state.json（非仓库文件，其中的测试作者未复跑，只用 pytest --collect-only 核对项数）；2026-11-12 增补依据：工作区未提交的 services/extraction 源码（selection.py、reevaluation.py、artifacts.py、orchestration.py、planning.py、segmentation.py、suggestions.py、domain.py、demo.py、model_io.py，作者读源码与 git diff，不运行测试）、仓库外摘要 /tmp/ear-mvp-extraction-v12.json 与 /tmp/ear-mvp-action-protocol.json（作者读取，其中的测试结果作者未复跑，只用 pytest --collect-only 与文件哈希核对）、services/storage 的 core_deployment.py（只读核对）；代码基线 integration/mvp-runnable = 79e8dc3；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核，本文不新增授权记录；未填写或改动任何 reviewed 字段
---

# ④ 活动提取

负责活动、规则、Markdown 产物及外部动作建议。**79e8dc3 含调用安全与重判完成首片；v1.2 对齐（纯 Markdown、`rule_selection`、按当前规则重判与完整 Finish）以及外部动作 action-v2 的 ④ 侧状态流转是第二批，只在工作区：未提交、未提 PR、未部署。仍不是完整的 ④，也不是 MVP 完成。**

- [目标设计](../../docs/design/design-v1.2.md)、[模块间信号](../../docs/design/signals-supplement.md)。
- [使用与接入](ai-docs/implementation.md)：演示命令、宿主注入示例、契约差距、首片的代码导航与限制（该文写于 v1.2 对齐之前，v1.2 部分以本文和源码为准，差距见[跑通交接第 8 节](../../ai-docs/plans/mvp-runtime-handoff.md)）。
- [网页演示](ai-docs/web-demo.md)：合成数据和固定模型回复。
- [MVP 跑通计划第 2 节](../../ai-docs/plans/mvp-runnable.md)与[MVP 运行时交接](../../ai-docs/plans/mvp-runtime-handoff.md)：工作区进展、验证来源与待补项。

模型端口保留 `ModelGateway`，实际 HTTP 客户端统一使用 workspace 包 `ear-llm-gateway` 的 `GatewayClient`，构造时传④的 `SYSTEM`。客户端仍由⑦维护，协议见[⑦入口](../llm_gateway/README.md)。演示仍用测试替身；远程宿主见[交接第 4 节](../../ai-docs/plans/mvp-runtime-handoff.md)的统一 worker。

## 状态

**c5c31f8 基线（已合入 dev）**：`GatewayClient.complete(Call)` 每次都生成新的 call_id。传输错误 `gateway_outcome_unknown` 可能已经执行，不能自动换新 id 重试；可恢复流程须发送前持久化 call_id 并用 `reconcile` 核对。基线上④的任务自动重试、存储冲突重算与租约恢复没有适配此语义，重判也缺请求关联字段，且 `extraction_run` 占用了请求的 event_id，使 ① 的 Finish 无法写入。

**79e8dc3（首批，已在分支历史中；不是完整 v1.2）**：

- **调用安全**：每次发送模型调用前，先把稳定的 `call_id` 和规范化请求的 SHA-256 以版本 CAS 写进 `extraction_run`；可能已经执行的调用（超时、断连、非 200、发送后崩溃、租约过期）一律停止为“结果未知”，**不自动换新 id、不重发**，`reconcile` 只补审计记录，不取回回复，也不解除阻塞。
- **重判与完成**：重判运行的 run id 与请求 event_id 解耦，请求的 root、depth、`request_event_id` 和全部额外起因原样传给⑦与存储写入；对 artifact 重判，业务写入、完整依赖、`FinishReevaluation`、`extraction.completion` 标记和 run 成功在同一批提交，无变化也提交完成结果但不新增 artifact 版本。基线上占用了请求 ID 的旧记录不改写、不迁移，未完成的会被阻塞。
- **远程订阅下的 worker**：`tick` 在客户端支持 `poll_once` 时先拉取一条 `extraction.jobs` 投递。

**第二批：v1.2 对齐（工作区，未提交）**：

- **纯 Markdown**：新生成的产物与动作文本是直接写名字的纯 Markdown；新输出里出现 `[[person:…]]`、`[[place:…]]` 引用标记会被拒绝（`legacy_entity_marker_in_new_output`）。旧文本不改写，也不反向解析成依赖。
- **活动划分只用原始数据**：划分与合并只读原始数据；归属就绪检查与实体解析分开，分段之后才组装人物、地点。实体输入只投影 `payload.name`（保留缺失与 null 的区别），登记名字字段依赖（含首次命名）；每个区间归属仍是粗粒度依赖，另加活动与当前规则。手动与自动产物都登记完整依赖集。
- **`rule_selection`**：每个活动一份版本化的选规则结果（核心类型，含空候选、空选择）。它的依赖覆盖活动、评估过的当前规则候选、所有区间归属和投影的实体字段；候选集合以规则类型修订在同一批里守护。**产物不依赖选择结果。**
- **按当前规则全集重选**：新选中的规则生成自动产物与建议；退出的规则使自动产物和仅 pending 的建议被替代；仍选中的不重新生成。`manual` 与 `source=manual` 的产物受保护，不被自动取消选择。
- **按当前规则重判**：产物重判读同一规则 id 的**当前**版本，规则出新版本时不再阻塞（旧代码 `historical_rule_dependency_unsupported` 已不在源码中）；规则版本变化时必须有完整的生成结果，字节相同的文本也可能新增产物版本。规则被删除或替代时，产物与仅 pending 的建议被终止，并以 `not_applicable` 完成；已审核的建议不动。规则编辑本身不扫历史。
- **起因与 Finish**：从规则历史发现的额外起因，连同所有贡献它们的 root 与 depth，写入 `execution_additional_causes`，原始请求起因保留；执行上下文在发送模型前持久化，并传给⑦、业务写入与 Finish。changed、unchanged、not_applicable、failed 四种结果都有真实的 `FinishReevaluation`；成功的活动目标带完整依赖，完成标记与 run 更新原子提交。已知非法输出和模型前的重试耗尽可以 Finish 为 failed；failed 的请求不可变，再试需要新请求。
- **登记要求**：`rule_selection` 须由可信 core 登记，`artifact` 须启用重判与强制依赖。工作区里 ① 的 `core_mvp_registry()` 提供这个登记（见[①部署指南第 9 节](../storage/ai-docs/deployment.md)，新库专用）。`DEFAULT_REGISTRY` 与 `contracts/` 没有改；演示与测试夹具用 `demo.fixture_registry()` 显式开启。未登记时，`extract`、`reextract` 在调用模型前阻塞为 `rule_selection_registration_required`，不静默回落到 v1.1。
- **旧数据**：不迁移、不回填。旧活动没有选择结果，不会被自动补出；仍可按当前规则直接重判已有产物。基线上占用请求 ID 的旧记录、未被追踪或结果未知的调用仍然阻塞。

**第二批：外部动作 action-v2 的 ④ 侧（工作区，未提交）**：`Command` 增加内部的 `execution` 字段与 `executing`、`unknown` 结果，`suggestions.py` 在业务提交里校验建议状态流转：`approved → executing → executed | failed | unknown`，`unknown` 经查证或用户声明已执行变 `executed`，`unknown` 或 `failed` 经带风险确认的显式重试回到 `executing`（同一 `action_key`，attempt 加一）。校验动作哈希与批准的精确版本，状态不符一律阻塞而不是变基。旧的终态回执只对没有 v2 交接的建议有效。这是模块内附加 payload，没有改公共契约。执行方在 ⑥（[⑥入口](../api/README.md)第 3.6 节）；④ 不执行外部动作，也不保证外部动作不重复。

**验证（来源与日期；作者未复跑，只用 `pytest --collect-only` 与文件哈希核对；全部合成数据，不是当前结果）**：

- 2026-11-12T01:58Z（extraction-v12 摘要，基线 79e8dc3 加当时工作区）：模块单元 113 passed（基线 84 项加 29 项新增）；`services/storage/tests/test_extraction_v12_runtime.py` 1 passed（临时 PostgreSQL，NATS 关，回环 ① HTTP、合成网关，事件分发由测试显式驱动）；模块测试用真实 `GatewayClient` 加进程内网关、journal、① 预检与合成 provider，没有真实模型与网络。当时无开关整仓 2178 passed、2 failed（e2e 夹具缺 `rule_selection` 登记）。后续 04:14Z–04:25Z 的几份摘要里，外部开关全关的整仓快照没有失败，但那是共享工作区在各自时刻的快照，不是当前结果。
- 2026-11-12T04:20Z（action-protocol 摘要）：新协议测试 84 passed（⑥ 与 ④ 合计，内存存储、真实 ④ consumer 与私有 SQLite，不是 PostgreSQL/NATS）；API 加 ④ 共 533 passed。
- 作者 04:31Z 的 `--collect-only`：`services/extraction/tests` 148 项（只收集，未执行）；报告里记录的 14 个文件中，除 `domain.py`（之后因 action-v2 增补而变）外，哈希与当前一致。
- 79e8dc3 的首批记录（真实 PostgreSQL/NATS 17 项一次连续运行等）见[跑通计划第 2.2 节](../../ai-docs/plans/mvp-runnable.md)；完整的最终数字由主 agent 在统一验证后补入[交接第 7 节](../../ai-docs/plans/mvp-runtime-handoff.md)。

**没做**：网关输出恢复与业务计划重放；用户确认重复风险后的新逻辑调用；模型已返回有效输出、业务提交被拒或结果未知时，停为 `gateway_result_not_committed`，不算 failed、不自动重发；规则级并发保护偏保守，模型返回后可能因无关的并发规则编辑而阻塞；v1.2 的 PostgreSQL 测试没有经 NATS 与真实网关传输；真实模型联调；对网关、数据库、消息总线做操作系统级强杀的恢复验证；外部动作的 HTTP 执行与真实日历适配器（在 ⑥，且未接）。详见[接入限制](ai-docs/implementation.md)。
