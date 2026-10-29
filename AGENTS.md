---
status: draft
reviewed-by:
reviewed-at:
---

# AGENTS.md：协作规则（人与 AI 都要遵守）

## 1. 文档分级

| 级别 | 位置 | 含义 |
|---|---|---|
| 已审定 | `docs/`、`README.md`、`AGENTS.md`、`services/<模块>/README.md` 等，除 `ai-docs/` 外的所有 Markdown | 有人完整读过、理解并认可，front matter 写明 `reviewed-by`、`reviewed-at` |
| 机器验证 | `contracts/`（JSON Schema）及其测试 | 语义约束以 `docs/` 为准；字段细节由 CI 和测试保证 |
| 工作文档（未审定） | 任意层级的 `ai-docs/` 目录 | 还没有人对内容负责；未审定不等于不可信 |

- 判断规则只看路径：**路径中有 `ai-docs/` 的是未审定的，其余 Markdown 都是已审定的**（`.github/` 模板除外）。
- 同一个文件不混合两种状态。往已审定文档里加入未经审核的内容，必须通过 PR，由审定人完整审核并更新 `reviewed-by` / `reviewed-at`。
- "完整审核"指审定人逐节读过，能讲清楚核心内容并认可。
- `tools/check_docs.py` 在 CI 里检查以上规则。

**冲突时以谁为准：** `docs/` > `contracts/` 及测试 > 代码 > `ai-docs/`。
发现 `ai-docs/` 与已审定文档冲突：以已审定文档为准，并报告冲突。
发现已审定文档与代码冲突：**报告，不要自行修改任何一方。**

## 2. AI 编程工具的额外规则

- 不得自行修改已审定文档，也不得替人填写 `reviewed-by`。修改提议写在 `ai-docs/proposals/` 或 PR 描述里。
- AI 生成的说明文档一律放进 `ai-docs/`，front matter 写明 `generated-by`、`generated-at`、`based-on`（见 `ai-docs/README.md`）。
- 动手前先读：`docs/design/design-v1.1.md`、`docs/design/signals-supplement.md`、所改模块的 README。

## 3. 架构硬约束（摘自设计文档，细节以原文为准）

- **每类数据只有一个模块负责写**（设计文档 5.1，补充章节第 7 节对 ⑤ 的补充）。不要在别的模块里写不归自己的数据。
- 原始数据只追加；语义信息 id 不变，修改即新增版本，旧版本标记为 `superseded`。
- 模块之间不直接互相下指令：只写 ①，由 ① 发「数据库被修改」；唯一例外是 ⑤ 发「请求重新判断」。
- 收到事件**先查当前状态**再决定是否处理；同一事件送达多次，结果和送达一次相同。
- 内容没有变化时不写入、不发事件。
- 读写数据库（①）、调用大模型（⑦）直接调用，不经过总线。所有大模型调用必须经过 ⑦，并带调用方模块名和 `root_feedback_id`。
- 写入时原样带上触发它的 `root_feedback_id` 和 `depth`。
- 核心数据不能依赖扩展数据；⑤ 按登记表分发，不在代码里写死接收方。

## 4. 公共约定（草案，以 `contracts/` 和审定后的术语表为准）

- 代码标识用英文，见 `ai-docs/proposals/glossary.md`（待审定后移入 `docs/`）。
- id：UUIDv7，小写字符串。版本号：整数，从 1 开始。
- 时间：一律 UTC，RFC 3339，以 `Z` 结尾；时间区间为半开区间 `[start, end)`。
- 系统发起的行为，`root_feedback_id` 为字面量 `"system"`。
- `contracts/` 中的字段**只能增加，不能删除或改义**；修改 `contracts/` 需要 ① 负责人与 I 共同审核。
- 日志用 JSON 结构化日志，每条带 `module`、`event_id`、`root_feedback_id`、`depth`。

## 5. 技术栈与工具

- 后端统一 Python 3.12，uv 管理依赖（仓库根 `pyproject.toml`，模块建好后加入 uv workspace）。
- 代码风格与检查：ruff（lint + format）、pyright、pytest。提交前跑：
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && uv run python tools/check_docs.py`
- 模块内部的选型（数据库、总线、算法、提示词等）由负责人决定，重要决定写成 `docs/adr/NNNN-<标题>.md`。
- MVP 不做客户端（⑥ 只做服务端 API）；原型阶段的客户端平台另行决定。

## 6. Git 规范

- `main` 为保护分支，只通过 PR 合并（squash）；不改写已推送的历史，不强推。
- 分支名：`<模块>/<主题>`，例如 `attribution/voiceprint-matching`、`docs/glossary`。
- 提交信息：Conventional Commits，scope 用模块名，例如 `feat(extraction): 定时提取跳过已提取时间段`。
- CODEOWNERS 指定的负责人审核对应目录。

## 7. 数据与凭据

- **真实录音、GPS、手机信息、D2 标注一律不进仓库**（含真实人名，伦理审查未定）。仓库只放 D1 脚本、标准答案和合成的假数据（`fixtures/`）。
- 凭据只放在 `deploy/.env`（不进仓库），模板见 `deploy/.env.example`。pre-commit 中的 gitleaks 负责拦截。
