---
audience: ai
doc-type: status
based-on: origin/dev 48d3c51 的代码与契约；docs/design/design-v1.2.md；docs/design/plugins-mvp.md；ADR-0001；6cea0b7 实施准备（当时无实现改动）；storage/implementation-preparation 分支 P0a 提交 a6a1c5a、P0b 提交 cf337d1（HEAD）；其后本分支实现的 P1（提交记录为准，未合并；范围：services/storage/src、services/storage/tests、ear_storage_client 的 engine/wire/remote/remote_errors 及 memory 改动、根 pyproject/uv.lock、CI）与提交前完整检查日志 /tmp/ear-p1http-full-OK7Aeg.log（exit 0）；项目负责人（用户）采用推荐方案并授权功能分支持续实现、分批提交推送提 PR、合并前由负责人兼 I 审核
---

# ① 实现状态与新目标差距

[设计](../../../docs/design/design-v1.2.md)、[插件](../../../docs/design/plugins-mvp.md)和[ADR](../../../docs/adr/0001-storage-and-runtime-modules.md)已采纳；采纳决定不等于已实现。本分支已有 P0a（a6a1c5a）、P0b（cf337d1）提交，以及本分支实现的 P1（提交记录为准，未合并；见文末）；三者均未经合并前审核。所有权及接口目标见[人读说明](../../../docs/modules/storage.md)。

## 已有代码

- contracts：记录、事件、依赖、注册表Schema和示例，部分DRAFT；P0b 增加依赖 `fields` 与登记表能力项。
- ear_contracts：公共模型、UUIDv7、UTC、登记表及一致性测试。
- ear_storage_client：同步Protocol、批次操作、结果/冲突、查询、MemoryStorage、进程内事件工具；P1 起另有共享纯逻辑 `engine.py`、线路编解码 `wire.py`、同步远程客户端 `remote.py` / `remote_errors.py`（本分支实现，提交记录为准，未合并）。
- ear_storage（`services/storage/`，本分支实现，提交记录为准，未合并）：PostgreSQL 后端、身份与授权、snapshot 租约、HTTP、CLI。

## 已决定，尚未实现

| 目标 | 差距 |
|---|---|
| 独立服务、数据库和总线 | P1 已有服务包、迁移、PostgreSQL 事务与持久 Outbox 行（本分支实现，提交记录为准，未合并）；**缺 Relay、JetStream、远程订阅**（P2）及部署 Compose/运行环境（与 I 协作） |
| 字段依赖/强制登记/防环 | 内存（P0b）与 PostgreSQL（P1，全局写闸下批末检查）均已实现；核心类型的 `requires_dependencies` 全为 false，需③④迁移验证后按类型启用 |
| 动态注册与可信权限 | P1 已有 token 身份、全部 Reader 路径授权与部署内 `AuthOperator`；**缺**动态持久注册、凭据轮换/撤销流程与管理入口、订阅授权的实际投递（P2/P3） |
| 管理反馈 | 缺①管理消费者、可恢复阶段、管理状态和⑥页面/API（P3）；`AuthOperator` 不能代替 |
| 完成记录与调度交接 | 缺③④及插件通用结果类别、多起因与请求关联的兼容契约（P4） |
| 最新规则/纯Markdown/选择结果 | 主要属于③④⑤业务对齐；①提供所需类型、依赖及事务支持（P4） |
| 插件多层/历史处理 | 缺启停与快照游标、恢复/补或跳、多层集成与权限撤销测试（P5） |

## 下一步

P1 提交、推送后随 PR8 接受合并前审核（负责人兼 I），提交状态以分支提交记录为准；CI 已改为运行真实 PostgreSQL 测试，但**尚未在远程运行过**这批 P1 代码，需以首次远程 CI 结果为准。之后按[全局实施计划](../../../ai-docs/plans/mvp-implementation.md)进入 P2（Relay/JetStream/远程订阅），再 P3 动态管理和插件链集成。部署与I合作，页面归⑥，传播调度归⑤；不要让①包办业务模块。

