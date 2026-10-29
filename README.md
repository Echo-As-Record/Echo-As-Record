---
status: draft
reviewed-by:
reviewed-at:
---

# Ear（Echo As Record）

面向个人部署的活动建模服务：收集录音、GPS、手机信息等个人数据，把每一天整理成一连串活动，并按用户的自然语言规则输出笔记、待办、日历和提醒事项。用户纠正一处错误后，受影响的结果自动修正。

## 先读这些

- [设计文档 v1.1](docs/design/design-v1.1.md)
- [模块间信号（补充章节）](docs/design/signals-supplement.md)
- [MVP 工作分解 v1.1](docs/design/work-breakdown-v1.1.md)
- [协作规则 AGENTS.md](AGENTS.md)：文档分级、架构硬约束、Git 规范

## 目录

| 路径 | 内容 |
|---|---|
| `docs/` | 已审定文档：设计、ADR（`docs/adr/`） |
| `ai-docs/` | 工作文档（未审定），说明见 [ai-docs/README.md](ai-docs/README.md) |
| `contracts/` | 模块间约定的 JSON Schema 与示例 |
| `services/` | ①～⑦ 各模块：`storage`、`adapters`、`attribution`、`extraction`、`dependency`、`api`、`llm_gateway` |
| `libs/` | 共享库（SDK 等） |
| `fixtures/` | 合成的假数据 |
| `tests/` | 仓库级测试；`tests/e2e/` 为端到端 8 步流程 |
| `deploy/` | docker compose 与环境变量模板 |
| `tools/` | 仓库工具（如文档审定检查） |

## 开发环境

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run pre-commit install
uv run pytest
uv run python tools/check_docs.py
```

基础处理模块和 Octopus 在各自的仓库中维护，Ear 按固定版本引用。
