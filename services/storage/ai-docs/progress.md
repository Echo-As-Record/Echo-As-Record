---
audience: ai
doc-type: status
based-on: origin/dev 48d3c51 的代码与契约；docs/design/design-v1.2.md；docs/design/plugins-mvp.md；ADR-0001；6cea0b7 实施准备（当时无实现改动）；storage/implementation-preparation 分支 P0a 提交 a6a1c5a、P0b 提交 cf337d1、P1 提交 d7c9919（提交前完整检查日志 /tmp/ear-p1http-full-OK7Aeg.log，exit 0）、P2/P3 后端提交 d0d8875（schema revision 5–8：Relay 与持久消费者、管理反馈、凭据、订阅重配置）与合并提交 b7307e9（同步 dev 4afeae0 的模块④，未改④代码）、P4 集合守护提交 f747e89（schema revision 9）、P4 重判完成记录提交 106cd2e（完整真实运行日志 /tmp/ear-p4-completion-full.log，日志内 EXIT=0，并有 /tmp/ear-p4-completion-full.exit），均在 PR8、未合并；106cd2e 之上的多起因提交 daa767f（已推送），其完整真实运行 /tmp/ear-p4-causality-full.log 为 1168 passed / 5 skipped（日志内 EXIT=0，/tmp/ear-p4-causality-full.exit 为 0）；daa767f 之上的⑦调用记录 `llm_call` 与正文策略 `ModelBodyPolicy` 已提交 e9391dd 并推送（状态记录 /tmp/ear-p4-modelcall-state.json、/tmp/ear-p4-bodies-state.json；e9391dd 本身未单独做完整真实运行）；e9391dd 之上的 P5 ①侧插件历史恢复（schema revision 11–14）已在本分支实现（提交状态以提交历史为准；完整真实运行 /tmp/ear-p5-checkpoint-full.log 为 1 failed / 1411 passed / 5 skipped，.exit 为 1；唯一失败测试已修正，定向重跑 /tmp/ear-p5-bootstrap-fixed.log 1 passed、.exit 为 0，未重跑完整，见 P5 节）；各阶段日志 /tmp/ear-p2b-mustfix/closing-*.log 及 .exit、/tmp/ear-p3-reliability-*.log、/tmp/ear-p3-credentials-{final,refresh}-*.log 与 /tmp/ear-p3-credentials-state.json、/tmp/ear-p3-checkpoint-validation.log（exit 0），以及 b7307e9 后的默认检查结果；P5 已提交 9705b61、②适配器合并 84f20fe；存储切片部署已在本分支实现（提交状态以提交历史为准，无生产启动）及容器冒烟 /tmp/ear-storage-final-smoke.log（.exit 0，16 项）、备份新版真实往返与五种真实竞争 /tmp/ear-final-race-84lx9T/real.log（1 passed in 100.61s）及汇总 /tmp/ear-final-race-close.json（历史失败记录 /tmp/ear-backup-main-final.log 早于新版，已被取代，不代表当前状态）、三开启本地完整运行 /tmp/ear-repro-suite-ykxiUWmy（exit 为 0，1503 passed, 5 skipped, 1 warning in 890.20s；远程 CI 与人工审核尚待），见 deployment.md；项目负责人（用户）采用推荐方案并授权功能分支持续实现、分批提交推送提 PR、合并前由负责人兼 I 审核；当前基线 origin/dev 376294d：PR8 已于 2026-11-07T09:32:19Z 合并（gh 只读核对，用户述已审核并合并），dev 上 CI run 42360158060 success（1503 passed, 5 skipped, 1 warning in 583.04s，三真实开关均为 1）；⑥⑦ 全套验收记录 /tmp/ear-67-full-v2.log（.exit 为 0，2067 passed, 11 skipped, 2 warnings in 1001.65s）与 /tmp/ear-67-static-final.log（.exit 为 0）；本次仅改 Markdown 同步当前状态，历史测试数字不改
---

# ① 实现状态与新目标差距

[设计](../../../docs/design/design-v1.2.md)、[插件](../../../docs/design/plugins-mvp.md)和[ADR](../../../docs/adr/0001-storage-and-runtime-modules.md)已采纳；采纳决定不等于已实现。本分支已提交 P0a（a6a1c5a）、P0b（cf337d1）、P1（d7c9919）、P2 与 P3 后端（d0d8875），并以 b7307e9 合并 dev 4afeae0（模块④，未改其代码）；均在 PR8；PR8 已于 2026-11-07T09:32:19Z 合并为 dev 376294d（见下方“当前状态”）。P3 只余⑥管理页对接；P4 已提交①侧集合守护（f747e89，schema revision 9）与按所属方验证的重判完成记录及权威查询（106cd2e），多起因（`additional_causes`）已提交 daa767f；⑦调用记录的①侧存储（`llm_call`、`ModelBodyPolicy`）已提交 e9391dd（见 P4 节）；⑦网关服务与实际模型调用、③④⑤⑦业务接入未做，不是完整 P4；P5 ①侧插件历史恢复已在本分支实现（提交状态以提交历史为准，见 P5 节），⑤实际调度、④业务改动与真实多层示例未做，不是完整 P5，也不是完整 MVP。所有权及接口目标见[人读说明](../../../docs/modules/storage.md)。

## 当前状态（dev 376294d；以本节为准）

- **合并与 CI（`gh` 只读核对）**：PR8（`storage/implementation-preparation`，标题“feat(storage): 存储与事件服务、动态插件及隔离部署恢复”）于 2026-11-07T09:32:19Z 合并，合并提交 `dev` 376294d（合并人 aytony；用户述已审核并合并，本页不代填任何 reviewed 字段）。dev 上 push 触发的 GitHub Actions CI run 42360158060（workflow CI，headSha 376294d）结论 success；日志中 `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS` 均为 1，pytest 末行 **1503 passed, 5 skipped, 1 warning in 583.04s (0:09:43)**。
- **含义**：①存储切片（P0–P5 ①侧与隔离部署/备份切片）在 CI 隔离环境的真实 PostgreSQL + NATS + 备份往返下通过，**不是生产验收**：没有生产启动；生产部署与升级迁移、流保留/清理与容量、备份文件保留/删除、按模块隔离的 NATS 账户仍未做。
- **仍未完成**：③④⑤尚未与①完整业务联调（④仍是 v1.1 演示基线，⑤调度未实现，通过的只有固定⑤测试驱动的插件链）；②适配器已实现远程单轮/常驻扫描和重放，真实①双门控联调测试与真实旧 Ear 数据写入临时①已于 2026-11-11 本机跑通（非生产验收）；⑥管理页与 ⑥ API、⑦网关服务**不是完成**，只是功能分支 `api/gateway-mvp`（基线 dev 376294d，**待合并**）里的部分交付、待审（见[⑥入口](../../api/README.md)、[⑦入口](../../llm_gateway/README.md)）：⑥ 的 34 项真实浏览器检查只对内存假①；API/网关 HTTP 层对真实回环①+隔离 PostgreSQL/NATS 的集成测试（`test_api_gateway_integration` 26 项）与①侧预检测试已通过（12:07 快照，合成 provider）；⑦ 没有任何真实模型调用或 Octopus wire 验证；没有真实浏览器↔真实①的页面联调，⑤调度未接入。这些不计入①切片的验证和生产验收；⑥⑦ 的全套验收（四个开关全开）当前结果：修复后的最终全量 `/tmp/ear-67-full-v2.log` 退出 0，2067 passed、11 skipped、2 warnings、1001.65 秒，最终静态检查 `/tmp/ear-67-static-final.log` 退出 0（合成/隔离环境，不是生产验收）；历史失败保留：12:35 全套 3 failed（部署/备份测试环境问题，已修复）、14:26 静态检查 `ruff format` 失败（已修复）。详见文末“⑥⑦ 与①的真实边界”。
- **对历史记录的说明**：下文各节（P0a…P5、部署切片）里“未合并”“未经合并前人工审核”“远程 CI 尚未运行”等措辞是当时状态，保留原文与历史测试数字（如 802、916、1091、1168、1411 + 1 failed、本地 1503 的 890.20s），不据此改写；`reviewed-by` / `reviewed-at` 未触碰。以本节为当前状态。

## 已有代码

