---
audience: human
doc-type: guide
reviewed-by: aytony
reviewed-at: 2026-10-30
---

# Ear（Echo As Record）

面向个人部署的活动建模服务：收集录音、GPS、手机信息等个人数据，把每一天整理成一连串活动，并按用户的自然语言规则输出笔记、待办、日历和提醒事项。用户纠正一处错误后，受影响的结果自动修正。

本文件是仓库的总规范，人和 AI 都要遵守。人类维护者读完本文件和下面三份设计文档，就可以开始工作。

## 1. 先读这些

- [设计文档 v1.2](docs/design/design-v1.2.md)
- [模块间信号（补充章节）](docs/design/signals-supplement.md)
- [MVP 工作分解 v1.2](docs/design/work-breakdown-v1.2.md)
- [动态插件 MVP](docs/design/plugins-mvp.md)、[已确认决策索引](docs/decisions/mvp-confirmed-decisions.md)
- 所负责模块的 `services/<模块>/README.md`

[AGENTS.md](AGENTS.md) 是写给 AI 编程工具的入口，只在本文件之上补充对 AI 的额外限制，人不必阅读。

## 2. 目录

| 路径 | 内容 |
|---|---|
| `docs/` | 面向人的功能、核心逻辑、验收、提案与架构决策；入口见 [docs/README.md](docs/README.md) |
| `ai-docs/` | 面向 AI 开发的代码导航、实现细节、测试与排障；入口见 [ai-docs/README.md](ai-docs/README.md) |
| `contracts/` | 模块间约定的 JSON Schema 与示例 |
| `services/` | ①～⑦ 各模块：`storage`、`adapters`、`attribution`、`extraction`、`dependency`、`api`、`llm_gateway` |
| `libs/` | 共享库（SDK 等） |
| `fixtures/` | 合成的假数据 |
| `tests/` | 仓库级测试；`tests/e2e/` 为端到端 8 步流程 |
| `experiments/` | 研究用的对照实验，不是产品模块（如 [Codex 基线](experiments/codex_baselines/README.md)） |
| `deploy/` | docker compose 与环境变量模板 |
| `tools/` | 仓库工具（如文档结构与链接检查） |

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

## 4. 文档职责与决策状态

**按内容职责分类，不按作者或审定状态分类。** 人可以阅读实现文档，AI 必须先理解相关设计。

| 位置 | 职责 | 不应承载 |
|---|---|---|
| `docs/`、模块 README | 面向人：功能、核心逻辑、模块边界、跨模块语义、验收和重要决策 | 大量代码导航、SQL、逐步调试记录 |
| 任意层级 `ai-docs/`、`AGENTS.md` | 面向 AI：工程上下文、实现方案、代码导航、开发及验证流程 | 唯一一份产品规则或未经批准的新功能要求 |
| `contracts/` | 可机器验证的接口结构、Schema 与示例 | 代替产品行为和核心逻辑的解释 |

- 判断标准：换一种实现后仍必须成立的行为，写入人的设计文档；依赖当前实现的细节写入工程上下文。
- **目录和合入分支不表示采纳。** 设计、提案、决策使用 `status: proposed / accepted / superseded`；其他说明按内容类型管理，不因放入 `docs/` 自动成为规范。
- `proposed` 仅用于讨论，不覆盖当前规则；`accepted` 表示经负责人明确采纳；`superseded` 保留历史，并指向替代文档。
- AI 可以起草面向人的文档，但不能自行批准提案或宣称完成了人工审核。
- `reviewed-by` / `reviewed-at` 可保留在任何目录，代表历史审核记录，不是当前正文自动通过审核的证明。不再要求每次改正文就更改这两个字段。
- 一条规则只维护一个定义位置，其他文档引用。具体元数据、阅读路径与迁移说明见 [文档导航](docs/README.md)。
- `tools/check_docs.py` 检查结构和本地链接，不判断设计正确性，也不代替人工批准。

**冲突处理：** 已采纳的设计与公共约定规定目标语义；`contracts/` 及测试约束字段和可执行行为；代码说明实际行为；工程文档解释实现。任何位置的 `proposed` 都不覆盖已采纳规则。发现目标、契约、代码不一致时报告并交负责人决定，不因目录、实现现状或提案已合入而静默改义。

## 5. 人与 AI 的协作

- **人对进入 `main` 的一切负责。** PR 作者和审核人对提交内容负责，不论起草者是谁。
- AI 可以修改代码、测试，并在合适的位置起草或维护文档；不再要求 AI 生成的文档一律进入 `ai-docs/`。
- 功能、核心逻辑、模块职责和跨模块保证的改变须获负责人明确批准；未批准的选择写入 `docs/proposals/`，标为 `proposed`。实现组织方案可以放在 `ai-docs/`，不得以此绕过设计决策。
- 已授权的实现或文档任务可以同步修订相关说明；没有授权的产品行为变化应先提出问题。PR 要列明设计、契约、实现和测试的同步范围。
- `reviewed-by` / `reviewed-at` 只能由审定人本人填写，AI 不代填、不改写历史审核记录。采纳依据由负责人在讨论或 PR 中明确，CI 通过不等于采纳。
- 对 AI 的额外要求写在 `AGENTS.md`；总规则以本文件为准。

