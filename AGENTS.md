---
audience: ai
doc-type: guide
based-on: README.md；项目负责人授权的文档职责迁移及本轮已确认决定同步
reviewed-by: aytony
reviewed-at: 2026-10-30
---

# AGENTS.md：给 AI 编程工具的说明

仓库的全部规范写在 [README.md](README.md)，对人和 AI 同样适用。本文件不重复 README，只补充对 AI 的额外限制；两者冲突时以 README 为准，并报告冲突。

## 开始工作前

1. 阅读 `README.md`、`docs/README.md`，了解分类和决策状态。
2. 阅读 `docs/decisions/mvp-confirmed-decisions.md` 和 `ai-docs/plans/mvp-implementation.md`，区分已采纳目标与尚未同步的契约/实现。完整阅读相关已采纳设计；默认入口是 `docs/design/design-v1.2.md`、`docs/design/signals-supplement.md`，按任务查阅工作分解。
3. 阅读所改模块的 `services/<模块>/README.md`（若存在），再按需加载该模块的 `ai-docs/`、契约和代码。模块入口不存在时报告缺口，不猜测路径或职责。
4. 核对任务指定分支、工作区状态；读取提案时先看 `status`，不要把“合入 dev”当成“已采纳”。

## 修改与交付

- 按内容选择位置：功能、核心逻辑、重要决定在 `docs/`；具体实现、代码导航、测试和排障在最近的 `ai-docs/`。双方都可以由人或 AI 编写。
- 未获批准的行为变化写成 `status: proposed` 的提案，不自行标记 `accepted`。已获明确授权的文档迁移、实现及同步维护可以执行，交付时说明依据和影响范围。
- **不填写、不修改 `reviewed-by` / `reviewed-at`**，不把原有审核记录当成本次人工审核的证明。
- 元数据遵循 `docs/README.md`；工程上下文必须有 `based-on`，区分当前实现、计划和已知偏差。
- 不把业务规则只留在进度笔记里，不重复抄写已有契约；使用相对链接指向定义位置。
- 发现文档、契约、代码冲突时报告；未经相应决策授权，不为使其一致而静默修改产品语义。
- 不确定数据由哪个模块写、某个约定是否已采纳时，向负责人确认，不按猜测实现。
- 不直接 push 到 `dev` 或 `main`，不强推，不改写已推送的历史。
- 交付前运行 README 的全部提交前检查；文档任务还应检查迁移链接和 `--base origin/dev` 的人工复核清单。如实报告失败、未测项和仍需决策的事项。