- contracts：记录、事件、依赖、注册表Schema和示例，部分DRAFT；P0b 增加依赖 `fields` 与登记表能力项；P3（d0d8875）在 `contracts/registry/data-types.json` 新增 `plugin_management_feedback`（raw_record，writer `api`）与 `plugin_management_result`（system_record，writer `storage`）。
- ear_contracts：公共模型、UUIDv7、UTC、登记表及一致性测试。
- ear_storage_client：同步Protocol、批次操作、结果/冲突、查询、MemoryStorage、进程内事件工具；P1 起 `engine.py`、`wire.py`、`remote.py` / `remote_errors.py`；P2（d0d8875）增加 `consumer_wire.py`、远程 `subscribe` / `poll_once` / `run_until_idle` 与 `DeliveryExpired`。
- ear_storage（`services/storage/`）：P1 的 PostgreSQL 后端、身份与授权、snapshot、HTTP、CLI；P2 的 `relay.py`、`consumers.py`、`consumer_broker.py`；P3 的 `management.py`、`management_input.py`、`credentials.py`、`reconfiguration.py`（P2/P3 均 d0d8875）。

## 已决定，尚未实现

| 目标 | 差距 |
|---|---|
| 独立服务、数据库和总线 | P1 与 P2（d0d8875）已有：PostgreSQL 事务与持久 Outbox、有序 Relay → JetStream、服务端授权的持久消费者（HTTP 代理）与 SDK 订阅。①存储切片的隔离部署 Compose 与备份/恢复工具已在本分支实现（非生产，见[隔离部署指南](deployment.md)）。**缺**：生产流保留/清理与容量策略、按模块隔离的 NATS 账户、生产部署与升级迁移、备份文件保留/删除、远程 CI 实跑（与 I 协作） |
| 字段依赖/强制登记/防环 | 内存（P0b）与 PostgreSQL（P1）已实现；核心类型 `requires_dependencies` 全为 false，需③④迁移验证后按类型启用 |
| 动态注册与可信权限 | P3 后端（d0d8875）：运行时数据库登记表、动态创建模块 namespace、启停与权限替换、可信管理员角色、凭据经管理反馈的轮换/撤销与一次性领取（revision 7）、订阅完整重配置（revision 8），见 P3 节。**缺**：⑥管理页与真实①的浏览器联调（页面已在⑥实现，功能分支待合并，仅对假①做过真实浏览器检查，API 层另有对真实①的集成测试，见文末“⑥⑦ 与①的真实边界”）；按模块隔离的 NATS 账户（与 I） |
| 管理反馈 | P3 后端（d0d8875）：①管理反馈处理器、可恢复阶段、`retry_task`、`rotate_credentials` / `revoke_credentials` 与一次性 claim、订阅重配置、管理结果系统记录。**缺**：⑥管理页与真实①的浏览器联调（⑥ 页面已实现，同上）；`AuthOperator` 不能代替日常管理 |
| 完成记录与调度交接 | ①侧：集合守护（f747e89，schema revision 9）与重判完成记录（106cd2e：权威索引验真，需显式 `reevaluation_result` 能力）已提交；多起因与请求关联已提交（daa767f，见 P4 节）。**缺**：③④⑤接入（④入队 marker 不是业务完成，④业务未改）（P4） |
| 调用记录（⑦经①保存） | ①侧 `llm_call` 系统记录（`llm_gateway` 单写终态、真实请求关联、按全部起因读授权）与部署级正文策略已在本分支实现（提交状态以提交历史为准，见 P4 节）。**缺**：⑦网关服务与实际模型调用、各调用方接入、正文保留期/TTL 与清理、包含本切片的完整真实运行（P4） |
| 最新规则/纯Markdown/选择结果 | 主要属于③④⑤业务对齐；①提供所需类型、依赖及事务支持（P4） |
| 插件多层/历史处理 | ①侧历史恢复（catch_up/skip、显式范围补处理、owner claim/commit、供⑤的请求关联与分配）已在本分支实现，见 P5 节。**缺**：⑤实际调度接入、④业务改动、真实多层/分叉汇合示例插件、⑥恢复选择页；注册默认仍只收未来事件 |

## 下一步

P2/P3 后端已提交 d0d8875 并推送到 PR8；b7307e9 合并了 dev 4afeae0（模块④，①未改其代码或文档）。b7307e9 本身只跑了默认检查（见 P3 节“检查点与合并后验证”）；文档记录中合并后的首次完整真实运行在 106cd2e（1091 passed / 0 skipped，见 P4 节）；CI 配置真实 PostgreSQL 与 NATS；该配置已在 dev 376294d 远程运行通过（run 42360158060，见“当前状态”）。P3 剩余⑥管理页对接。P4 ①侧已提交集合守护（f747e89）与重判完成记录（106cd2e），多起因已提交 daa767f，⑦调用记录的①侧存储已提交 e9391dd（见 P4 节）；P5 ①侧历史恢复已在本分支实现（提交 9705b61），其中 schema 硬编码测试已修正，之后的三开启本地完整运行 1503 passed / 5 skipped 无失败（见“提交更新与存储切片部署”）；之后按[全局实施计划](../../../ai-docs/plans/mvp-implementation.md)完成 P4 其余部分（⑦网关服务与实际模型调用，③④⑤⑦接入完成记录、集合守护与调用记录）与 P5 其余部分（⑤实际调度接入、④业务改动、多层集成与示例插件）。部署与I合作，页面归⑥，传播调度归⑤；不要让①包办业务模块。

不要把 `test_memory.py::test_concurrent_writers`（顺序旧版本冲突）当真正并发测试；真实多进程竞争在 `services/storage/tests/test_p1a_regressions.py`。SDK用法与命令见[工程指南](interface.md)。

## 交接清单（b7307e9 合并 dev 4afeae0 后）

- 模块④：合入的 `services/extraction/` 是 v1.1 演示基线，其文档注明不代表 v1.2（见[④实现记录](../../extraction/ai-docs/implementation.md)）。仍使用特殊实体标记、旧规则阻塞，没有 `rule_selection`；其原有 39 项测试通过**不等于**已与 v1.2 对齐。①未改④的代码或文档；④按类型启用强制依赖登记、完成记录、接入集合守护等仍待④迁移（P4）。P4 集合守护后，默认运行中④原有 39 项仍通过；这不代表④已与①远程服务集成。
- `services/extraction/README.md` 不存在（AGENTS 要求的模块入口缺口），已报告，由④负责方补；①不代写。
- 待通知：P0b 契约字段与能力项、P3 新增的两个登记类型，尚未通知③④及 `api` 负责方；P4 新增的 `llm_call` 登记类型、`llm_call_metadata` 能力项与调用记录契约，未记录已通知⑦（`llm_gateway`）负责方。
- 待办（写下时的状态；⑥⑦ 现状见文末“⑥⑦ 与①的真实边界”）：⑥管理页与真实①的浏览器联调（⑥ 页面已在功能分支 `api/gateway-mvp` 实现，待审、待合并）；生产验收与部署。原待办“合并前人工审核”“包含调用记录切片的完整真实运行”“远程 CI”已完成：PR8 已合并，完整真实运行与远程 CI 见“当前状态”。

## 实施准备（历史记录，基线 6cea0b7）

准备阶段当时**无实现改动**（未改代码/测试/契约/部署）。首批PR拆分、证据与阶段出入口见[①实施准备](implementation-plan.md)，P0b契约选择见[P0契约提案](../../../docs/proposals/storage-p0-contracts.md)（准备阶段为 proposed，现已 accepted）。这一段只描述准备阶段，不代表此后没有代码改动。

## P0a：四项回归修复（提交 a6a1c5a）

在 `storage/implementation-preparation` 分支基于 6cea0b7 实现并验证。**尚未经合并前人工审核**：审核由负责人兼 I 在合并前进行，不由 AI 代填 reviewed 字段。没有改 contracts、公共签名、依赖或部署。

| # | 修复 | 位置 |
|---|---|---|
| 1 | 投递上限在出队前检查；超限抛 RuntimeError 时未投递的事件留在队列，下次调用补投 | `memory.MemoryBus.run_until_idle` |
| 2 | 每个 consumer 有内部在途标记：多个 runner（含 handler 内嵌套调用）不并发处理同一 consumer；重投/重复投递的事件在释放标记前放回队列，保持顺序；handler 在总线锁外执行；标记在 `finally` 中释放（包括 BaseException） | `memory._Subscription.busy`、`MemoryBus.run_until_idle` / `_deliver` |
| 3 | `TimeRange(t, t)` 不命中任何数据（仍允许构造） | `queries.TimeRange.overlaps` |
| 4 | 系统记录更新用 `_build(SystemRecord, ...)` 重新校验，先校验再判断“无变化”；非 UTC 时间或无效 clock 使整批回滚；created_at 保留，时间规范化为 UTC；无变化不加 revision、不改 updated_at、不发事件 | `memory._Txn.put_system`（P1 后共享逻辑位于 `engine.py`，以代码为准） |

