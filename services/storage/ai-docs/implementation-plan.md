---
audience: ai
doc-type: guide
based-on: origin/dev 6cea0b7 的实际代码、契约与测试；已采纳的 docs/design/design-v1.2.md、signals-supplement.md、plugins-mvp.md、decisions/mvp-confirmed-decisions.md、ADR-0001；全局实施计划；①负责人“按当前设计开始准备模块1实现”的授权（准备阶段只写文档）；此后用户授权实现 P0a（已在本分支实现并验证，提交与合并状态以提交历史和 PR 为准）；项目负责人（用户）随后明确“采用推荐方案，允许在功能分支持续实现并分批提交、推送和提 PR；I 审核由我负责，合并前再审核”，据此 P0 契约提案为 accepted
---

# ① 实施准备：P0 首批 PR 与阶段出入口

本页原为①的**实施准备**。第 1、2 节和第 3 节的探针证据是准备阶段（基于 6cea0b7，当时没有改代码、测试、contracts、部署或依赖）的历史记录，不代表此后没有代码改动。**当前状态：P0a 已在本分支实现并验证，提交与合并状态以分支提交历史和 PR 为准（见第 3 节末尾和 [progress](progress.md)）；P0b 的契约提案已 accepted、已获实现授权，但尚未实现。** 其余能力都不因本页存在而视为已完成。目标语义以[设计 v1.2](../../../docs/design/design-v1.2.md)、[插件 MVP](../../../docs/design/plugins-mvp.md)和[决策索引](../../../docs/decisions/mvp-confirmed-decisions.md)为准；阶段编号沿用[全局实施计划](../../../ai-docs/plans/mvp-implementation.md)的 P0–P5；P0b 的契约选择见已采纳（accepted）的[P0 契约提案](../../../docs/proposals/storage-p0-contracts.md)。采纳依据是用户明确采用推荐方案并授权在功能分支持续实现、分批提交、推送和提 PR；这不等于合并前审核已完成：合并前由负责人兼 I 审核，AI 不代填 `reviewed-by` / `reviewed-at`。

## 1. 基线

- 代码基线：`origin/dev` = `6cea0b7`（PR #7 已合并），工作分支 `storage/implementation-preparation`。
- 准备开始前，主代理在该基线上实际运行并通过：`uv run ruff check .`；`uv run ruff format --check .`（23 个文件）；`uv run pyright`（0 错误/警告）；`uv run pytest`（289 passed）；`uv run python tools/check_docs.py --base origin/dev`（22 个 Markdown）；`git diff --check`。这只是改动前的基线，不代表任何后续 PR 的结果。

## 2. 当前能力与证据

路径相对 `libs/`。“探针”指本轮在仓库外用临时脚本对 6cea0b7 实际调用的结果，没有加入仓库。