不要把 `test_memory.py::test_concurrent_writers`（顺序旧版本冲突）当真正并发测试；真实多进程竞争在 `services/storage/tests/test_p1a_regressions.py`。SDK用法与命令见[工程指南](interface.md)。

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
| 契约 | `DependencyRecord` 顶层可选 `fields`（字符串、非空、不重复、不接受 null）；登记表能力项 `requires_dependencies`、`supports_reevaluation`（缺省 false，仅 semantic/rule）；8 个新正反例；`data-types.json` 未改 | `contracts/records/`、`contracts/registry/data-types.schema.json` |
| 模型 | `DependencyRecord.fields`（省略时序列化不输出）；能力项 from_json/to_json 往返；`Registry.core_types`、`is_core`、`extend`（插件）、`extend_core` 与 `from_json/load(trusted_core=)`（受信 bootstrap）；JSON 不作为可信来源 | `ear_contracts/records.py`、`registry.py` |
| SDK | `DependencyInput(ref, fields)`：去重、按码点排序，拒绝空、单字符串、非字符串；带 `fields` 的 `VersionRef` 以 `InvalidRequest` 拒绝（行为变化）；`Reader.dependency_set()` / `DependencySet` 区分从未登记（None）与空集；`StaleDependency.fields`；冲突 `dependency_cycle`（不可重试） | `ops.py`、`queries.py`、`protocol.py`、`results.py` |
| 内存 | 同对象同版本合并（整个对象优先，否则字段并集），版本/类型不同拒绝；字段模式按 `canonical_json` 比较所选字段（缺失≠null），整体替代始终过时；需依赖类型 Create/Update（含 unchanged）须同批登记，Supersede 免；本批登记/重算结果批末复查新鲜度；当前有效图成环返回 `dependency_cycle` 及路径；核心依赖插件拒绝；同集重复登记 unchanged | 当时位于 `memory._Txn`；P1 后共享逻辑抽到 `engine.py`（以代码为准） |

测试：`libs/ear_contracts/tests/test_p0_contract_models.py`、`libs/ear_storage_client/tests/test_p0_sdk_models.py`、`test_p0_memory_dependencies.py`，加 contracts 示例；当时全量 **426 passed、0 xfail**。

边界：核心类型能力项全部 false，强制登记目前不对任何内置类型生效；内存实现中 `client(module)` 仍信任自报模块名（可信身份只在 P1 服务路径）；提案 2.10 要求的 `dependency_cycle` 旧客户端端到端兼容测试**未确认已补**，合并前审核须核对。

## P1：持久化服务、可信身份与远程 SDK（本分支实现，提交记录为准，**未合并**）

依据：ADR-0001；[P1 工程指南](persistent-service-plan.md)（已按实际实现修订）；上述用户授权。本页不记录 P1 SHA，提交后以分支历史与 PR8 为准。**未经合并前人工审核**（负责人兼 I），未通知③④。

| 范围 | 实际实现 | 位置 |
|---|---|---|
| 包与依赖 | 服务包 `ear-storage` 加入 uv workspace（psycopg 3、FastAPI、uvicorn）；SDK 增加 httpx；pyright/pytest 覆盖 `services/storage`；pytest marker `postgres` | `services/storage/pyproject.toml`、根 `pyproject.toml`、`uv.lock` |
| schema 与迁移 | `SCHEMA_VERSION = 3`：revision 1 基础 DDL（不可变）→ 2 身份（`principals`、`type_grants`）→ 3 snapshot（`snapshot_leases`、`snapshot_items`），均由显式 `migrate` 执行；启动/每次连接 `check_schema` 不匹配即拒绝；不自动 drop/重建 | `schema.py` |
| 数据布局 | `storage` schema 公共表（meta、registry、publishers、modules、identity、dependency_sets/edges、processed、published、outbox、idempotency、principals、type_grants、snapshot_*）+ 每模块独立 schema 的规范化表；payload 以 `canonical_json` 规范文本保存与比较 | `schema.py`、`codec.py`、`reader.py` |
| 写事务 | 每个操作一个短 `READ COMMITTED` 事务，第一步取全库 advisory 锁 `WRITER_GATE`；写、读、snapshot 创建和身份管理共用此闸；认证在取闸后进行；业务数据、依赖、消费标记、Outbox 行和幂等回执在同一事务原子提交 | `service.py`、`backend.py` |
| 有界校验 | 每次写只加载有界当前图投影：`Limits` 默认 10 000 节点、50 000 边、32 MiB 投影、2 000 操作/批、8 MiB 请求；超限 `ProjectionLimit` 失败关闭、不提交；成本与当前图规模相关，属公开已知限制 | `repository.py`、`backend.py` |
| 身份与授权 | `secrets.token_urlsafe(32)` token，只存 SHA-256 摘要，绑定模块 principal；无隐式授权（含所有者读）；grant 行即读权限，`can_subscribe` 为附加能力；全部 Reader 路径授权，依赖两端、计数均检查，错误详情脱敏；写入需对实际输入类型有读权限 | `auth.py`、`service.py`、`reader.py` |
| `AuthOperator` | 部署环境内部的可信 Python 能力（签发、撤销、整体替换授权），不经 token 认证的传输暴露，没有 CLI/HTTP 入口；**不是 P3 日常管理或管理反馈流程的替代** | `auth.py` |
| 请求 ID 重放 | 回执按 principal 作用域；同 ID 同内容返回已存结果；重放时按当前凭据并对**原请求实际输入**复核读权限（修复了复用旧依赖输入的权限漏洞，有回归测试）；P1 不清理幂等回执 | `service.py`、`test_auth.py::test_reused_input_*` |
| snapshot | 有界物化到 `snapshot_items`，在全局写闸下的短 `READ COMMITTED` 读写事务中创建（**不是** `REPEATABLE READ READ ONLY`），无跨请求长事务；每次读取复核 token、租约归属、TTL 与当前读授权；`SnapshotLimits` 默认 50 000 行、32 MiB、每 principal 4 个活跃租约、TTL 默认 300 s / 最大 3600 s；`sweep_expired` 分批清理（CLI 未提供清理命令） | `snapshots.py` |
| HTTP | FastAPI：`GET /v1/identity`、`GET /v1/registry`、`POST /v1/batches`、`POST /v1/read`、`POST /v1/snapshots`、`DELETE /v1/snapshots/{snapshot_id}`；请求体默认上限 8 MiB、读取超时 15 s | `http.py` |
| 远程 SDK | 同步 `RemoteStorageClient` / `RemoteReader`；不读环境变量、无隐藏重试；`write(..., request_id=None)` 默认 UUIDv7；传输中断、服务不可用或回执异常抛 `CommitUnknown(request_id)`，调用方用同 ID、同 ops、同 context 重放，`write_with_retry` 不自动重算；`subscribe` 抛 `NotImplementedError`（P2） | `remote.py`、`remote_errors.py`、`wire.py` |
| CLI | `ear-storage {migrate,bootstrap,serve}`；只读显式 `EAR_STORAGE_DSN`，不读 `.env`、无默认库；`serve` 不自动迁移 | `cli.py` |
| CI | 测试步骤预拉 `postgres:17-alpine` 并设 `EAR_RUN_POSTGRES_TESTS=1` | `.github/workflows/ci.yml` |