测试：`libs/ear_storage_client/tests/test_memory_regressions.py`（12 项）。

- 修复前：11 failed / 1 passed。通过的是 BaseException 释放标记的守护用例：旧代码没有在途标记，所以它本来就会通过。
- 修复后：12 passed；该文件连续运行 60 次都通过；当时全量 `pytest` 301 passed（原 289 + 新 12）；ruff、format、pyright、check_docs 及 `git diff --check` 均通过。
- 行为边界：某个 runner 遇到正被其他 runner 处理的 consumer 时直接跳过（不等待），可能在队列尚未清空时返回，由持有该 consumer 的 runner 继续投递。handler 以 BaseException 退出时，该事件仍按原语义丢失，只保证释放标记。
- 未覆盖：同时传入 naive 与 aware 时间时 `time_range` 的比较异常（新建和更新路径都有，未改）。

## P0b：字段依赖、登记表能力、批末检查（提交 cf337d1）

依据：[P0 契约提案](../../../docs/proposals/storage-p0-contracts.md)已 accepted（用户采用推荐方案并授权功能分支实现）。**尚未经合并前人工审核**（负责人兼 I），未通知③④。

| 范围 | 实现 | 位置 |
|---|---|---|
| 契约 | `DependencyRecord` 顶层可选 `fields`（字符串、非空、不重复、不接受 null）；登记表能力项 `requires_dependencies`、`supports_reevaluation`（缺省 false，仅 semantic/rule）；8 个新正反例；`data-types.json` 当时未改 | `contracts/records/`、`contracts/registry/data-types.schema.json` |
| 模型 | `DependencyRecord.fields`（省略时序列化不输出）；能力项 from_json/to_json 往返；`Registry.core_types`、`is_core`、`extend`（插件）、`extend_core` 与 `from_json/load(trusted_core=)`（受信 bootstrap）；JSON 不作为可信来源 | `ear_contracts/records.py`、`registry.py` |
| SDK | `DependencyInput(ref, fields)`：去重、按码点排序，拒绝空、单字符串、非字符串；带 `fields` 的 `VersionRef` 以 `InvalidRequest` 拒绝（行为变化）；`Reader.dependency_set()` / `DependencySet` 区分从未登记（None）与空集；`StaleDependency.fields`；冲突 `dependency_cycle`（不可重试） | `ops.py`、`queries.py`、`protocol.py`、`results.py` |
| 内存 | 同对象同版本合并（整个对象优先，否则字段并集），版本/类型不同拒绝；字段模式按 `canonical_json` 比较所选字段（缺失≠null），整体替代始终过时；需依赖类型 Create/Update（含 unchanged）须同批登记，Supersede 免；本批登记/重算结果批末复查新鲜度；当前有效图成环返回 `dependency_cycle` 及路径；核心依赖插件拒绝；同集重复登记 unchanged | 当时位于 `memory._Txn`；P1 后共享逻辑抽到 `engine.py`（以代码为准） |

测试：`libs/ear_contracts/tests/test_p0_contract_models.py`、`libs/ear_storage_client/tests/test_p0_sdk_models.py`、`test_p0_memory_dependencies.py`，加 contracts 示例；当时全量 **426 passed、0 xfail**。

边界：核心类型能力项全部 false，强制登记目前不对任何内置类型生效；内存实现中 `client(module)` 仍信任自报模块名（可信身份只在服务路径）；提案 2.10 要求的 `dependency_cycle` 旧客户端端到端兼容测试**未确认已补**，合并前审核须核对。

## P1：持久化服务、可信身份与远程 SDK（提交 d7c9919，已随 PR8 合入 dev）

依据：ADR-0001；[P1 工程指南](persistent-service-plan.md)；上述用户授权。以分支历史与 PR8 为准。**未经合并前人工审核**（负责人兼 I），未通知③④。下表描述 d7c9919 提交时的状态；P2/P3（d0d8875）的增量见后两节。

| 范围 | 实际实现（d7c9919） | 位置 |
|---|---|---|
| 包与依赖 | 服务包 `ear-storage` 加入 uv workspace（psycopg 3、FastAPI、uvicorn）；SDK 增加 httpx；pyright/pytest 覆盖 `services/storage`；pytest marker `postgres` | `services/storage/pyproject.toml`、根 `pyproject.toml`、`uv.lock` |
| schema 与迁移 | 提交时 `SCHEMA_VERSION = 3`：revision 1 基础 DDL（不可变）→ 2 身份（`principals`、`type_grants`）→ 3 snapshot（`snapshot_leases`、`snapshot_items`），均由显式 `migrate` 执行；`check_schema` 不匹配即拒绝；不自动 drop/重建。d0d8875 起为 8（见后两节） | `schema.py` |
| 数据布局 | `storage` schema 公共表 + 每模块独立 schema 的规范化表；payload 以 `canonical_json` 规范文本保存与比较 | `schema.py`、`codec.py`、`reader.py` |
| 写事务 | 每个操作一个短 `READ COMMITTED` 事务，第一步取全库 advisory 锁 `WRITER_GATE`；写、读、snapshot 创建和身份管理共用此闸；认证在取闸后进行；业务数据、依赖、消费标记、Outbox 行和幂等回执同一事务原子提交 | `service.py`、`backend.py` |
| 有界校验 | 每次写只加载有界当前图投影：`Limits` 默认 10 000 节点、50 000 边、32 MiB 投影、2 000 操作/批、8 MiB 请求；超限 `ProjectionLimit` 失败关闭；成本与当前图规模相关，属公开已知限制 | `repository.py`、`backend.py` |
| 身份与授权 | `secrets.token_urlsafe(32)` token，只存 SHA-256 摘要，绑定模块 principal；无隐式授权（含所有者读）；grant 行即读权限，`can_subscribe` 为附加能力；全部 Reader 路径授权，依赖两端、计数均检查，错误详情脱敏；写入需对实际输入类型有读权限 | `auth.py`、`service.py`、`reader.py` |
| `AuthOperator` | 部署环境内部的可信 Python 能力（签发、撤销、整体替换授权；P2 另加消费者名预留与旧标记认领，P3 另加应急 `rotate(principal_id)`，均 d0d8875），不经 token 认证的传输暴露，没有 CLI/HTTP 入口；内部签发**不等于 P3 日常管理或管理反馈流程** | `auth.py` |
| 请求 ID 重放 | 回执按 principal 作用域；同 ID 同内容返回已存结果；重放时按当前凭据并对**原请求实际输入**复核读权限（有回归测试）；不清理幂等回执 | `service.py`、`test_auth.py::test_reused_input_*` |
| snapshot | 有界物化到 `snapshot_items`，在全局写闸下的短 `READ COMMITTED` 读写事务中创建（**不是** `REPEATABLE READ READ ONLY`），无跨请求长事务；每次读取复核 token、租约归属、TTL 与当前读授权；默认 50 000 行、32 MiB、每 principal 4 个活跃租约、TTL 默认 300 s / 最大 3600 s；`sweep_expired` 分批清理（无 CLI 命令） | `snapshots.py` |
| HTTP | FastAPI：`GET /v1/identity`、`GET /v1/registry`、`POST /v1/batches`、`POST /v1/read`、`POST /v1/snapshots`、`DELETE /v1/snapshots/{snapshot_id}`；请求体默认上限 8 MiB、读取超时 15 s。d0d8875 另加 `/v1/consumers/*`（P2）、`/v1/management/requests` 与 `/v1/management/credentials/{feedback_id}/claim`（P3） | `http.py` |
| 远程 SDK | 同步 `RemoteStorageClient` / `RemoteReader`；不读环境变量、无隐藏重试；`write(..., request_id=None)` 默认 UUIDv7；结果未知抛 `CommitUnknown(request_id)`，用同 ID、同 ops、同 context 重放。d7c9919 时 `subscribe` 抛 `NotImplementedError`，P2（d0d8875）已实现（见下） | `remote.py`、`remote_errors.py`、`wire.py` |
| CLI | d7c9919：`ear-storage {migrate,bootstrap,serve}`；只读显式 `EAR_STORAGE_DSN`，不读 `.env`、无默认库；`serve` 不自动迁移。d0d8875 另加 `relay*`（P2）与 `manage-worker`（P3） | `cli.py` |
| CI | 测试步骤预拉 `postgres:17-alpine` 并设 `EAR_RUN_POSTGRES_TESTS=1` | `.github/workflows/ci.yml` |

