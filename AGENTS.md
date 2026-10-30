---
reviewed-by: aytony
reviewed-at: 2026-10-30
---

# AGENTS.md：给 AI 编程工具的说明

仓库的全部规范写在 [README.md](README.md)，对人和 AI 同样适用。本文件不重复 README，只补充对 AI 的额外限制；两者冲突时以 README 为准，并报告冲突。

## 开始工作前

完整阅读：

1. `README.md`
2. `docs/design/design-v1.1.md`、`docs/design/signals-supplement.md`
3. 所改模块的 `services/<模块>/README.md`

## 额外限制

- **已审定文档**（路径中不含 `ai-docs/` 的 Markdown）：不主动修改。只有人明确要求时才修改，并在交付说明里写明改了哪些已审定文档，提醒需要审定。
- **不填写、不修改 `reviewed-by` / `reviewed-at`**，即使被要求也不代填。
- 自己生成的说明文档放进最近的 `ai-docs/`，front matter 写 `generated-by`、`generated-at`、`based-on`（格式见 `ai-docs/README.md`）。
- 发现冲突（文档之间、文档与代码之间）时只报告，不修改任何一方。
- 不确定某类数据由哪个模块写、某个约定是否已定时，停下来问人，不要按猜测实现。
- 不直接 push 到 `dev` 或 `main`，不强推，不改写已推送的历史。
- 交付前运行 README「开发环境」中的提交前检查，并如实报告结果，包括没有通过的项。
