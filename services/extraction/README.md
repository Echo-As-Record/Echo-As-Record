---
audience: human
doc-type: guide
based-on: 设计 v1.2；c5c31f8 的④实现；本轮负责人授权④直接依赖⑦ GatewayClient；工作区未提交的④首片（orchestration.py、call_safety.py、domain.py、model_io.py、planning.py、worker.py、demo.py 与相应测试，作者读 git diff 与源码核对）；任务状态文件 /tmp/ear-mvp-extraction-followup.json 与 /tmp/ear-mvp-extraction-state.json（非仓库文件，其中的测试作者未复跑，只用 pytest --collect-only 核对项数）；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核；未填写或改动任何 reviewed 字段
---

# ④ 活动提取

负责活动、规则、Markdown 产物及外部动作建议。**当前是 v1.1 演示基线，外加工作区里一个未提交的“调用安全与重判完成”首片；不表示已完成 v1.2。**

- [目标设计](../../docs/design/design-v1.2.md)、[模块间信号](../../docs/design/signals-supplement.md)。
- [使用与接入](ai-docs/implementation.md)：演示命令、宿主注入示例、契约差距、工作区首片的代码导航与限制。
- [网页演示](ai-docs/web-demo.md)：合成数据和固定模型回复。
- [MVP 跑通计划第 2.1 节](../../ai-docs/plans/mvp-runnable.md)：②③④⑤ 工作区进展的汇总与验证状态。

模型端口保留 `ModelGateway`，实际 HTTP 客户端统一使用 workspace 包 `ear-llm-gateway` 的 `GatewayClient`，构造时传④的 `SYSTEM`。客户端仍由⑦维护，协议见[⑦入口](../llm_gateway/README.md)。演示仍用测试替身。

## 状态

**c5c31f8 基线（已合入 dev）**：`GatewayClient.complete(Call)` 每次都生成新的 call_id。传输错误 `gateway_outcome_unknown` 可能已经执行，不能自动换新 id 重试；可恢复流程须发送前持久化 call_id 并用 `reconcile` 核对，不能靠重新调用 complete 恢复。基线上④的任务自动重试、存储冲突重算与租约恢复没有适配此语义，重判也缺请求关联字段，且 `extraction_run` 占用了请求的 event_id，使 ① 的 Finish 无法写入。基线只是统一了客户端，没有完成安全恢复或真实模型联调。

**工作区首片（未提交、未提 PR，不是完整 v1.2）**：

- **调用安全**：每次发送模型调用前，先把稳定的 `call_id` 和规范化请求的 SHA-256 以版本 CAS 写进 `extraction_run`；可能已经执行的调用（超时、断连、非 200、发送后崩溃、租约过期）一律停止为“结果未知”，**不自动换新 id、不重发**，重启、重复投递、显式重试和定时区间放大都绕不过；`reconcile` 只补审计记录，不取回回复，也不解除阻塞。没有实现网关输出恢复、业务计划重放，也没有“用户确认重复风险后重新生成”。
- **重判与完成**：重判运行的 run id 与请求 event_id 解耦（确定性派生），请求的 root、depth、`request_event_id` 和全部额外起因原样传给⑦与存储写入；对 artifact 重判，业务写入、完整依赖、`FinishReevaluation`、`extraction.completion` 标记和 run 成功在同一批提交，无变化也提交完成结果但不新增 artifact 版本、不发业务事件。基线上占用了请求 ID 的旧记录不改写、不迁移，未完成的会被阻塞。
- **远程订阅下的 worker**：`tick` 在客户端支持 `poll_once` 时先拉取一条 `extraction.jobs` 投递（远程 `subscribe` 只注册处理函数，之前没有人轮询）。
- **登记表没有变**：核心登记表里 `artifact` 的重判与强制依赖能力仍是关闭的（跑通计划的 E2，待 ① 负责人与 I 审核）；演示与测试夹具各自显式打开，不改核心登记表。

**验证（来源：任务记录，作者未复跑；作者只用 `pytest --collect-only` 核对到相同项数）**：记录于 2026-11-11T20:05Z 的任务称，extraction 单元测试 84 项通过；真实 PostgreSQL/NATS 加回环 ⑦ 网关与 ① HTTP 的测试 17 项通过，分 8 组不相交的选择运行，不是一次连续的完整运行，模型提供方是进程内的合成实现。输入合成｜模型回放｜外部不适用｜平台真实（测试夹具起的临时容器）。同一记录里的全仓 pytest、ruff、pyright、`check_docs` 当时有失败，位置在其他模块并行修改的文件，属于历史结果，不是当前状态。

**没做**：完整 v1.2（纯 Markdown、`rule_selection`、按当前规则重判、字段依赖）；历史规则出新版本时仍以 `historical_rule_dependency_unsupported` 阻塞；远程运行入口、部署、真实模型联调；终态 failed/not_applicable 的 Finish；旧记录迁移；对网关、数据库、消息总线做操作系统级强杀的恢复验证。外部动作（建议审核与执行）不在本首片内。详见[接入限制](ai-docs/implementation.md)。