验证（提交前本地日志 `/tmp/ear-p1http-full-OK7Aeg.log`，exit 0）：ruff check、ruff format --check、pyright（0 错误）、启用真实 PostgreSQL 的 `pytest` **644 passed、0 skipped、1 warning**（Starlette TestClient 的 httpx 弃用警告）75.11 s、`check_docs.py`（含 `--base origin/dev`）、`git diff --check`、`uv lock --check` 均通过。

- 测试位于 `services/storage/tests/`：`test_auth.py`、`test_codec.py`、`test_http.py`、`test_http_postgres.py`、`test_p1a_regressions.py`、`test_postgres.py`、`test_snapshots.py`。
- 默认不设 `EAR_RUN_POSTGRES_TESTS=1` 时 PostgreSQL 测试明确 skip，不能与“全启用 0 skip”混称。
- 启用后只使用 `conftest.py` 起的**独立 Docker 临时库**（`postgres:17-alpine`、`--pull=never`、`--rm`、随机名与密码、只绑 `127.0.0.1` 随机端口、tmpfs）。设置了 `EAR_POSTGRES_TEST_DSN` 时夹具抛 `RuntimeError` 拒绝运行，**不支持外部数据库**。

边界：全局写闸使所有操作串行（保正确不保吞吐）；幂等回执不清理，保留策略需与 I 确定；核心能力项仍为 false。

## P2：Relay、持久消费者与远程订阅（提交 d0d8875，已随 PR8 合入 dev）

依据：ADR-0001（JetStream + Outbox/Relay）、[全局实施计划](../../../ai-docs/plans/mvp-implementation.md) P2、上述用户授权。代码随 d0d8875 提交（与 P3 同一提交）。**未经合并前人工审核**，未通知③④⑤或任何订阅方。P2 完成时 `SCHEMA_VERSION = 5`；d0d8875 为 8（P3 管理 6、凭据 7、订阅重配置 8）。工程细节见[工程指南](persistent-service-plan.md)第 8、9 节（其中消费者部分以本页和代码为准）。

### Relay（Outbox → JetStream）

| 范围 | 实际实现 | 位置 |
|---|---|---|
| 依赖与 CI | `nats-py>=2,<3`；pytest marker `nats`；CI 预拉 `nats:2.11-alpine` 并设 `EAR_RUN_NATS_TESTS=1` | `pyproject.toml`、`ci.yml` |
| revision 4 | `outbox` 投递列与未投递部分索引；单行 `relay_state`（`fence`、`lease_until`、`paused`、`destination`） | `schema.py` |
| 顺序与租约 | 数据库全局唯一租约 + 单调 fence；短事务认领 `(commit_seq, ordinal)` 队首，broker I/O 不在数据库事务内 | `relay.RelayStore` |
| 发布语义 | 拿到 PubAck（校验 stream 与 seq）后才标记已发；标记前退出会重发。`Nats-Msg-Id = event_id` 只在 `duplicate_window`（默认 120 s）内去重：**有限去重的至少一次，不是恰好一次** | `relay.Relay.run_once` |
| 失败与重试 | 失败队首阻塞后续、从不跳过；指数退避 1 s 起、上限 60 s、次数不限；可查询、可暂停；无 Relay 侧死信或跳过命令 | 同上 |
| 流配置 | 不存在才创建：文件存储、limits 保留、`discard=new`、默认 256 MiB / 1 000 000 条、`max_age=0`（无 TTL）、单副本、禁 delete/purge；已存在但不符即 `stream_mismatch`，绝不修改。**流满时拒收新消息**，Outbox 行保持未投递并持续重试；**没有清理机制**，生产保留/容量策略仍欠；默认持久化**不强制每条 fsync**（沿用 JetStream 默认） | `RelayConfig.stream_config` |
| 主题 | `<prefix>.<event_type>.<类型>.<所属模块>`，默认 `ear.events`；`data_changed` 取 `data_type`，`reevaluation_requested` 取 `target_type`；消息体为原样事件 JSON | `RelayStore.subject` |
| 健康 | `relay-health` 只读数据库（`broker_probed=false`），不证明 broker 可达 | `RelayStore.health` |

### 持久消费者与订阅

| 范围 | 实际实现 | 位置 |
|---|---|---|
| 架构 | 服务端授权的 JetStream durable pull consumer，由存储服务代理；模块经 HTTP `POST /v1/consumers/{register,fetch,ack,nack,deadletters,replay}` 使用，不直接接触 NATS。`serve` 设置了 `EAR_NATS_SERVERS` 时才启用，否则 `ConsumerUnavailable` | `consumers.ConsumerService`、`consumer_broker.py`、`http.py` |
| revision 5 | `consumer_identities`、`consumers`、`consumer_deliveries`、`consumer_receipts`；revision 1–4 不变 | `schema.py` |
| 授权过滤 | `data_changed`：本 principal 同时有读与 `can_subscribe` 的类型；`reevaluation_requested`：仅该 principal 为 writer 且 `supports_reevaluation` 的类型，并须能读 cause 类型。每次取件按当前授权复核；已有订阅配置不可变，重新注册不得静默扩大过滤 | `approved_filters`、`authorize_event` |
| 投递状态 | 数据库租约与全局递增 `queue_seq` 定序（不按 UUID 排序）；`ConsumerPolicy` 默认 delivery_lease 30 s、ack_wait 60 s、max_attempts 5、退避 1 s 起上限 60 s；ack / nack 重试退避，超限入死信（`state='dead'`）；`replay` 只把死信放回**同一 consumer**。JetStream 重投只是入口，尝试次数与死信持久在数据库 | `consumers.py` |
| 过期与回执 | 租约过期的 ack/nack 确定返回 `DeliveryExpired`（HTTP 410）；协调结果未知时保留回执，可用同一回执重试协调；过期租约只会重投**同一**事件 | `finish`、`remote.py` |
| SDK | 远程 `subscribe` 注册；`poll_once(consumer=None)` 与 `run_until_idle(max_deliveries=100)`（1–10 000，同时限制轮询轮数）为调用方显式驱动的有界泵 | `remote.py`、`consumer_wire.py` |
| 起点 | 注册默认**只收未来事件**（起点为当前已发布最大 seq + 1）；`from_beginning` 仅限策略允许且写核心类型的模块。P5 的历史恢复补/跳见 P5 节 | `register` |
| 消费标记 | `MarkProcessed` 只能用本 principal 拥有的 consumer 名；P1 遗留标记没有 principal 来源，注册时报 `legacy consumer requires deployment adoption`，**不自动认领旧进度**；须部署内 `AuthOperator.reserve_consumer_identity` / `adopt_consumer_markers` 显式处理，认领后的重放须核对 scope、request_id 与 fingerprint | `service.py`、`auth.py` |

保证边界：整体为**有限去重的至少一次**；消费方仍按事件 ID 去重（`MarkProcessed` 与业务写同批）；不讲恰好一次。

### 验证（P2 完成时的工作区快照，P3 改动前；该代码后随 d0d8875 提交）

- `/tmp/ear-p2b-mustfix/closing-full.log` 与 `closing-full-repeat.log`：启用真实 PostgreSQL + 隔离 NATS 的完整 `pytest` 两次，各 **802 passed、0 skipped、1 warning**（155.42 s / 154.53 s），`.exit` 均为 0。
- `closing-default.log`：默认 603 passed、199 skipped（exit 0）。`closing-ruff`、`closing-format`、`closing-pyright`、`closing-lock`、`closing-docs` 的 `.exit` 均为 0。
- 这些运行对应 P3 改动之前的工作区快照，不代表 d0d8875 或 b7307e9；之后的完整真实运行见 P3 节“检查点与合并后验证”。早先的 `/tmp/ear-p2-*-final.log` 分组结果已被取代。
- 测试只用临时 Docker 容器（PostgreSQL 与 NATS 均只绑 loopback 随机端口）；设置 `EAR_POSTGRES_TEST_DSN` 或 `EAR_NATS_TEST_DSN` 即拒绝运行。只设 `EAR_RUN_NATS_TESTS=1` 而未设 `EAR_RUN_POSTGRES_TESTS=1` 会 `pytest.fail`。
- CI 的 NATS 配置**尚未在远程运行**。此前记录的代码内过时说明文字（`__init__.py`、`pyproject.toml` description 等）本轮未复核。

## P3：动态注册、管理反馈、凭据与订阅重配置（后端提交 d0d8875；⑥管理页未做）