| 能力 | 代码证据 | 结论 |
|---|---|---|
| 原子批次 | `MemoryStorage._write`：`_State.copy()` 后在 `_Txn` 上执行，全部成功才替换，外层 `RLock` | 语义可复用；写时复制加进程锁**不能**证明 PostgreSQL 并发正确 |
| 版本/R6 | `_Txn.update`、`_Txn.supersede`：终止追加 n+1 并复制内容；重复终止为 `unchanged`；终止后 Update 为 `object_superseded` | 基本符合设计 §3.2，保留，不重写 |
| 内容比较 | `memory.canonical_json`：`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` | 不是 RFC 8785；`1`/`1.0`、`True`/`1` 比较结果不同 |
| 冲突工具 | `results.WriteResult.check()`、`Conflict.retryable`、`RETRYABLE`；`protocol.write_with_retry` 最后仍可能返回冲突 | 冲突检查工具已经存在，问题在于 handler 是否调用 |
| 依赖登记 | `_Txn.register`：被依赖方按**该操作执行时**必须当前且未终止；只拦直接自依赖 `ref.id == op.dependent_id`；重建 `VersionRef(data_type, id, version)`；按 `(id, version)` 集合比较；`self.s.deps[key] = tuple(...)` 整体替换 | 探针：传入带 `fields` 的 VersionRef 后，读出时 `fields` 已经丢失；同批 A→B、B→A 可以提交 |
| 显式空集 | 空集登记时存入 `deps[key] = ()`；判断 `old_set == new_set and old` 中 `old` 为空 | 内部有记录，但 `dependencies_of` 无法区分“登记过空集”和“从未登记”；探针：连续两次登记空集都返回 `written` |
| 登记顺序 | `dependent_version=None` 取本批前面操作执行后的当前版本 | 保留，不改为整批任意顺序 |
| 过时查询 | `_StateReader.stale_dependencies`：`depends_on.version < target.version` | 只按版本判断，不支持字段 |
| 批末检查 | `_Txn.check_constraints` 在整批执行后处理 `unique_fields`、`exclusive_members` | 可作为批末新鲜度和图检查的挂载点 |
| 登记表 | `ear_contracts/registry.py`：`DataTypeSpec` 只有 kind/writer/unique/exclusive/time_from/description；`from_json` 只读取已知键；`Registry.extend` 只能在构造时扩展 | 探针：`requires_dependencies` 经 from_json/to_json 往返后丢失；当前不支持动态热注册 |
| 身份 | `MemoryStorage.client(module)` 直接按传入的模块名授权；读取和 `subscribe(from_beginning)` 不检查读权限 | 模块名由调用方自报，不是可信身份 |
| 契约 | `dependency-record.schema.json` 两层都是 `additionalProperties: true`；`records.VersionRef`、`DependencyRecord` 为 `extra="allow"` | 带未知字段可以通过校验，不等于 fields 已生效 |
| 并发测试 | `test_memory.py::test_concurrent_writers` 是同一线程内的两次顺序写 | 只证明乐观锁冲突，不是真实并发测试 |

## 3. P0a：四个已有规则的回归修复（首个实现 PR）

范围只包括以下四项。每项先提交会失败的测试，PR 中记录失败输出，再修复使测试通过（先红后绿）。公共签名不变，不改 contracts 和依赖。

| # | 违反的现有规则 | 证据（6cea0b7 探针） | 位置 | 红测试要点 | 修复方向 |
|---|---|---|---|---|---|
| 1 | 达到投递上限时不应丢失事件 | `run_until_idle(max_deliveries=1)` 有 2 个事件时：抛出 RuntimeError，handler 只收到 1 个，`pending()==0`，第 2 个事件丢失 | `MemoryBus.run_until_idle`：先 `popleft()`，再判断 `delivered > max_deliveries` | 抛出后 `pending()` 仍包含未投递事件；再次运行会补投 | 先判断上限再出队；超限时不取出事件 |
| 2 | 同一 consumer 按发布顺序串行处理 | 两个线程同时调用 `run_until_idle`，同一 consumer 有 2 个 handler 同时执行，顺序被打乱 | 同上：出队后释放锁再调用 handler；失败时 `appendleft` 可能晚于另一线程取走下一事件 | 用 `threading.Event` 控制时序，不用 sleep；`join(timeout)` 防止测试挂住；断言并发数为 1 且顺序不变（含失败重投） | 在总线锁下为每个 consumer 设在途标记，处理完成或重新入队后再清除；嵌套调用不能死锁 |
| 3 | `[t,t)` 是空区间 | `TimeRange(t,t).overlaps(t-1m, t+1m)` 返回 True | `queries.TimeRange.overlaps` 中有时长数据的分支 | `Query(time=TimeRange(t,t))` 对有时长和单点数据都不命中；现有 `test_point_time_semantics` 保持通过 | 空区间直接返回 False；构造函数仍允许 `start == end`，不改为抛异常 |
| 4 | 系统记录时间必须是 UTC，新建和更新使用同一模型校验 | 用无时区或 `+08:00` 时间更新已有记录时被直接保存；新建同样输入会抛出 `InvalidRequest` | `_Txn.put_system` 更新分支使用 `current.model_copy(update=...)`，跳过 Pydantic 校验 | 更新时传入无时区或非零时区时间抛出 `InvalidRequest`，revision 不变；UTC 更新仍成功 | 改用 `_build(SystemRecord, ...)` 重新校验 |

第 4 项只针对 `model_copy(update=...)` 跳过模型校验，与“系统记录类型能否声明 `time_from`”无关（后者由 `tests/test_contracts.py::test_registry_semantics` 约束）。

测试放进 `libs/ear_storage_client/tests/test_memory.py` 对应的小节，或新建同目录的 `test_memory_regressions.py`；不要顺带重构现有测试。

