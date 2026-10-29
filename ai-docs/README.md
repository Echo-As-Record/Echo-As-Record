# ai-docs：工作文档（未审定）

这里放**还没有经过人完整审定**的文档：AI 生成的接口细节说明、实现方案、调研记录，也包括人写的随手笔记。

- 未审定不等于不可信。它的意思是：**还没有人对它的内容负责**。
- 人没有义务阅读这里的内容。这里的文档可以随时删除，也可以重新生成。
- 和已审定文档（`docs/`、`README.md`、`AGENTS.md`、模块 README）冲突时，以已审定文档为准，并把冲突报告出来。
- 各模块自己的工作文档放在 `services/<模块>/ai-docs/`。

## 建议的 front matter

```yaml
---
generated-by: Claude (AI)        # 或作者名
generated-at: 2026-10-29
based-on: docs/design/design-v1.1.md (reviewed-at 2026-10-26), commit abc1234
---
```

`based-on` 记下生成时依据的已审定文档版本。依据更新后，可以据此判断这份文档是否已经过时。

## 升格为已审定文档

1. 人完整阅读并认可。
2. 在 PR 里把文件移到 `docs/` 下，删掉 `generated-*`，加上 `reviewed-by`、`reviewed-at`。
3. CI 的文档审定检查通过后合并。

## 目录

- `proposals/`：等待讨论、准备升格的提案。