依据：[插件设计](../../../docs/design/plugins-mvp.md)、实施计划 P3、上述用户授权。管理反馈为 revision 6（只增加：`module_lifecycle`、`module_permissions`、`management_authority`、`management_tasks`、`management_audit`；仅凭 revision 号不认定结构完整，不隐式修复本地旧 v5），凭据流程为 revision 7、订阅完整重配置为 revision 8（见下文小节）；d0d8875 的 `SCHEMA_VERSION = 8`。**未经合并前人工审核**；contracts 登记表新增两类型，**尚未通知**相关模块。

| 范围 | 实际实现 | 位置 |
|---|---|---|
| 类型 | `plugin_management_feedback`（raw，writer `api`）承载管理请求；`plugin_management_result`（system，writer `storage`）记录结果与阶段 | `contracts/registry/data-types.json`、`management.py` |
| 入口与权限 | `POST /v1/management/requests`（202），仅 `api` 模块且 principal 带可信管理员角色；角色绑定在 principal 上，**不取自请求体**。动作：`register_plugin`、`enable`、`disable`、`set_permissions`、`status`、`retry_task` | `http.py`、`management.require_admin`、`management_input.py` |
| 处理器 | `ear-storage manage-worker`（读 `EAR_STORAGE_DSN`、`EAR_MANAGEMENT_WORKER_TOKEN`、`EAR_NATS_*`）以消费者方式处理反馈，经 `ManagementProcessor.poll_once` | `cli.py`、`management.py` |
| 注册 | 运行时数据库登记表，动态创建模块 namespace；重复请求不重复建资源 | `management.py` |
| 启停 | `enable` 在资源就绪后才生效；`disable` 关闭授权闸门、防止复权；停用期间错过的历史由恢复时的补/跳选择处理，见 P5 节。`status` 报告存储的生命周期/任务状态，**不等于插件在线** | `_prepare_enable`、`_interrupt`、`_status` |
| 恢复 | 资源未就绪时重试，耗尽后收敛为 `failed` / `recovery_required`；`retry_task` 须用新的管理反馈（新 request_id + `target_feedback_id`），复核两次请求管理员的当前权限，只恢复原任务在原 consumer 中的死信，worker 凭据须属于原 consumer principal | `reconcile`、`_retry_task` |
| 订阅变更 | 扩容与收缩走完整重配置（revision 8，见下文小节），不再以 `consumer_reconfiguration_required` 拒绝扩容。`management.py` 中仍有一条路径（订阅降为空而已存在 managed consumer）返回该错误码，合并前审核时核对是否符合预期 | `management.py`、`reconfiguration.py` |

未完成：⑥管理页对接；`AuthOperator` 内部签发不等于日常管理。

### 验证（管理与消费者目标组，schema 6 时的工作区快照，早于 d0d8875）

- `/tmp/ear-p3-reliability-management-main.log` 47 passed（4 deselected）+ `management-authority.log` 4 passed（20 deselected）= 管理 51；`consumers-final.log` consumer 目标组 52 passed；合计 **103 项目标测试通过**（真实 PostgreSQL + 隔离 NATS）。
- `ear-p3-reliability-default.log`：默认 615 passed、239 skipped。`checks.log`：pyright 0 错误、文档检查通过。
- 当时**没有**完整启用真实运行：一次完整/管理全量运行中断（`management-final.log` 不完整），不计成功；此缺口后由“检查点与合并后验证”中的 916 项运行补上。`target-initial` / `target-second` / `type-second` 为中途失败的迭代记录。

### 凭据轮换、撤销与一次性领取（schema revision 7，d0d8875）

| 范围 | 实际实现 | 位置 |
|---|---|---|
| 动作 | 管理反馈 `rotate_credentials` / `revoke_credentials`，与其他管理动作同一入口和可信管理员角色；执行器复核提交管理员的当前权限后才进入凭据阶段；核心模块没有生命周期行，不能走此流程 | `management.py`、`management_input.py`、`credentials.stage_credentials` |
| 身份 | 每个受管插件一个稳定 principal（active 或 revoked）；首次签发时一次性复制已批准的模块授权，轮换不重建授权；同模块存在多个旧 principal 时轮换失败关闭（`credential_identity_not_ready`），撤销则全部停用 | `stage_credentials` |
| rotate | 阶段提交即作废旧 token（`active=false`、`token_hash=NULL`、`credential_epoch+1`），并写入待领取项（绑定反馈、principal、epoch、模块 revision，有效期 300 s）；领取成功前**没有**可用 token | 同上 |
| revoke | 停用该模块全部 principal、清空摘要并递增 epoch，使待领取项作废 | 同上 |
| claim | `POST /v1/management/credentials/{feedback_id}/claim`：仅限原提交者，且仍是 active 的可信 `api` 管理员；须任务 `succeeded`、待领取且未领取、300 s 内、模块资源就绪且 revision 未变、principal epoch 一致、仍是同一 principal（模块没有其他 principal）。先提交摘要与已领取状态再返回明文；200 只有一次且 no-store，不可用 403，认证无效 401 | `credentials.claim_credentials`、`http.py` |
| 明文 | `secrets.token_urlsafe(32)`，只出现在 claim 响应中；数据库只存 SHA-256 摘要，交换表只存授权元数据，worker 看不到明文 | `credentials.py`、`auth.token_digest` |
| 响应丢失 | 不能重放 claim 取回明文，须提交**新的** `rotate_credentials` 反馈；凭据流程不会自动恢复授权或启用模块 | 同上 |
| 应急 | 部署内 `AuthOperator.rotate(principal_id)`：保留 ID 与授权，递增 epoch 使待领取项作废，不启用模块、不恢复授权；不是日常管理入口 | `auth.py` |

验证（凭据完成时的工作区快照，schema 7，早于 d0d8875）：日志 `/tmp/ear-p3-credentials-{final,refresh}-*.log`，状态 `/tmp/ear-p3-credentials-state.json`。目标组去重后 **216 项通过**（203 项真实 PostgreSQL/NATS 标记测试、13 项输入/单元检查，目标组 0 skip），覆盖领取回滚、schema 6 旧管理权限保留、旧多身份拒绝轮换/撤销全部等；默认 `pytest` 616 passed、267 skipped；ruff、format、pyright、check_docs 均 exit 0。这些是当时的目标组快照，**不代表** d0d8875 或 b7307e9，也不是完整启用运行。

### 订阅完整重配置（schema revision 8，d0d8875）

| 范围 | 实际实现 | 位置 |
|---|---|---|
| revision 8 | 只增加：consumer 代状态（`prepared` / `active` / `retired`）与 `consumer_reconfigurations`（每模块最多一个 `prepared`）；准备中的模块拒绝并发管理变更（`reconfiguration_in_progress`） | `schema.py`、`management.py` |
| 扩容 | 新增类型只从切换时按类型记录的提交 watermark 之后投递（不自动补历史；显式范围补处理见 P5 节）；原有类型的积压与在途回执保留、不跳过；consumer 保留 owner、回执与投递 ID，按原 consumer 事件身份排除已完成工作 | `reconfiguration.prepare` / `activate`、`consumers.py` |
| 切换 | 多个 consumer 的 broker 资源在数据库事务外准备，部分失败可重试且重复请求稳定；最终在一个数据库事务内原子切换；退役资源保留，不做破坏性补偿；触及硬上限时失败关闭，待部署维护 | `prepare`、`install`、`activate` |
| 收缩 | 先提交限制并关闭授权闸门（不依赖 broker 配置）；后续准备或 broker 失败时闸门保持关闭，待处理数据保留并给出恢复选择 | `withdraw`、`cancel` |
| 身份与竞态 | 已撤销或停用的 principal 可准备资源，但**不复活** token 或授权；owner、授权、凭据 epoch 的任何变化（含停用后复现的 ABA）都会使准备失效；并发中断不能激活，也不留下终态意图 | `identities`、`activate` |
| ack | 退役代的回执在当前已无权限时记为 `retired_ack_cleanup_pending`、保留待清理，不阻塞当前已授权的新投递，清理不绕过安全闸门；普通 ack 仍按当前授权复核 | `consumers.py` |

测试：`services/storage/tests/test_reconfiguration.py`（9 个测试函数，部分参数化；需真实 PostgreSQL + 隔离 NATS）。

### 检查点与合并后验证