验证（提交前的本地完整检查日志 `/tmp/ear-p1http-full-OK7Aeg.log`，exit 0，对应当时的工作区快照；提交后以提交记录与新的运行结果为准）：ruff check、ruff format --check、pyright（0 错误）、启用真实 PostgreSQL 的 `pytest` **644 passed、0 skipped、1 warning**（Starlette TestClient 的 httpx 弃用警告）75.11 s、`check_docs.py`、`check_docs.py --base origin/dev`、`git diff --check` 均通过。`uv lock --check` 实测 exit 0（锁文件一致）。

- 测试位于 `services/storage/tests/`：`test_auth.py`、`test_codec.py`、`test_http.py`、`test_http_postgres.py`（真实 TCP 下提交后响应丢失、同 ID 重放字节一致且不重复发事件）、`test_p1a_regressions.py`（独立进程竞争）、`test_postgres.py`（重启持久、原子冲突、重放）、`test_snapshots.py`；含 revision 1 真实 DDL 升级保留记录/边/Outbox/重放的测试。
- 默认不设 `EAR_RUN_POSTGRES_TESTS=1` 时 PostgreSQL 测试**明确 skip**，skip 数以该次 pytest 的实际输出为准，不预先写数字，也不能与上面“全启用 644/0 skip”混称。
- 启用后只使用 `services/storage/tests/conftest.py` 起的**独立 Docker 临时库**：本机 Docker、`postgres:17-alpine`（`--pull=never`，镜像需已在本地，CI 步骤预拉）、`--rm`、随机容器名与随机密码、只绑定 `127.0.0.1` 的随机端口、数据在 tmpfs，结束后只清理带本次任务标签的容器。**不支持外部数据库**：启用时若环境里设置了 `EAR_POSTGRES_TEST_DSN`，夹具直接抛 `RuntimeError` 拒绝运行，不会连接该 DSN，因此不能用它指向开发库、共享库或生产库。
- CI 改动**尚未在远程运行**这批代码。

边界与未完成：

- Outbox 已在事务内持久写入，但 **Relay、JetStream、持久消费者、远程订阅未做**（P2）；当前没有任何进程把 Outbox 投递出去。
- 全局写闸使所有操作串行，首版保正确不保吞吐；有界图投影的上限与成本对调用方公开，超限即拒绝。
- 动态注册、凭据生命周期、管理反馈（P3），业务接口（P4），插件历史/多层（P5）未做；核心能力项仍为 false，待③④等业务负责人提供实际输入。
- 幂等回执当前不清理，保留策略属部署事项，需与 I 确定。

未完成项不得因文档检查通过而标为完成；进度更新必须对应实际提交和测试结果。
