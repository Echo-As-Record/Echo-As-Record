---
audience: ai
doc-type: status
based-on: origin/dev 48d3c51 的代码与契约；docs/design/design-v1.2.md；docs/design/plugins-mvp.md；6cea0b7 实施准备（当时无实现改动）；storage/implementation-preparation 分支基于 6cea0b7 的 P0a 实现与实际测试结果；项目负责人（用户）采用推荐方案并授权功能分支实现（P0 契约提案 accepted）
---

# ① 实现状态与新目标差距

[设计](../../../docs/design/design-v1.2.md)、[插件](../../../docs/design/plugins-mvp.md)和[ADR](../../../docs/adr/0001-storage-and-runtime-modules.md)已采纳；采纳决定不等于已实现。设计同步那一轮没有改代码或契约；此后只有 P0a 回归修复的代码改动（本分支，见文末）。所有权及接口目标见[人读说明](../../../docs/modules/storage.md)。

## 已有代码

- contracts：记录、事件、依赖、注册表Schema和示例，部分DRAFT。
- ear_contracts：公共模型、UUIDv7、UTC、登记表及一致性测试。
- ear_storage_client：同步Protocol、批次操作、结果/冲突、查询、MemoryStorage、进程内事件工具。
- 基础内存行为测试覆盖去重、批次回滚、版本、版本级依赖、快照与重投；新目标尚不由这些测试保证。

## 已决定，尚未实现

| 目标 | 差距 |
|---|---|
| 独立服务、数据库和总线 | 无正式后端、迁移、Outbox/Relay、总线适配和实际Compose服务 |
| 字段依赖/强制登记/防环 | 当前主要按版本判断，不强制Update同批重登，没有新目标并发图校验；契约提案已 accepted（P0b），尚未实现 |
| 动态注册与可信权限 | 当前构造时extend不等于热注册，模块名绑定不等于部署认证；缺持久注册、全读路径权限和凭据生命周期 |
| 管理反馈 | 缺①管理消费者、可恢复阶段、管理状态和⑥页面/API |
| 完成记录与调度交接 | 缺③④及插件通用结果类别、多起因与请求关联的兼容契约 |
| 最新规则/纯Markdown/选择结果 | 主要属于③④⑤业务对齐；①提供所需类型、依赖及事务支持 |
| 插件多层/历史处理 | 缺启停与快照游标、恢复/补或跳、多层集成与权限撤销测试 |

## 下一步

按[全局实施计划](../../../ai-docs/plans/mvp-implementation.md)先对齐契约和共用行为测试，再持久化/鉴权/可靠投递，再动态管理和插件链集成。部署与I合作，页面归⑥，传播调度归⑤；不要让①包办业务模块。

原静态审查的四个边界（投递上限先出队、并发runner、空时间区间、系统记录更新时间校验）已在下文 P0a 中补回归测试并修复（提交与合并状态以分支提交历史和 PR 为准）。不要把 `test_memory.py::test_concurrent_writers`（顺序旧版本冲突）当真正并发测试。SDK用法与命令见[工程指南](interface.md)。

## 实施准备（历史记录，基线 6cea0b7）

准备阶段当时**无实现改动**（未改代码/测试/契约/部署）。首批PR拆分、证据与阶段出入口见[①实施准备](implementation-plan.md)，P0b契约选择见[P0契约提案](../../../docs/proposals/storage-p0-contracts.md)（准备阶段为 proposed，现已 accepted）。上文“已有代码”按48d3c51记录，6cea0b7未改代码。这一段只描述准备阶段，不代表此后没有代码改动。

## P0a：四项回归修复（本分支已验证，提交历史为准）

在 `storage/implementation-preparation` 分支基于 6cea0b7 实现并验证；本页不记录提交 SHA，提交、推送和 PR 状态以分支提交历史与 PR 为准。**尚未经合并前人工审核**：审核由负责人兼 I 在合并前进行，不由 AI 代填 reviewed 字段。没有改 contracts、公共签名、依赖或部署。

| # | 修复 | 位置 |
|---|---|---|
| 1 | 投递上限在出队前检查；超限抛 RuntimeError 时未投递的事件留在队列，下次调用补投 | `memory.MemoryBus.run_until_idle` |
| 2 | 每个 consumer 有内部在途标记：多个 runner（含 handler 内嵌套调用）不并发处理同一 consumer；重投/重复投递的事件在释放标记前放回队列，保持顺序；handler 在总线锁外执行；标记在 `finally` 中释放（包括 BaseException） | `memory._Subscription.busy`、`MemoryBus.run_until_idle` / `_deliver` |
| 3 | `TimeRange(t, t)` 不命中任何数据（仍允许构造） | `queries.TimeRange.overlaps` |
| 4 | 系统记录更新用 `_build(SystemRecord, ...)` 重新校验，先校验再判断“无变化”；非 UTC 时间或无效 clock 使整批回滚；created_at 保留，时间规范化为 UTC；无变化不加 revision、不改 updated_at、不发事件 | `memory._Txn.put_system` |

测试：`libs/ear_storage_client/tests/test_memory_regressions.py`（12 项）。

- 修复前：11 failed / 1 passed。通过的是 BaseException 释放标记的守护用例：旧代码没有在途标记，所以它本来就会通过。
- 修复后：12 passed；该文件连续运行 60 次都通过；全量 `pytest` 301 passed（原 289 + 新 12）；ruff、format、pyright、check_docs 及 `git diff --check` 均通过。
- 行为边界：某个 runner 遇到正被其他 runner 处理的 consumer 时直接跳过（不等待），可能在队列尚未清空时返回，由持有该 consumer 的 runner 继续投递。handler 以 BaseException 退出时，该事件仍按原语义丢失，只保证释放标记。
- 未覆盖：PostgreSQL、多进程、持久总线；同时传入 naive 与 aware 时间时 `time_range` 的比较异常（新建和更新路径都有，未改）。

P0b（字段依赖、登记表能力、批末强制）的[契约提案](../../../docs/proposals/storage-p0-contracts.md)已 accepted（依据：用户采用推荐方案并授权在功能分支持续实现、分批提交、推送和提 PR；合并前审核由用户与 I 负责，不是审核已完成），已获实现授权，但**尚未实现**。

未完成项不得因文档检查通过而标为完成；进度更新必须对应实际提交和测试结果。