- merge 前 P3 检查点（d0d8875 的代码）：`/tmp/ear-p3-checkpoint-validation.log`，启用真实 PostgreSQL + 隔离 NATS 的完整 `pytest` **916 passed、0 skipped**，338.05 s，exit 0。
- b7307e9（合并 dev 4afeae0 后）：默认 `pytest` 657 passed、298 skipped（3.57 s）；pyright 0 错误；ruff（89 个文件）与 check_docs（28 个文档）通过。b7307e9 本身未做完整真实运行，不能把 916 记为 b7307e9 的结果；合并后首次完整真实运行在 106cd2e，见 P4 节。
- 远程 CI 尚未运行这批代码。

## P4、P5（未完成）

P4：①侧规则集合守护（f747e89，schema revision 9）与重判完成记录（106cd2e）已提交，多起因已提交 daa767f（已推送），⑦调用记录的①侧存储已提交 e9391dd 并推送（见下），**不是 P4 完成**。⑦网关服务与实际模型调用未做；③④⑤⑦尚未接入完成记录、守护与调用记录，①侧提供存储协议不代表业务对齐完成。P5 ①侧插件历史恢复已在本分支实现（见 P5 节）；⑤实际调度、④业务改动、多层集成与示例插件未做，**不是 P5 完成**。

### 集合守护（schema revision 9，提交 f747e89）

- 接口：`Reader.type_revision(data_type) -> int`（内存、PostgreSQL 服务、HTTP 与 snapshot reader）；提交期操作 `AssertTypeRevision(data_type, expected_revision)`，`expected_revision` 须为非负整数（拒绝 bool）。不匹配返回可重试冲突 `collection_changed`：须在同一 snapshot 内重新查询并取 revision 后重算，不能原样重提交。
- 一致性：查询结果与 revision 必须取自**同一 snapshot**；两次独立的实时调用不构成一致的一对。
- 计数：该类型的实际业务变更（原始、语义、系统记录写入及终止版本）使 revision 增加；unchanged、去重、仅元数据变化（如依赖登记刷新）、请求重放与回滚不增加。
- 守护：只在提交期生效，不产生事件或依赖边；在批开始和批末最终状态（含管理任务隐式写入的结果记录）原子校验。它只拒绝并发变化，不扫描历史；规则编辑不会因此自动重判历史（⑤门控不变）。
- 授权：需要读权限，不需要拥有该类型；snapshot 读取与已存请求重放按当前权限复核；未知类型与无权类型的失败相同。
- 迁移：revision 9 为显式迁移，新增 `type_revisions` 表；已有类型以其已持久化记录的最大 `commit_seq` 为初值——不是 watermark，也不是历史变更次数；新类型（含动态注册，与登记同一事务）从 0 开始。
- 旧快照：revision 8 时创建的 snapshot 租约仍可读；其中没有 revision 元数据时 `type_revision` 明确失败，不用当前计数冒充。
- 未改：内置核心类型能力项仍全为 false（未默认开启）；④代码与默认登记表未改。既有测试只改了 schema 8 静态等式与三处 snapshot 条目数断言（因新增 revision 元数据）。

验证（本地定向，基于 b7307e9 的 P4 代码；摘要 `/tmp/ear-p4-collection-close.json`，日志 `/tmp/ear-p4-*.log`，各组 exit 0）：

- 新增 32 项：`libs/ear_storage_client/tests/test_type_revisions.py` 19 项（单元）、`services/storage/tests/test_type_revisions.py` 13 项（真实隔离 PostgreSQL、TCP HTTP、迁移与并发）。
- 默认 `pytest`：676 passed、311 skipped；ruff、ruff format（91 个文件）、pyright（0 错误）通过。
- 真实 PostgreSQL/NATS 分组：type_revisions 13、迁移 28、snapshot 38、授权 42、管理 27、管理 NATS 6、持久化/HTTP 43。各组互有重叠，**不能相加**，也**不等于**完整真实运行（全部 987 项未跑）。
- 远程 CI 未运行。

### 重判完成记录与权威查询（提交 106cd2e）

- 范围：按所属方验证的重判完成记录与权威查询，供③④⑤及插件后续接入；①只做存储与校验，不承担业务完成判断。
- 验真：完成协议只以①的权威索引验证请求与所属方，不信任调用方自报。
- 能力：类型须在登记表显式声明 `reevaluation_result` 能力才受完成协议保护；普通名称后缀不会自动获得保护；旧的同名类型保持兼容。
- ④：④的入队 marker 只表示已入队，不是业务完成；本提交未改④业务代码。
- 验证：`/tmp/ear-p4-completion-full.log`，真实 PostgreSQL + 隔离 NATS 完整 `pytest` **1091 passed、0 skipped**，日志内 EXIT=0（DURATION=382.71）。远程 CI 未运行。

### 多起因（提交 daa767f，基于 106cd2e）

- 范围：契约 `data-changed`、`reevaluation-requested`、`reevaluation-result`、`semantic-record` schema 及 multi-origin 正例与 depth 反例；`ear_contracts`；`ear_storage_client`（context、completion、engine、ops、wire）；服务端新增 `causality.py` 并改 backend、consumers、reader、repository、service、snapshots。已提交 daa767f 并推送。
- 兼容：原有 `cause` / `root` / `depth` 含义不变，仍表示主起因。
- 新增 `additional_causes`：每项（`TracedCause`）只有自己的 `cause`、`root_feedback_id`、`depth`（≥1），不含 `request_event_id` 或 `trigger_request_event_id`。请求关联在别处：`request_event_id` 位于 Context、语义记录与 `DataChanged`；`ReevaluationRequested.trigger_request_event_id` 指向上游请求。关联必须指向真实已提交的请求，不能指向自身（拒绝自循环）。
- 执行中：新增来源须先在 Context 中预合并，并与 Finish 一致，保证后续事件携带完整来源。
- 权限：逐 origin 复核读权限，覆盖历史读取、结果、消费者投递与旧消费者代的待确认消息。
- 验证：`/tmp/ear-p4-causality-full.log`，真实 PostgreSQL + 隔离 NATS 完整 `pytest` **1168 passed、5 skipped**（JUnit `/tmp/ear-p4-causality-full.xml`：共 1173 项，0 failed、0 error），日志内 EXIT=0（DURATION=436.33；`/tmp/ear-p4-causality-full.exit` 为 0）。5 个 skip 均为 `memory` 参数化用例，该后端不适用认证路径（跳过原因 requires authenticated PostgreSQL），不是缺少真实环境：`test_association_authorization_and_replay_without_finish[memory-target]`（requires authenticated PostgreSQL）；`test_association_authorization_and_replay_without_finish[memory-primary]`（requires authenticated PostgreSQL）；`test_association_authorization_and_replay_without_finish[memory-secondary]`（requires authenticated PostgreSQL）；`test_new_context_origin_permission_and_source_metadata_precedence[memory]`（requires authenticated PostgreSQL）；`test_dependency_can_read_foreign_upstream_and_publish[memory]`（requires authenticated PostgreSQL）。默认 `pytest` 779 passed / 394 skipped 为此前历史结果。远程 CI 未运行。

### ⑦调用记录 `llm_call` 与正文策略（提交 e9391dd，基于 daa767f，已推送）