**handler 冲突处理（同 PR 只补说明，不修改总线）**：handler 忽略 `write()` 返回的冲突并正常返回时，内存总线会把该事件当成处理成功，这确实是风险。现有处理方式是：先单独判断 `already_processed`（说明之前已提交，正常结束），其余情况调用 `result.check()`，把冲突转成异常以触发重投。总线无法发现 handler 是否忽略了返回值；与本 consumer 无关的事件可以直接 return，不能为此禁止。持久消费者的确认/失败语义在 P2 处理。

出口：四个新测试先红后绿；README 中的完整检查命令全部通过；progress 按实际提交和测试结果更新。

**当前状态（本分支已实现并验证；提交与合并状态以提交历史和 PR 为准；尚未经合并前人工审核）**：测试位于 `libs/ear_storage_client/tests/test_memory_regressions.py`（12 项）。修复前 11 项失败、1 项通过（BaseException 释放在途标记的守护用例，旧代码没有标记，所以本来就会通过）；修复后 12 项全部通过，全量 301 passed。第 2 项采用每个 consumer 的内部在途标记：遇到正被处理的 consumer 时跳过而不等待，由持有它的 runner 继续投递；handler 以 BaseException 退出时只保证释放标记，事件仍按原语义丢失。详细结果和未覆盖项见 [progress](progress.md)。上表的“证据”列是修复前的探针结果。

## 4. P0b：字段依赖、登记表能力和批末检查（提案已采纳，尚未实现）

入口：已满足。[P0 契约提案](../../../docs/proposals/storage-p0-contracts.md)已 accepted，依据是用户明确采用推荐方案并授权在功能分支持续实现、分批提交、推送和提 PR。**P0b 尚未实现**，可以在本分支的同一 PR 中增量实施，或另开后续 PR（以 PR 说明为准）。合并前由负责人兼 I 审核（核对清单见提案第 4 节）；涉及③④的类型按类型启用时，仍须取得其实际输入并通知，不因本授权视为已通知。提案中标为“暂拟”的名称和内部 API 是工程决定，由实现者定名，不再等待确认。

| PR | 内容 | 文件 / 符号 | 主要测试 |
|---|---|---|---|
| P0b-1 契约与模型 | `DependencyRecord` 增加可选顶层 `fields`；登记表增加能力项且 from_json/to_json 往返不丢失；读取模型继续接受未知字段 | `contracts/records/dependency-record.schema.json` 及正反例；`contracts/registry/data-types.schema.json`、`data-types.json`；`ear_contracts/records.py`、`registry.py`、`__init__.py` | `tests/test_contracts.py`、`libs/ear_contracts/tests/test_ear_contracts.py` 的新旧正反例 |
| P0b-2 SDK 与内存登记 | 新增显式依赖输入（暂拟名 `DependencyInput`）；保留 `fields`；规范化；同输入合并；字段模式的新鲜度规则；`stale_dependencies` 按字段判断；能读出空集登记 | `ops.py` 中的 `RegisterDependencies` 及新类型；`memory._Txn.register`、`_StateReader.stale_dependencies` / `dependencies_of`；`queries.StaleDependency` | 缺失和 null、数组顺序、重复字段、`[]`、A→B→A、整体替代、`1` 与 `1.0`、`True` 与 `1`；旧 VersionRef 调用行为不变 |
| P0b-3 批末强制 | 需要依赖的类型必须同批完整登记；本批登记在批末重新检查新鲜度；检查当前图的自环、间接环和批内环；禁止核心依赖插件 | 在 `_Txn.check_constraints` 旁新增批末检查；`_State.deps_by_target`；登记表的来源信息 | 漏登、显式空集、无变化的 Update、Supersede 免登记；先登记再在同批更新被依赖方：全对象模式（含旧 VersionRef、未指定 fields 的 DependencyInput）被拒，字段模式下所选字段变化被拒、仅无关字段变化可接受；旧历史边不阻止新图；启用前对现有当前图做环校验（遗留环显式报告，不自动删边）；**其他对象已有的过时边不影响本批提交** |

约束（详见提案，不在此重复定义）：保留 `dependent_version=None` 的现有登记顺序；新的强制检查必须有明确的启用方式和调用方迁移说明，不能因为“只加了字段”就称为向后兼容；比较 JSON 时不依赖数据库的 json 相等判断。

