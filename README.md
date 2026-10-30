---
status: draft
reviewed-by:
reviewed-at:
---

# Ear（Echo As Record）

面向个人部署的活动建模服务：收集录音、GPS、手机信息等个人数据，把每一天整理成一连串活动，并按用户的自然语言规则输出笔记、待办、日历和提醒事项。用户纠正一处错误后，受影响的结果自动修正。

本文件是仓库的总规范，人和 AI 都要遵守。人类维护者读完本文件和下面三份设计文档，就可以开始工作。

## 1. 先读这些

- [设计文档 v1.1](docs/design/design-v1.1.md)
- [模块间信号（补充章节）](docs/design/signals-supplement.md)
- [MVP 工作分解 v1.1](docs/design/work-breakdown-v1.1.md)
- 所负责模块的 `services/<模块>/README.md`

[AGENTS.md](AGENTS.md) 是写给 AI 编程工具的入口，只在本文件之上补充对 AI 的额外限制，人不必阅读。

## 2. 目录

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

基础处理模块和 Octopus 在各自的仓库中维护，Ear 按固定版本引用。

## 3. 开发环境

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run pre-commit install
```

提交前检查（CI 跑的是同一组命令）：

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && uv run python tools/check_docs.py
```

## 4. 文档分级

| 级别 | 位置 | 含义 |
|---|---|---|
| 已审定 | 除 `ai-docs/` 外的所有 Markdown：`docs/`、本文件、`AGENTS.md`、`services/<模块>/README.md` 等 | 有人完整读过、理解并认可，front matter 写明 `reviewed-by`、`reviewed-at` |
| 机器验证 | `contracts/`（JSON Schema）及其测试 | 语义以 `docs/` 为准；字段细节由 CI 和测试保证 |
| 工作文档（未审定） | 任意层级的 `ai-docs/` 目录 | 还没有人对内容负责；未审定不等于不可信 |

- 只看路径判断：**路径中有 `ai-docs/` 的是未审定的，其余 Markdown 都是已审定的**（`.github/` 模板除外）。
- 同一个文件不混合两种状态。修改已审定文档必须走 PR，由审定人完整审核后更新 `reviewed-by` / `reviewed-at`。
- "完整审核"指审定人逐节读过，能讲清楚核心内容并认可。
- 人没有义务阅读 `ai-docs/`；其中的文档要成为已审定文档，按 [ai-docs/README.md](ai-docs/README.md) 的升格流程办。
- `tools/check_docs.py` 在 CI 里检查以上规则。

**冲突时以谁为准：** `docs/` > `contracts/` 及测试 > 代码 > `ai-docs/`。已审定文档与代码冲突时，先报告、讨论，再决定改哪一方，不要顺手改掉其中一方。

## 5. 人与 AI 的协作

- **人对进入 `main` 的一切负责。** PR 作者对提交的内容负责，不论是否由 AI 生成；审核人按同一标准审核。
- AI 可以写代码、测试和 `ai-docs/` 中的文档。AI 生成的说明文档一律放进 `ai-docs/`，front matter 写明 `generated-by`、`generated-at`、`based-on`。
- AI 不主动修改已审定文档，修改提议写在 `ai-docs/proposals/` 或 PR 描述里。人可以让 AI 起草对已审定文档的修改，但合并前必须由审定人完整审核。
- `reviewed-by` / `reviewed-at` 只能由审定人本人填写，不能交给 AI 代填。
- AI 发现文档之间、文档与代码之间的冲突时只报告，不自行修改；由人决定改哪一方。
- 对 AI 的额外限制写在 `AGENTS.md`。协作规则有变化时改本文件；`AGENTS.md` 只在 AI 专属规则变化时修改。

## 6. 架构硬约束（摘自设计文档，细节以原文为准）

- **每类数据只有一个模块负责写**（设计文档 5.1，补充章节第 7 节对 ⑤ 的补充）。不要在别的模块里写不归自己的数据。
- 原始数据只追加；语义信息 id 不变，修改即新增版本，旧版本标记为 `superseded`。
- 模块之间不直接互相下指令：只写 ①，由 ① 发「数据库被修改」；唯一例外是 ⑤ 发「请求重新判断」。
- 收到事件**先查当前状态**再决定是否处理；同一事件送达多次，结果和送达一次相同。
- 内容没有变化时不写入、不发事件。
- 读写数据库（①）、调用大模型（⑦）直接调用，不经过总线。所有大模型调用必须经过 ⑦，并带调用方模块名和 `root_feedback_id`。
- 写入时原样带上触发它的 `root_feedback_id` 和 `depth`。
- 核心数据不能依赖扩展数据；⑤ 按登记表分发，不在代码里写死接收方。

## 7. 公共约定（草案，以 `contracts/` 和审定后的术语表为准）

- 代码标识用英文，见 `ai-docs/proposals/glossary.md`（待审定后移入 `docs/`）。
- id：UUIDv7，小写字符串。版本号：整数，从 1 开始。
- 时间：一律 UTC，RFC 3339，以 `Z` 结尾；时间区间为半开区间 `[start, end)`。
- 系统发起的行为，`root_feedback_id` 为字面量 `"system"`。
- `contracts/` 中的字段**只能增加，不能删除或改义**；修改 `contracts/` 需要 ① 负责人与 I 共同审核。
- 日志用 JSON 结构化日志，每条带 `module`、`event_id`、`root_feedback_id`、`depth`。

## 8. 技术栈与选型

- 后端统一 Python 3.12，uv 管理依赖（仓库根 `pyproject.toml`，模块建好后加入 uv workspace）。
- 代码风格与检查：ruff（lint + format）、pyright、pytest。
- 模块内部的选型（数据库、总线、算法、提示词等）由负责人决定，重要决定写成 `docs/adr/NNNN-<标题>.md`。
- CI 中涉及大模型的测试使用录制好的回复，不调用真实模型。
- MVP 不做客户端（⑥ 只做服务端 API）；原型阶段的客户端平台另行决定。

## 9. Git 与 PR

- `main` 为保护分支，只通过 PR 合并（squash）；不改写已推送的历史，不强推。
- 分支名：`<模块>/<主题>`，例如 `attribution/voiceprint-matching`、`docs/glossary`。
- 提交信息：Conventional Commits，scope 用模块名，例如 `feat(extraction): 定时提取跳过已提取时间段`。
- CODEOWNERS 指定的负责人审核对应目录；PR 按模板逐项勾选。

## 10. 数据与凭据

- **真实录音、GPS、手机信息、D2 标注一律不进仓库**（含真实人名，伦理审查未定）。仓库只放 D1 脚本、标准答案和合成的假数据（`fixtures/`）。
- 凭据只放在 `deploy/.env`（不进仓库），模板见 `deploy/.env.example`。pre-commit 中的 gitleaks 负责拦截。