- 范围：契约 `contracts/records/llm-call-record.schema.json` 及正反例（metadata、正文正例；无错误码的 failed、负 token 数、错误正文类型反例）；登记表新增核心类型 `llm_call`（system_record，writer `llm_gateway`，能力项 `llm_call_metadata`）及登记表 schema；`ear_contracts/calls.py`（`LLMCallRecord`、`LLMUsage`）；SDK `model_body.py`（`ModelBodyPolicy`）、`engine.py`、`memory.py`；服务端 backend、causality、cli、http、repository、service。测试 `services/storage/tests/test_llm_calls.py`、`test_model_bodies.py`，另改 `test_http.py` 与 `libs/ear_contracts/tests/test_ear_contracts.py`。字段以 schema 与 `calls.py` 为准。
- 写入：⑦网关用 `PutSystemRecord` 写 `llm_call`，envelope id 即稳定 call_id；只有 `llm_gateway` 能写；每个逻辑调用一条终态记录（`success` / `failed`），写后内容不可替换；系统记录不发事件。只有登记为 `llm_call`、writer `llm_gateway` 且 `llm_call_metadata=true` 的条目适用本协议；该能力项不是正文开关。payload 封闭，未知字段拒绝；数值、UTC 时间、请求与起因由 schema 和①校验。
- 身份：`caller_module`、`model` 是⑦网关的断言，可与请求所属方不同；不是①对远端调用方或模型的认证证明。写入者身份仍只来自 token。
- 请求关联：`request_event_id` 须指向真实已提交请求；记录保存该请求的全部起因（请求原主起因作为额外追溯保留），伪造或丢失来源的写入被拒绝。
- 读取：当前、历史、查询、计数、snapshot 需要 `llm_call` 读授权，并能读全部已保存起因的类型；写入与重放另复核已保存的请求目标与起因。按调用方或根反馈查询用现有 `Query("llm_call", where={"caller_module": ..., "root_feedback_id": ...})` 与 `count`，没有新增专用查询接口。读取按类型授权，没有逐字段 ACL。
- 正文：默认只存 metadata。`request_text` / `response_text` 为可选严格字符串；null 或缺省视为未提供并在序列化时省略，空字符串视为已提供。写入任一正文须部署级 `ModelBodyPolicy` 开启且调用方在范围内：默认 `enabled=False`、`max_bytes=65536`（两项正文累计 UTF-8 字节，超限拒绝、不截断）、`allowed_callers=None` 允许全部调用方断言、`()` 全部拒绝、元组为精确模块名匹配。策略只由部署方经构造参数（`MemoryStorage`、`PostgresStorage`、`StorageService`、`create_app_from_dsn`）或 `ear-storage serve` 的环境变量设置：`EAR_MODEL_BODY_ENABLED`（严格小写 `true` / `false`，缺省 false）、`EAR_MODEL_BODY_MAX_BYTES`（正十进制整数）、`EAR_MODEL_BODY_ALLOWED_CALLERS`（逗号分隔精确模块名；未设为全部，空串为无），非法值失败关闭。写请求不能自行开启。①只保存调用方显式提供的文本，不采集环境、请求头或网络；生产方不得放入凭据或 API key。
- 关闭与保留：关闭策略后新写入正文被拒绝；已提交的同一请求 ID 按当前授权仍可重放，结果只有 OpResult，不含正文或记录。既有正文不删除、不隐藏，仍按原类型与起因 ACL 可读；没有 TTL 或清理。
- 迁移：schema revision 保持 10，本切片无 DDL；`llm_call` 是新核心登记类型，已有库只跑 `migrate` 不会加入，须显式可信 `bootstrap`。
- 未做：⑦网关服务与实际模型调用（本切片未改⑦服务）；③④⑤⑦调用方接入；正文保留期/TTL 与清理；前端；包含本切片的完整真实 PostgreSQL + NATS 运行。
- 验证（本地工作区；状态 `/tmp/ear-p4-bodies-state.json`）：默认 `pytest` **875 passed、449 skipped**（`/tmp/ear-p4-bodies-default.log`，exit 0）；启用真实后端的定向分组：正文 66 passed（`/tmp/ear-p4-bodies-real-body.log`）、调用记录 metadata 74 passed（`/tmp/ear-p4-bodies-real-calls.log`），均 exit 0。两组有重叠，**不能相加**，也**不是完整运行**。静态检查 `/tmp/ear-p4-bodies-static.log` exit 0（ruff、format 104 个文件、pyright 0 错误、check_docs）。e9391dd 本身未单独做完整真实运行；包含它的完整真实运行只有 P5 工作区那次（1 failed，见 P5 节）。远程 CI 未运行。

### P5 ①侧插件历史恢复（schema revision 11–14，本分支已实现，基于 e9391dd；提交状态以提交历史为准）

- 范围：服务端新增 `history.py`、`history_execution.py`、`history_scheduler.py`，并改 `management.py`、`management_input.py`、`consumers.py`、`reconfiguration.py`、`schema.py`、`service.py`、`http.py`；SDK `engine.py`、`remote.py`。新增测试 `test_history.py`、`test_history_execution.py`、`test_history_scheduler.py`、`test_history_bridge_fixes.py`、`test_recovery_fixes.py`，另改 `test_completion.py`、`test_llm_calls.py`、`test_management.py`、`test_management_integration.py`。签名与字段以代码为准。
- 管理入口（经 `plugin_management_feedback`，仅可信管理员）：恢复选择 `resume_choice`（`catch_up` / `skip`）必须同时带 `expected_revision`（模块当前生命周期 revision，过期为 `stale_recovery_revision`）；显式范围补处理 `action="history_backfill"` 须给 `rangeStart` / `rangeEnd`（非空半开区间）与 `input_types`，输入类型须在模块当前读权限内（否则 `history_input_not_authorized`）；范围字段只允许用于 `history_backfill`（`management_input.py`、`history.authorize`）。
- 补处理：①持久化按对象**当前状态**生成的候选（`history_items`，`current_state` 条目），不盲目重放旧事件。
- 跳过：旧结果与依赖登记不改，不伪造 Finish 或完成记录；只在资源就绪后推进所选类型的游标与交接（`history.switch`；`catch_up` 须对应作业 `completed` 后才切换）。
- 交接：watermark W 之后的事件按模块/类型保留交接（`module_type_handoffs`），即使该模块此前没有 consumer（首次 consumer）也保留。
- 门控：同模块另有未结束的已授权恢复决定时不推进游标或投递（重叠失败关闭、`recovery_in_progress`）。对获准读取但未订阅的上游，同样检查当前字段依赖是否过时；发现本模块自有的受影响结果时，进入需⑤真实请求的历史任务。
- 执行（插件所属方）：`POST /v1/history/jobs/{job_id}/claim`（SDK `claim_history_job`，无参数）领取条目，带 snapshot、租约与 epoch/fence，库中只存 claim 哈希、不存明文 token；`POST /v1/history/jobs/{job_id}/commit`（`commit_history_job(job_id, claim_token, fence, ops, context, *, request_id, outcome="completed", error_code=None)`）使业务写入与进度回执同一事务原子提交；`GET /v1/history/jobs/{job_id}`（`history_job`）查状态。
- 供⑤：`POST /v1/history/reevaluations/list`（`list_history_reevaluations(job_id=None, *, limit=50, cursor=None)`）；`POST /v1/history/jobs/{job_id}/requests`（`attach_history_request(job_id, item_id, request_event_id)`）只接受真实已提交请求，复核权限与起因覆盖；所属方经 `POST /v1/history/jobs/{job_id}/assignments/claim`（`claim_history_assignment(job_id, *, consumer)`）安全领取分配——consumer 须已登记且启用或部署预留给本 principal，至少一次交接，重复领取返回同一事件/consumer——之后由所属方自行 Finish，①与⑤不代为完成。完成索引与历史依赖按 `commit_seq` 验新鲜；作业终态后冻结（`history_job_final`）。
- 迁移：显式迁移 11–14（`schema.py` `DDL_V11`…`DDL_V14`，`SCHEMA_VERSION = 14`）：11 引入历史作业与条目等表；12 条目租约与原子分块回执（`history_receipts`）；13 已批准的按模块/类型交接与可审计替代（`recovery_decisions`、`module_type_handoffs`）；14 已批准的调度范围与只追加的请求分配（`history_reevaluation_authority`、`history_request_links`）。
- 未做：⑤实际调度（①只提供接口，未改⑤）；④业务改动；真实多层/分叉汇合示例插件与演示；⑥管理页的恢复/补处理选择；部署 Compose、备份与清理。**不是 P5 完成。**（当时状态；现状见文末“⑥⑦ 与①的真实边界”）
- 验证（本地工作区；不是全绿）：
  - 完整真实 PostgreSQL + NATS：`/tmp/ear-p5-checkpoint-full.log`（`/tmp/ear-p5-checkpoint-full.exit` 为 1，JUnit `/tmp/ear-p5-checkpoint-full.xml`）**1 failed、1411 passed、5 skipped**（共 1417），DURATION=717.45。唯一失败 `services/storage/tests/test_llm_calls.py::test_existing_database_requires_explicit_bootstrap`：测试硬编码 schema revision 11，实际为 14。已修正：测试改用 `SCHEMA_VERSION`，不再硬编码；定向重跑 `/tmp/ear-p5-bootstrap-fixed.log` 1 passed（3.70 s，`/tmp/ear-p5-bootstrap-fixed.exit` 为 0）。修正后未重新完整运行，上面的 1 failed 仍是最近一次完整运行的结果；定向通过不等于完整运行通过。5 个 skip 明细见 JUnit，本页未逐项核对。
  - 默认 `pytest` 876 passed / 541 skipped（`/tmp/ear-p5-default.log`）为此前历史结果。此前各定向组（history、execution、scheduler、management、reconfiguration、consumers、completion 等）互有重叠，**不能相加**。
  - 静态：此前 ruff、format、pyright 与 check_docs 通过（`/tmp/ear-p5-{ruff,format,pyright,docs}.log`）；测试修正后主会话最新一轮 ruff check、ruff format --check（112 个文件）、pyright（0 错误）、`uv lock --check`、check_docs（28 个文档）与 `git diff --check` 均通过。本批（P5 实现与该测试修正）待提交，提交状态以提交历史为准。远程 CI 未运行。