## 6. 架构硬约束（摘自设计文档，细节以原文为准）

- **每类数据只有一个所属模块负责写，重新判断也由所属模块执行。** 所有模块经① SDK读写，可信身份决定权限；插件不能直接连接数据库或写核心数据。
- 原始数据只追加；语义信息 id 不变、历史版本不可变。普通修改追加新版，整体替代追加 `superseded` 终止版本；最新版本是当前版本，不原地改写历史状态。
- 总线只有「数据库被修改」「请求重新判断」：①负责前者，⑤负责后者。插件管理同样由⑥写反馈、①发事件，再由①管理处理器消费并执行内部逻辑；①不接管归属/提取业务。
- 收到事件**先查当前状态**再决定是否处理；同一事件送达多次，结果和送达一次相同。
- 业务内容不变不新增业务版本、不发修改事件，但重新判断仍提交完整依赖与完成系统记录；⑤查询完成情况，不凭无事件推断未完成。
- 读写数据库（①）、调用大模型（⑦）直接调用，不经过总线。所有大模型调用必须经过 ⑦，并带调用方模块名和 `root_feedback_id`。
- 写入时原样带上触发它的 `root_feedback_id` 和 `depth`。
- 核心数据不能依赖插件数据；允许插件跨模块和内部的多层依赖。①强制当前有效对象图无环、需要依赖的结果同批完整登记；⑤动态按所属方/重判能力分发并协调相关上游完成。
- 产物为纯Markdown，字段级依赖区分名字与参考变化；活动划分及名称描述仅用原始数据。规则编辑不主动扫历史，历史因其他变化重判时用当前规则。
- 插件权限按类型显式批准，订阅不超过读取范围；历史补处理、撤权/停用、外部结果未知均按设计明确处理，不静默冒充成功。

以上是已采纳目标；当前代码/契约的差距见[实施计划](ai-docs/plans/mvp-implementation.md)，不能以设计更新宣称实现完成。

## 7. 公共约定（细节以 `contracts/` 和术语表为准）

- 代码标识用英文，见 [术语表](docs/glossary.md)。
- id：UUIDv7，小写字符串。版本号：整数，从 1 开始。
- 时间：一律 UTC，RFC 3339，以 `Z` 结尾；时间区间为半开区间 `[start, end)`。
- 系统发起的行为，`root_feedback_id` 为字面量 `"system"`。
- `contracts/` 中的字段**只能增加，不能删除或改义**；修改 `contracts/` 需要 ① 负责人与 I 共同审核。
- 日志用 JSON 结构化日志，每条带 `module`、`event_id`、`root_feedback_id`、`depth`。

## 8. 技术栈与选型

- 后端统一 Python 3.12，uv 管理依赖（仓库根 `pyproject.toml`，模块建好后加入 uv workspace）。
- 代码风格与检查：ruff（lint + format）、pyright、pytest。
- 已采纳：①独立服务，PostgreSQL同库多schema，NATS JetStream+Outbox/Relay，见 [ADR-0001](docs/adr/0001-storage-and-runtime-modules.md)。具体协议、版本、参数及算法/提示词由实施设计确定；重要变更仍走决策流程。
- CI 中涉及大模型的测试使用录制好的回复，不调用真实模型。
- MVP以服务端API为主，明确增加⑥的轻量插件Web管理页/API；完整客户端和通用插件结果页面仍不纳入。插件程序的自动安装/启动、市场及不可信代码沙箱不做。

## 9. Git 与 PR

- 两个长期分支，都是保护分支，只能通过 PR 合并，合并前必须通过全部 CI 检查：
  - `dev`：日常集成分支。成员从 `dev` 拉出自己的分支，开发完成后提 PR 合回 `dev`（squash 合并）。
  - `main`：稳定分支。由 `dev` 提 PR 合入（使用 merge commit，不 squash，保证 `dev` 与 `main` 的历史一致）。
- 不直接向 `dev`、`main` 推送；不改写已推送的历史，不强推。
- 分支名：`<模块>/<主题>`，例如 `attribution/voiceprint-matching`、`docs/glossary`。
- 提交信息：Conventional Commits，scope 用模块名，例如 `feat(extraction): 定时提取跳过已提取时间段`。
- CODEOWNERS 指定的负责人审核对应目录；PR 按模板逐项勾选。

## 10. 数据与凭据

- **真实录音、GPS、手机信息、D2 标注一律不进仓库**（含真实人名，伦理审查未定）。仓库只放 D1 脚本、标准答案和合成的假数据（`fixtures/`）。
- 凭据只放在 `deploy/.env`（不进仓库），模板见 `deploy/.env.example`。pre-commit 中的 gitleaks 负责拦截。
