---
audience: ai
doc-type: guide
based-on: README.md；docs/README.md；本轮已确认设计v1.2；代码基线origin/dev 48d3c51（原有条目）；2026-11-11增补跑通计划入口时origin/dev为c5c31f8，只核对了新增条目与本文件的本地链接，未逐条复核其余条目；2026-11-12增补运行时交接入口行，同样只核对新增条目与本文件的本地链接
---

# AI 工程上下文索引

这里保存实现上下文，目录不表示未审定或只能由AI起草。目标与代码有差距时明确记录，不把当前实现当成新设计，也不把accepted当作已经实现。

## 阅读顺序

1. [AGENTS](../AGENTS.md)与[文档规则](../docs/README.md)。
2. [设计 v1.2](../docs/design/design-v1.2.md)、[信号](../docs/design/signals-supplement.md)、任务相关[插件设计](../docs/design/plugins-mvp.md)及[ADR](../docs/adr/0001-storage-and-runtime-modules.md)。
3. [决策来源](../docs/decisions/mvp-confirmed-decisions.md)和[实施计划](plans/mvp-implementation.md)。
4. 当前契约、代码、测试和所改模块工程指南；不要批量读取所有无关文件。

| 内容 | 入口 |
|---|---|
| ①人读目标 | [模块说明](../docs/modules/storage.md) |
| 当前SDK用法 | [工程指南](../services/storage/ai-docs/interface.md) |
| ①代码差距 | [状态记录](../services/storage/ai-docs/progress.md) |
| ①隔离部署与备份交接 | [部署指南](../services/storage/ai-docs/deployment.md)（非生产运行手册） |
| ②实现、导入规则与验收 | [实现说明](../services/adapters/ai-docs/implementation.md)、[payload 字段](../services/adapters/ai-docs/payloads.md) |
| ⑥ API 入口与实施计划 | [模块入口](../services/api/README.md)、[实施计划](../services/api/ai-docs/implementation.md) |
| ⑦ 大模型入口与实施计划 | [模块入口](../services/llm_gateway/README.md)、[实施计划](../services/llm_gateway/ai-docs/implementation.md) |
| ⑤ 依赖分析入口与实现设计 | [模块入口](../services/dependency/README.md)、[实现设计草案](../services/dependency/ai-docs/design.md)、[接口提案](../docs/proposals/dependency-scheduler-interfaces.md) |
| 全局契约/实施顺序 | [实施计划](plans/mvp-implementation.md) |
| MVP 跑通：阶段门槛、首批任务、演练维度（输入/模型/外部适配器/平台）、真实数据边界与授权范围 | [跑通计划](plans/mvp-runnable.md) |
| MVP 第二批工作区切片的运行时交接：切片边界、初始化与 worker 说明、最终验收证据、提交与门槛状态（待主 agent） | [运行时交接](plans/mvp-runtime-handoff.md)（工程交接，不是完成报告；第二批未提交，PR 16 的 CI 不含它） |
| 原结构提案 | [历史参考](proposals/module-internal-structure.md)，已superseded，不照搬旧API与职责 |
| 原依赖提案 | [历史来源](../docs/proposals/dependency-refinement.md)，最终以设计及确认索引为准 |

## 维护

写具体代码路径、符号、命令、失败边界和验证，不堆积对话全文。based-on记依据与代码基线；设计规则引用人读文档，别建立第二份产品规范。尚未冻结的Schema/传输格式作为工程方案评审，修改公共契约仍需①与I审核。若影响已批准行为再请负责人决定，普通实现细节不用无限追加问答。
