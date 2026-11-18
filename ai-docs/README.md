---
audience: ai
doc-type: guide
based-on: README.md；docs/README.md；本轮已确认设计v1.2；代码基线origin/dev 48d3c51（原有条目）；2026-11-11增补跑通计划入口时origin/dev为c5c31f8，只核对了新增条目与本文件的本地链接，未逐条复核其余条目；2026-11-12增补运行时交接入口行；2026-11-18限定同步：HEAD84d2d0f已含origin/dev ba36543/PR20及GPS，新evidence/flat workspace与独立空部署的磁盘证据；仅新增/更正相关导航，其余条目仍为历史基线
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
| MVP 第二批切片的历史运行时交接：初始化、worker与各时点验收 | [运行时交接](plans/mvp-runtime-handoff.md)（历史快照；“第二批未提交/PR16 CI不含”不描述当前HEAD） |
| 2026-11-18 当前事实：HEAD84d2d0f含ba36543/PR20与GPS；真实evidence API、flat只读Web、独立空运行时及验证/重启边界 | [事实与恢复交接](plans/real-sample-web-mcp.md)（默认3209通过/862跳过；GPS定向10通过但输入/模型/效果合成，范围不相加；无最新四开关；无真实数据导入，源Evidence/embedding授权未证明；最终API重部署已核对完成；helper更新回执采纳/重启恢复未完） |
| MCP只读优先、权限/外发/确认与启用门槛 | [MCP提案](../docs/proposals/mcp-interface.md)（proposed；未安装/接线，不因Web/API已部署而视为MCP交付） |
| 原结构提案 | [历史参考](proposals/module-internal-structure.md)，已superseded，不照搬旧API与职责 |
| 原依赖提案 | [历史来源](../docs/proposals/dependency-refinement.md)，最终以设计及确认索引为准 |

## 维护

写具体代码路径、符号、命令、失败边界和验证，不堆积对话全文。based-on记依据与代码基线；设计规则引用人读文档，别建立第二份产品规范。尚未冻结的Schema/传输格式作为工程方案评审，修改公共契约仍需①与I审核。若影响已批准行为再请负责人决定，普通实现细节不用无限追加问答。
