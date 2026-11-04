---
audience: ai
doc-type: guide
based-on: README.md；docs/README.md；本轮已确认设计v1.2；代码基线origin/dev 48d3c51
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
| 全局契约/实施顺序 | [实施计划](plans/mvp-implementation.md) |
| 原结构提案 | [历史参考](proposals/module-internal-structure.md)，已superseded，不照搬旧API与职责 |
| 原依赖提案 | [历史来源](../docs/proposals/dependency-refinement.md)，最终以设计及确认索引为准 |

## 维护

写具体代码路径、符号、命令、失败边界和验证，不堆积对话全文。based-on记依据与代码基线；设计规则引用人读文档，别建立第二份产品规范。尚未冻结的Schema/传输格式作为工程方案评审，修改公共契约仍需①与I审核。若影响已批准行为再请负责人决定，普通实现细节不用无限追加问答。