### 提交更新与存储切片部署（本分支已实现，提交状态以提交历史为准）

- 提交：P5 已提交 9705b61（上文 P5 节中的“待提交”“修正后未重新完整运行”是当时状态，已过时，该节历史数字不改）；②适配器经合并提交 84f20fe 同步；部署切片已在本分支实现，提交状态以提交历史为准。其后的三开启本地完整运行（见下）无失败，覆盖了该测试修正。（当时状态；PR8 已合并、远程 CI 已通过，见上方“当前状态”。）
- 部署切片（本分支已实现，提交状态以提交历史为准；**已随 PR8 合入 dev、无生产启动**）：`deploy/compose.yaml`、`.env.example`、`Dockerfile`、`runtime.py`、`postgres-init.sh`、`nats.conf`；服务端 `health.py`（`HealthProbe` 只读 ready、`WorkerHealth` 最近成功轮询）、`deployment.py`（`init_identities` 只写 `deploy/.env` 或容器身份路径，0600、原子发布、不重签）、`cli.py` 与 `http.py` 改动；测试 `test_deployment.py`、`test_deployment_hardening.py`、`test_plugin_chain_e2e.py`；备份工具 `tools/storage_backup.py` 与 `tests/test_storage_backup{,_real}.py`。操作流程、健康差别、故障检查与卷保留见[隔离部署指南](deployment.md)。
- 验证：容器冒烟 `/tmp/ear-storage-final-smoke.log`（`.exit` 0）16 项 PASS，结束时清理随机名项目；插件链 `test_plugin_chain_e2e.py` 2 passed，使用**固定⑤测试驱动**，不是⑤真实调度；二者结果不变。
- 备份/恢复（新版，隔离环境）：真实往返与五种真实同名竞争通过——`/tmp/ear-final-race-84lx9T/real.log` 为 1 passed in 100.61 s，汇总 `/tmp/ear-final-race-close.json`（`roundtrip.phase=passed`、`production_touched=false`、`cleanupFailures=[]`）；PostgreSQL、NATS、身份与数据库数据校验通过。历史失败（`/tmp/ear-backup-main-final.log` exit 1，早于新版）已被取代，不是最新结果：根因定位为 Compose `create --no-recreate` 停止其他项目的容器（`systemd-repro` 复现项 exit 1，不是新版结果），现不再使用该命令。恢复只进独立的新项目，成功必须读取 JSON `.project` 并手动以该名字启动，`--start` 不接受，不在原目标项目执行 `up`；源需 `--allow-downtime`，备份必须可信（`--trust-backup`），manifest SHA-256 走独立渠道。仅隔离环境验证，**不是生产运行手册**，未生产使用。
- 三开启本地完整运行 `/tmp/ear-repro-suite-ykxiUWmy/{pytest.log,exit}`（`EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS` 均为 1，`uv run pytest -q`）：已结束，`exit` 为 0，末行 **1503 passed, 5 skipped, 1 warning in 890.20s (0:14:50)**；同目录 `static.exit` 为 0（pyright 0 errors，文档检查通过）。5 个 skip 均为 `memory` 参数、原因 `requires authenticated PostgreSQL`，与既有 `test_causality_context_guard.py` 的三处 skip 一致；日志只有汇总行，未逐项列出。对应运行时本分支的工作区（运行开始后无非 Markdown 文件被修改）。这是本地一次运行，不替代远程 CI 与负责人兼 I 的人工审核，不是生产验收。
- 未做（当时状态，现状见文末“⑥⑦ 与①的真实边界”）：⑥管理页对接、⑤实际调度对接、保留/清理（流容量、幂等回执、snapshot 清理调度、退役资源）、备份文件的保留与删除（工具不提供）、升级迁移与管理员 token 交付流程；生产验收未做（PR8 已合并、远程 CI 已通过，见“当前状态”，但不是生产验收）。

### 运维交接（备份/恢复，简短）

- 隔离环境验证，未生产、未合并；详见[隔离部署指南](deployment.md)第 8 节。
- 备份：`--project` + `--allow-downtime`（停源的应用与 NATS）。恢复：只进独立新项目；备份必须可信（`--trust-backup`），manifest SHA-256 经独立渠道传入 `--trusted-manifest-sha256`；成功必须读 JSON `.project` 并手动以该名启动，`--start` 不接受；不在原目标项目执行 `up`。
- `deploy/.env` 只许新建（noclobber），已存在即停止。
- 三开启本地完整运行：已结束，exit 为 0，1503 passed / 5 skipped（5 个需要认证 PostgreSQL 的 `memory` 参数 skip，见上）；该配置随后已在 dev 的远程 CI 通过（583.04s），仍不是生产验收。
- 其余未做（保留/清理、⑤实际调度对接，以及⑥ 页面与真实①的浏览器联调等）不因备份通过而改变；⑥⑦ 现状见下一节。

### ⑥⑦ 与①的真实边界（`api/gateway-mvp` 功能分支，待合并，基线 dev 376294d；以本节和“当前状态”为准）

- **性质**：⑥（`services/api`）与⑦（`services/llm_gateway`）是**部分交付、待审**，不是完整 MVP，也不属于①切片的验证；代码在功能分支 `api/gateway-mvp`，待合并，尚未合入 dev。上文各节里“缺：⑥管理页对接/管理页面”“未做：⑥管理页的恢复/补处理选择”等措辞是写下时的状态，保留原文作为历史；⑥ 页面现已在 `services/api` 实现（功能分支待合并）。
- **①侧新入口（功能分支待合并，不在 dev 376294d）**：`GET /v1/management/plugins`、`POST /v1/llm/preflight`、`POST /v1/llm/call-id-check` 及 SDK，规则见[接口说明](interface.md)。旧①缺这些入口时，⑦ 对相应调用失败关闭（503），不回退普通读取。
- **验证边界**：

| 检查 | 结果 | 不能说明什么 |
|---|---|---|
| 真实 Chromium 对 `create_app` + 回环**内存假①** | 34 项通过 | 不是真实①：没有真实管理处理器、PostgreSQL、NATS |
| `test_api_gateway_integration`（真实回环①/⑥/⑦ + 隔离 PostgreSQL，NATS 开关开启，合成 provider；12:07） | 26 passed（与 `test_llm_preflight` 47 passed/6 skipped 合计 73 passed/6 skipped） | 只覆盖 API/网关 HTTP 层；管理处理步骤由测试显式驱动；不含浏览器、⑤调度、真实模型/Octopus、生产 |
| 全套·当前（`/tmp/ear-67-full-v2.log`，14:09 起、14:26 结束，四个开关全开） | 退出 0：2067 passed、11 skipped、2 warnings、1001.65 秒 | 合成/隔离环境；11 个跳过为需要认证 PostgreSQL 的 `test_causality_context_guard.py` ×5、`test_llm_preflight.py` ×6；不是生产验收 |
| 静态检查·当前（`/tmp/ear-67-static-final.log`，14:36） | 退出 0：ruff check/format --check、pyright、uv lock --check、check_docs（含 `--base origin/dev`）、git diff --check、Node UI 测试 | 只是静态检查 |
| 全套·历史失败（保留，12:35） | 3 failed、2058 passed、11 skipped（部署烟测缺镜像变量 ×2；旧 storage 镜像入口文件权限 ×1） | 未通过；已按根因修复，并由上面 14:26 的全套重跑取代 |
| 静态检查·历史失败（保留，`/tmp/ear-67-static-v2.log`，14:26） | 退出 1：`ruff format --check` 要求重排 ⑦ README 的 Python 示例代码块，其后各项当次未运行 | 仅格式；已修复，14:36 整条链重跑退出 0 |

- **仍未做**：真实浏览器↔真实①的页面联调；⑥⑤① 之间历史补处理的联调（⑤调度未实现）；⑥ 外部动作执行/确认/风险重试（HTTP 501，缺④协议）；⑦ 真实模型调用与 Octopus 生产 wire 验证（只有合成回放）；③⑤和插件接入网关、④恢复与重判关联接入；⑦ 正文保留期；生产部署。

未完成项不得因文档检查通过而标为完成；进度更新必须对应实际提交和测试结果。
