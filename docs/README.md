---
audience: human
doc-type: guide
---

# 文档导航与维护规则

## 1. 先理解系统，再阅读实现

`docs/` 让人理解系统做什么、为什么、边界在哪里、怎样验收；`ai-docs/` 让 AI 开发时找到代码、理解实现和完成验证。分类与作者无关，也不代表可信程度。人的审核不需要通读全部实现文档，AI 则必须阅读与任务相关的设计。

### 项目负责人和模块负责人

1. [项目总规范](../README.md)：协作、开发和提交规则。
2. [设计 v1.2](design/design-v1.2.md)：当前产品行为与核心逻辑。
3. [信号补充](design/signals-supplement.md)：模块通信和处理条件。
4. [工作分解](design/work-breakdown-v1.2.md)、[术语表](glossary.md)：职责、验收和公共标识。
5. ①相关：[存储职责与接口语义](modules/storage.md)。
6. [已确认决策](decisions/mvp-confirmed-decisions.md)、[插件 MVP](design/plugins-mvp.md)、[架构 ADR](adr/0001-storage-and-runtime-modules.md)。原[审议清单](proposals/review-agenda.md)已转为结果导航。

### AI 开发入口

阅读 [AGENTS.md](../AGENTS.md)，再从 [工程上下文索引](../ai-docs/README.md) 按需加载模块资料。不要一次加载全部文档，也不要跳过设计只依据代码猜测需求。

## 2. 放在哪里

| 内容 | 位置 | 示例 |
|---|---|---|
| 功能、核心逻辑、模块边界、验收 | `docs/design/`、`docs/modules/` | 依赖什么时候过时、谁能改规则 |
| 待决的产品或跨模块变化 | `docs/proposals/` | 尚未获批准的行为变化；已决提案保留历史并链接当前设计 |
| 重要架构决策及取舍 | `docs/adr/` | 存储是服务还是库、数据库和总线选型 |
| 面向人的模块入口 | `services/<模块>/README.md` | 模块职责和设计/实现入口，不重复完整设计 |
| 工程导航、实现和测试 | 根或模块的 `ai-docs/` | 表结构、函数定位、故障排查 |
| 机器验证的公共结构 | `contracts/` | Schema、正反例；行为含义由设计解释 |

跨模块 API 的行为保证给人读；参数、代码示例、实现路径给 AI 读。必要时拆分并双向链接，不以“很技术”为由把核心规则藏进工程笔记。

## 3. 元数据和决策状态

除 `.github/` 的 issue/PR 模板外，Markdown 使用简单的单行 front matter：

| 字段 | 要求 |
|---|---|
| `audience` | `human` 或 `ai`；`docs/` 与普通 README 为 human；任意层级 `ai-docs/` 及根 `AGENTS.md` 为 ai |
| `doc-type` | `design`、`proposal`、`decision`、`reference`、`guide`、`status` |
| `status` | design/proposal/decision 必填：`proposed`、`accepted`、`superseded`；其他类型可省，有值也须使用这三者 |
| `superseded-by` | status 为 superseded 时必填，指向相对本文的仓库内替代文件 |
| `based-on` | AI 文档必填，人读汇总建议填写；自由文本注明依据文档、代码提交或授权来源，不作为自动路径校验 |
| `generated-by`、`generated-at` | 可选的起草来源，不决定目录与效力 |
| `reviewed-by`、`reviewed-at` | 可选且成对；保留历史记录，只由本人填写，日期为 YYYY-MM-DD |

- **proposed**：尚未采纳，不覆盖已采纳行为；进入 `docs/` 或合入 `dev` 不改变状态。
- **accepted**：负责人已明确采纳。采纳依据应在文档或 PR 中可追溯；不能靠 AI 改字段制造批准。
- **superseded**：不再作为当前决定；保留替代关系，避免读到旧决定后照做。
- guide/reference/status 是导航、说明或现状，不因省略 status 而自动成为生效规范。混合“目标/现状/计划”时必须分节标清。
- 原有 reviewed 字段保留原值。新正文的审核通过 PR 完成，不因继承旧日期就视为已审。

校验器只支持简单单行 `key: value`，不要使用多行 YAML 或复杂集合。文档正文仍可正常使用 Markdown。

## 4. 修改流程

1. 判断是功能/核心语义变化，还是不改义的说明维护、实现同步。
2. 前者先形成 proposed 提案，写明现状、变化、影响模块和验收；负责人明确批准后再同步设计、契约、实现、测试。
3. 后者在授权任务范围内直接维护相应文档，避免重复定义。AI 可以起草人读文档。
4. 将未解决的设计/代码偏差写明，不通过“迁移文档”悄悄决定产品行为。
5. 更新相对链接并运行全部提交前检查。`--base origin/dev` 输出人工复核清单，不再强制刷新历史审核字段。

链接检查只验证支持的本地 inline 链接是否指向仓库内文件，跳过外链、页内 anchor、受支持的代码块和 inline code；不检查标题 anchor，也不是完整 Markdown 解析器。语法范围见 `tools/check_docs.py` 的说明。CI 不验证人工是否真正理解或批准了设计。

## 5. 两次变更的边界

第一阶段已调整文档职责、入口、元数据与校验，没有采纳功能变化。第二阶段在负责人逐项确认并明确要求同步后，才更新当前目标：新设计与工作分解 v1.2、插件 MVP、ADR、信号和术语。

- 原设计/工作分解 v1.1 标为 superseded 并链接新版，历史正文和 reviewed 字段保留。
- 新版 accepted 的依据是[本轮确认索引](decisions/mvp-confirmed-decisions.md)，不意味着代码已实现或新正文已人工逐字复审。
- 旧提案保留其原始讨论并标 superseded；最终采用的边界以当前设计为准，不把历史原稿全部追认为正式设计。
- contracts、业务代码、部署尚未因本轮同步改变；当前差距和后续改动见[实施计划](../ai-docs/plans/mvp-implementation.md)。
- 人读文档定义目标，AI指南保留准确的当前SDK用法，不能让“已采纳”与“已实现”混淆。