## 5. 阶段出入口（①视角）

| 阶段 | 入口 | ①交付 / 出口 | 不在本阶段 |
|---|---|---|---|
| P0a | 本页 | 四个回归修复先红后绿 | 契约改动、request-id、远程 snapshot、鉴权 |
| P0b | 提案已采纳、已获功能分支实现授权（合并前由①负责人兼 I 审核） | 契约、模型、SDK 和内存实现与设计 §3.5、§8.2 对齐；新旧用例通过 | HTTP/API/凭据/数据库版本选型；多 cause |
| 进入 P1 前 | P0b 合入 | 写出并评审请求 ID 与提交结果未知、远程 snapshot 与分页边界、可信身份与凭据三份设计 | 实现 |
| P1 | 上述设计已评审，I 提供可用的 PostgreSQL 环境 | 正式服务包、迁移、PostgreSQL 事务；**可信身份及读取路径的授权**（get/history/get_many/query/count/依赖/过时依赖/is_processed/snapshot）；订阅授权的接口设计与过滤规则在 P1 定稿，但实际按授权投递和远程订阅在 P2 验收，P1 不代表生产订阅已实现；**多连接的真实并发测试**（同版本竞争、并发形成环、计算期间输入更新）；提交结果未知时可安全重试；snapshot 生命周期边界 | 授权完成前不向插件开放 |
| P2 | P1 | Outbox/Relay、JetStream 持久消费者、确认/退避/死信/重放；handler 失败不能被当作确认；按 P1 规则做最小权限的事件过滤与授权投递、远程订阅；`data_changed` 按获批类型订阅分发（多个获授权模块各有独立消费进度），`reevaluation_requested` 才按目标类型所属方和重判能力路由，均保留最小权限过滤；每个模块使用独立消费者；数据库内数据与 Outbox 原子提交，跨数据库/总线为至少一次投递与可恢复重投，消费端靠幂等去重，不宣称单事务或外部恰好一次 | 管理业务 |
| P3 | P1、P2 | 动态持久注册、权限与凭据生命周期、①管理反馈处理器、可恢复阶段；配合⑥管理页 | 插件计算 |
| P4 | 可以在 P0 内存基线上与早期阶段并行 | ①为③④⑤⑦提供结果记录、调用记录、多因与规则集合所需的类型、依赖和事务支持；不包办它们的业务 | 归属/提取/调度逻辑 |
| P5 | P1–P4 | 插件历史交接（快照/游标）、停用恢复、多层集成；配合 I 和示例插件 | — |

插件动态注册、权限、管理页、多层传播和历史处理都属于 MVP，不能因阶段靠后就移出 MVP。从 P1 开始，逐步把内存测试抽取成内存与持久化后端共用的契约测试，P0 不强制大规模重构测试。

## 6. 阻塞项与交接

- **①+I**：合并前审核 P0 实现 PR（核对清单见 P0 契约提案第 4 节）；P1 前的三份设计；提供 PostgreSQL/NATS 环境与 Compose（I 负责部署）。
- **③④负责人**：确认 `attribution`、`artifact`、`rule_selection` 等候选类型的实际输入以及是否需要强制依赖；④描述规则候选集合的业务一致性需求，由①与④共同确定提交期的集合校验方式，①负责提供存储原语，⑤遵守不扫描历史的门控。①不替他们决定业务规则。
- **后续接口队列（不进 P0）**：多 cause 与跨 root 的兼容表达（不能改变旧 `root_feedback_id` 的含义，也不把“只合并同 root”当作已决定）；完成记录按所属模块分类型单写（不把“按行记录写入方”当作合法替代）；外部动作恢复标识；插件建议的交接方式。
- 本页和提案都不冻结服务 HTTP 协议、API、凭据或数据库版本。

## 7. 已验证与未测

- 已验证：第 1 节的基线命令（主代理运行）；第 2、3 节的探针（只读调用，没有修改仓库）。
- 未测或未实现：P0b（字段依赖、登记表能力、批末强制）；PostgreSQL 和真实多进程并发；远程服务；鉴权；可靠投递；动态注册。P0a 后全量 301 个测试通过（含 12 个回归测试）不能证明这些新目标已经实现。
