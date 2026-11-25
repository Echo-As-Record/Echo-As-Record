---
audience: human
doc-type: proposal
status: proposed
generated-by: Claude (AI)，应 YijieYing（③④代理负责人）要求起草
generated-at: 2026-11-25
based-on: docs/design/design-v1.2.md 3.5、6、7.1、7.2、8.1 节；docs/decisions/mvp-confirmed-decisions.md C07；contracts/registry/data-types.json（origin/dev 5200899，person/attribution 由③写，activity 由④写）；PRTS pulse 分诊的 high 定义与统计（仓库外：312 段中 21 段 high；high 指“用户本人在口述想法、计划或决定”，经 YijieYing 与 PRTS 总控 11-22 转达）；YijieYing 2026-11-20 关于“判定是否推送的字段能否加入④输出”的提问与 11-22、11-25 的分工决定（客观判断放④，推不推送由送达环节决定；③优先）
---

# 提案骨架：活动重要性评估（`activity_assessment`）

> **状态：proposed，未采纳，只是骨架。** 本文只定下记录的形状、依赖和边界，**不能在③完成声纹匹配校准、`person.is_self` 被采纳并实现之前实现**。判定标准和提示词留待那时补全。新增类型采纳后仍需①负责人与 I 审核。

## 1. 为什么单独成一条记录

- 判断重要性要知道“是不是用户本人在说”，这需要③的归属和“哪个人物是用户本人”。
- [C07](../decisions/mvp-confirmed-decisions.md) 规定活动划分、名称和描述不读归属。所以重要性不能放进 `activity` 本身。
- 单独成一条记录后，归属一改，⑤只请求重判这条评估，不重划活动。

## 2. 记录形状

新增语义类型 `activity_assessment`（暂名），④单写，每个活动一份当前记录：

| 字段 | 说明 |
|---|---|
| `activity_id`、`activity_version` | 被评估的活动 |
| `importance` | `high` / `normal` / `low` |
| `reason` | 一两句理由，直接写名字，不存 id 引用标记（与[设计 §7.5](../design/design-v1.2.md) 一致） |
| `evidence_ids` | 支撑判断的原始记录 id（各句证据的并集），过[结果审核](extraction-result-audit.md)的确定性检查 |
| `evidence_index` | `reason` 中各句到证据 id 的索引，模型不输出原话，规则同结果审核提案第 3.3 节 |
| `review` | 可选，`high` 时的独立复核结论，见结果审核提案第 4 节 |
| `method` | 判定方法与版本 |

`high` 的含义沿用 pulse：**用户本人是主要说话人，并且在口述想法、计划或决定**。它不是一般意义上的“重要”。`normal`、`low` 的边界和反例在实现前与用户确认（pulse 现有反例可作起点，在仓库外）。

## 3. 依赖

- 活动本身。
- 活动时间范围内、被判断用到的归属（声纹 → 人物）。
- 所读人物的 `is_self` 字段；写进 `reason` 的人物 `name` 字段。
- 所读转写及其纠错（见[纠错提案](transcript-correction.md)）。

归属被纠正、`is_self` 改变、人物改名时，⑤请求重判评估；结果不变时不新增版本，仍提交完成记录。活动被整体替代时，评估随之整体替代。

## 4. 实现前提（必须先满足）

1. ③的声纹匹配完成校准：在用户标注的样本上，本人召回和误认率达到用户定的线。没有这一步，“用户本人在说”这个输入不可靠，评估会系统性出错。
2. `person.is_self` 由③提案定义并实现：用户指定，至多一个人物。目前没有这个字段，也还没有对应提案。
3. 纠错层与结果审核至少完成确定性部分，以便 `reason` 和原话可核对。

在这些满足之前，④不写 `activity_assessment`，也不在 `activity` 或产物里放替代字段。

## 5. 不在④做的事

- **推送决策不在④。** ④只给出客观判断；推不推送、什么时候推，由推送方决定（先是仓库外的 pulse/reporter，以后可以是插件），条件例如：`high` 且复核 `supported`、未迟到、每日上限、同一件事只推一次。
- ④不按重要性跳过活动或产物生成；重要性不参与规则选择，除非以后另提。

## 6. 与 pulse 的过渡

pulse 现在自己做分诊（312 段中 21 段 high）。按 R24，③④上线后 pulse 改读活动层、去掉自己的分诊。实现后先在同一批录音上对照 pulse 的 high 结果，由用户判定差异，再决定 pulse 何时停用分诊。

## 7. 待决

1. 类型名和“每个活动一份”是否合适。
2. `normal` 和 `low` 的定义。
3. 评估在什么时候生成：随提取一起，还是在归属就绪后单独排队。
4. 推送方读取评估的方式（直接读①，还是经⑥）。
